#!/usr/bin/env python3
"""Archive xiasl.net from the Wayback Machine: full CDX inventory, local analysis,
canonicalization, digest-aware job planning, blob deduplication.

Layered model:

    logical_object -> original URL -> capture -> blob

The CDX inventory is kept complete and immutable; canonicalization, digest
grouping and planning are derived layers that can be rebuilt at any time.
"""
from __future__ import annotations

import argparse
import collections
import errno
import gzip
import hashlib
import http.client
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

CDX_ENDPOINT = 'https://web.archive.org/cdx/search/cdx'
REPLAY_BASE = 'https://web.archive.org/web/'
FIELDS = 'timestamp,original,mimetype,statuscode,digest,length'
LOG = logging.getLogger('wayback')
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
SCHEMA_VERSION = '2'
EXHAUST_ATTEMPTS = 6  # retries after which a job counts as permanently unavailable

# Failure classes used for scheduling and reporting.
RATE_LIMITED = 'rate_limited'
TEMPORARY = 'temporary_wayback_error'
NETWORK = 'network_error'
REPLAY_MISSING = 'replay_missing'
HISTORICAL_FAILURE = 'historical_failure'
INVALID_CAPTURE = 'invalid_capture'
RUNTIME_ERROR = 'wayback_runtime_error'
LOCAL_CORRUPTION = 'local_corruption'
PERMANENT = 'permanent_error'
PERMANENT_CLASSES = {HISTORICAL_FAILURE, INVALID_CAPTURE, PERMANENT}
TEMP_CLASSES = {RATE_LIMITED, TEMPORARY, NETWORK, RUNTIME_ERROR, REPLAY_MISSING}

PRIORITY_LABELS = {0: 'content', 1: 'attachment', 2: 'auxiliary', 3: 'appearance', 4: 'low-value'}

# Resource types and their default download priority.
RESOURCE_PRIORITY = {
    'thread': 0, 'thread_page': 0, 'forum': 0, 'article': 0, 'generic_html': 0,
    'attachment': 1, 'document': 1, 'archive': 1,
    'image': 2, 'user': 2, 'avatar': 2, 'font': 3,
    'css': 3, 'javascript': 3,
    'tracker': 4, 'ad': 4, 'other': 4,
}

IMAGE_MIMES = {'image/jpeg', 'image/pjpeg', 'image/gif', 'image/png', 'image/bmp', 'image/webp',
               'image/vnd.microsoft.icon', 'image/x-icon', 'image/svg+xml', 'image/tiff'}
DOC_MIMES = {'application/pdf', 'application/msword', 'application/vnd.ms-excel', 'application/rtf',
             'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
ARCHIVE_MIMES = {'application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed',
                 'application/x-tar', 'application/gzip', 'application/x-gzip', 'application/x-bzip2'}
FONT_MIMES = {'font/woff', 'font/woff2', 'font/ttf', 'font/otf', 'application/font-woff',
              'application/x-font-ttf', 'application/vnd.ms-fontobject'}
EXTENSION_BY_MIMETYPE = {
    'text/html': '.html', 'application/xhtml+xml': '.html', 'text/css': '.css',
    'application/javascript': '.js', 'application/x-javascript': '.js', 'text/javascript': '.js',
    'image/jpeg': '.jpg', 'image/pjpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
    'image/bmp': '.bmp', 'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/tiff': '.tif',
    'image/vnd.microsoft.icon': '.ico', 'image/x-icon': '.ico',
    'application/pdf': '.pdf', 'application/zip': '.zip', 'application/x-rar-compressed': '.rar',
    'text/plain': '.txt', 'text/xml': '.xml', 'application/xml': '.xml', 'application/json': '.json',
    'text/vnd.wap.wml': '.wml', 'application/x-shockwave-flash': '.swf', 'audio/mid': '.mid',
    'font/woff': '.woff', 'font/woff2': '.woff2', 'font/ttf': '.ttf', 'font/otf': '.otf',
}

# Query parameters that never change the archived content.
NOISE_PARAMS = {'extra', 'highlight', 'sid', 'from', 'referer', 'referrer', 'rand', 'rpid',
                'formhash', 'inajax', 'ajaxdata', 'searchid', 'mobile', 'mobiletype', 'waptype',
                'ordertype', 'orderby', 'filter', 'page_'}

# Low-value endpoints (counters, AJAX helpers, redirect shells, ads).
UTILITY_MARKERS = ('do.php', 'batch.panel.php', 'xajax.php', 'redirect.php', 'misc.php',
                   'ajax.php', 'getHits.asp', 'sendmail.asp', 'comment.asp?action=js',
                   'counter', 'analytics', '/ad/', 'ad_', 'advert', 'stat.php', 'pixel')

UNDERSCORE_EXT = {'_avatar_middle.jpg', '_avatar_small.jpg', '_avatar_big.jpg', '.thumb.jpg'}


class FetchError(Exception):
    """Current transport/replay failure; retryable unless the class says otherwise."""

    def __init__(self, message, kind=NETWORK, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class RejectedCapture(Exception):
    """Definite historical failure or unusable body."""

    def __init__(self, message, kind=HISTORICAL_FAILURE):
        super().__init__(message)
        self.kind = kind


class Pacer:
    """Adaptive pacing: slow down on rate limits, recover slowly when healthy."""

    def __init__(self, delay, minimum=None, maximum=None, window=50, enabled=True):
        self.delay = float(delay)
        self.minimum = float(delay if minimum is None else minimum)
        self.maximum = float(max(maximum or 0.0, self.minimum * 8, 30.0))
        self.enabled = enabled
        self.window = collections.deque(maxlen=window)
        self.ok_streak = 0

    def record(self, outcome, latency):
        self.window.append((outcome, latency))
        if outcome == 'ok':
            self.ok_streak += 1
        else:
            self.ok_streak = 0
        self.adjust()

    def adjust(self):
        if not self.enabled or not self.window:
            return
        total = len(self.window)
        throttled = sum(1 for outcome, _ in self.window if outcome in (RATE_LIMITED, TEMPORARY))
        refused = sum(1 for outcome, _ in self.window if outcome == NETWORK)
        if total >= 10 and (throttled / total > 0.10 or refused / total > 0.25):
            self.delay = min(self.maximum, max(self.minimum, self.delay * 1.5))
            self.window.clear()
        elif self.ok_streak >= max(30, total) and self.delay > self.minimum:
            self.delay = max(self.minimum, self.delay * 0.9)

    def snapshot(self):
        total = len(self.window)
        return {
            'delay': round(self.delay, 2),
            'window': total,
            'rate_limited': sum(1 for outcome, _ in self.window if outcome == RATE_LIMITED),
            'temporary': sum(1 for outcome, _ in self.window if outcome == TEMPORARY),
            'network': sum(1 for outcome, _ in self.window if outcome == NETWORK),
            'avg_latency': round(sum(latency for _, latency in self.window) / total, 3) if total else 0.0,
        }


class ArchiveRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != 'web.archive.org':
            raise FetchError(f'refused redirect outside HTTPS Wayback: {newurl}', INVALID_CAPTURE)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, delay=1.5, timeout=45.0, retries=4, user_agent=None,
                 max_delay=None, adaptive=True):
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.next_request = 0.0
        self.opener = urllib.request.build_opener(ArchiveRedirects())
        self.headers = {
            'User-Agent': user_agent or 'XiaslWaybackArchiver/2.0 (personal archival; Python urllib)',
            'Accept-Encoding': 'identity',
        }
        self.pacer = Pacer(delay, minimum=delay, maximum=max_delay, enabled=adaptive)
        self.stats = collections.Counter()

    def get(self, url, max_bytes):
        last = None
        for attempt in range(self.retries + 1):
            time.sleep(max(0.0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.pacer.delay
            retry_after = None
            started = time.monotonic()
            try:
                request = urllib.request.Request(url, headers=self.headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise FetchError(f'HTTP {response.status}: {url}', TEMPORARY)
                    body = response.read(max_bytes + 1)
                    advertised_length = response.headers.get('Content-Length')
                    if advertised_length and advertised_length.isdigit() and len(body) < int(advertised_length):
                        raise http.client.IncompleteRead(body, int(advertised_length) - len(body))
                    if len(body) > max_bytes:
                        raise FetchError(f'response exceeds {max_bytes} bytes', PERMANENT)
                    if response.headers.get('Content-Encoding', '').lower() == 'gzip':
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                            body = stream.read(max_bytes + 1)
                        if len(body) > max_bytes:
                            raise FetchError('decompressed response exceeds size limit', PERMANENT)
                    self.pacer.record('ok', time.monotonic() - started)
                    self.stats['ok'] += 1
                    return body, dict(response.headers.items()), response.geturl()
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get('Retry-After')
                if exc.code == 429:
                    kind = RATE_LIMITED
                elif exc.code == 404:
                    kind = REPLAY_MISSING
                elif exc.code in RETRYABLE:
                    kind = TEMPORARY
                else:
                    kind = PERMANENT
                last = FetchError(f'HTTP {exc.code}: {url}', kind, retry_after)
                exc.close()
                self.pacer.record(kind, time.monotonic() - started)
                self.stats[kind] += 1
                if kind in (REPLAY_MISSING, PERMANENT):
                    raise last from exc
            except FetchError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, EOFError, http.client.HTTPException) as exc:
                last = FetchError(str(exc), NETWORK)
                self.pacer.record(NETWORK, time.monotonic() - started)
                self.stats[NETWORK] += 1
            if attempt < self.retries:
                wait = max(self.pacer.delay, min(120.0, 2.0 ** attempt * 2))
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        try:
                            wait = max(wait, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            pass
                wait = min(wait, 300.0)
                LOG.warning('%s; retry in %.1fs (%d/%d)', last, wait, attempt + 1, self.retries)
                self.next_request = max(self.next_request, time.monotonic() + wait)
        raise last or FetchError('request failed')


# --------------------------------------------------------------------------- CDX

def split_record(line, style='plain'):
    """Split a CDX row whose `original` may itself contain spaces (request URLs)."""
    if style == 'plain':
        if ' ' not in line:
            return None
        timestamp, rest = line.split(' ', 1)
        if not re.fullmatch(r'\d{14}', timestamp):
            return None
    else:
        parts = line.split(' ', 2)
        if len(parts) != 3 or not re.fullmatch(r'\d{14}', parts[1]):
            return None
        timestamp, rest = parts[1], parts[2]
    tail = rest.rsplit(' ', 4)
    if len(tail) != 5:
        return None
    original, mimetype, statuscode, digest, length = tail
    if not original.startswith(('http://', 'https://')):
        return None  # Guards against a space that actually belongs to urlkey.
    if not re.fullmatch(r'[-\w.+]+/[-\w.+]+|unk|warc/revisit', mimetype):
        return None
    if not re.fullmatch(r'\d{3}|-', statuscode) or not re.fullmatch(r'\d+|-', length):
        return None
    return {'timestamp': timestamp, 'original': original, 'mimetype': mimetype,
            'statuscode': statuscode, 'digest': digest, 'length': length}


def parse_cdx_json(text, fields=FIELDS.split(',')):
    """JSON output: optional header row, data rows, `[]`, then the resume token."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f'invalid CDX JSON: {exc}', TEMPORARY) from exc
    if not isinstance(payload, list):
        raise FetchError('unexpected CDX JSON payload', TEMPORARY)
    records, resume_key, separator_seen = [], None, False
    for row in payload:
        if row == []:
            if separator_seen:
                raise FetchError('unexpected duplicate CDX JSON separator', TEMPORARY)
            separator_seen = True
            continue
        if not isinstance(row, list):
            raise FetchError(f'unexpected CDX JSON row: {str(row)[:160]}', TEMPORARY)
        if separator_seen:
            if resume_key is not None:
                raise FetchError('unexpected CDX data after resume key', TEMPORARY)
            resume_key = str(row[0]) if row else ''
            continue
        if row and row[0] == 'timestamp':
            continue
        if len(row) != len(fields):
            raise FetchError(f'unexpected CDX JSON row: {str(row)[:160]}', TEMPORARY)
        record = dict(zip(fields, (str(value) for value in row)))
        if not re.fullmatch(r'\d{14}', record['timestamp']):
            raise FetchError(f'unexpected CDX JSON timestamp: {record["timestamp"][:40]}', TEMPORARY)
        records.append(record)
    if resume_key == '':
        raise FetchError('empty CDX resume key', TEMPORARY)
    return records, resume_key


def parse_cdx(text, fields=FIELDS.split(',')):
    """Parse either the JSON or the legacy plain-text CDX response."""
    if text.lstrip().startswith('['):
        return parse_cdx_json(text, fields)
    records, resume_key, delimiter, style = [], None, False, None
    if not text.strip():
        return records, resume_key
    for line in text.splitlines():
        if not line.strip():
            delimiter = True
            continue
        if delimiter:
            if resume_key is not None:
                raise FetchError(f'unexpected CDX data after resume key: {line[:160]}')
            resume_key = line.strip()
            continue
        if style is None:
            style = 'plain' if line.split(' ', 1)[0].isdigit() else 'urlkey'
        record = split_record(line, style)
        if record is None:
            raise FetchError(f'unexpected CDX response: {line[:160]}')
        records.append(record)
    if delimiter and resume_key is None:
        raise FetchError('truncated CDX continuation: delimiter without token')
    return records, resume_key


def in_domain(url, domain):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        return parsed.scheme in ('http', 'https') and (host == domain or host.endswith('.' + domain))
    except ValueError:
        return False


def source_key(url):
    """HTTP/HTTPS/default ports are equivalent only when checking replay redirects."""
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    host = (parsed.hostname or '').lower()
    if port and port not in (80, 443):
        host += f':{port}'
    return host, parsed.path or '/', parsed.query


# ------------------------------------------------------------- canonicalization

def split_query(query):
    """Keep raw percent-encoding: query values on this site are GBK bytes."""
    pairs = []
    for chunk in query.split('&'):
        if not chunk:
            continue
        key, _, value = chunk.partition('=')
        pairs.append((key, value))
    return pairs


def percent_key(key):
    try:
        return urllib.parse.unquote(key, errors='replace').lower()
    except Exception:  # pragma: no cover - defensive
        return key.lower()


def normalized_host(netloc):
    host = netloc.lower()
    if host.endswith(':80') or host.endswith(':443'):
        host = host.rsplit(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


def clean_query(pairs, drop=None):
    drop = NOISE_PARAMS | set(drop or ())
    kept = [(key, value) for key, value in pairs if percent_key(key) not in drop]
    kept.sort(key=lambda item: (percent_key(item[0]), item[1]))
    return '&'.join(f'{key}={value}' if value != '' else key for key, value in kept)


def query_get(pairs, name):
    name = name.lower()
    for key, value in pairs:
        if percent_key(key) == name:
            return value
    return None


def integer(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def classify_resource(mimetype, path, query):
    """Return (resource_type, priority) from the resource itself."""
    mime = (mimetype or '').split(';')[0].strip().lower()
    lowered_path = path.lower()
    lowered_query = query.lower()
    if any(marker in lowered_path for marker in UNDERSCORE_EXT):
        return 'avatar', RESOURCE_PRIORITY['avatar']
    if '/avatar/' in lowered_path:
        return 'avatar', RESOURCE_PRIORITY['avatar']
    if '/attachments/' in lowered_path or '/attachment/' in lowered_path or 'attachment.php' in lowered_path:
        if lowered_path.endswith(('.thumb.jpg', '.thumb.png')):
            return 'image', RESOURCE_PRIORITY['image']
        return 'attachment', RESOURCE_PRIORITY['attachment']
    if any(marker in lowered_path + lowered_query for marker in UTILITY_MARKERS):
        if '/ad/' in lowered_path or 'ad_' in lowered_path:
            return 'ad', RESOURCE_PRIORITY['ad']
        return 'tracker', RESOURCE_PRIORITY['tracker']
    if '/ad/' in lowered_path or re.search(r'(^|[/_.-])ads?(/|[_.-]|$)', lowered_path):
        return 'ad', RESOURCE_PRIORITY['ad']
    if mime in IMAGE_MIMES:
        return 'image', RESOURCE_PRIORITY['image']
    if mime in DOC_MIMES:
        return 'document', RESOURCE_PRIORITY['document']
    if mime in ARCHIVE_MIMES:
        return 'archive', RESOURCE_PRIORITY['archive']
    if mime in FONT_MIMES or lowered_path.endswith(('.woff', '.woff2', '.ttf', '.otf', '.eot')):
        return 'font', RESOURCE_PRIORITY['font']
    if mime == 'text/css' or lowered_path.endswith('.css'):
        return 'css', RESOURCE_PRIORITY['css']
    if mime in ('application/javascript', 'application/x-javascript', 'text/javascript') \
            or lowered_path.endswith('.js'):
        return 'javascript', RESOURCE_PRIORITY['javascript']
    if mime in ('text/html', 'application/xhtml+xml', 'warc/revisit', 'unk', '') or not mime:
        return 'generic_html', RESOURCE_PRIORITY['generic_html']
    return 'other', RESOURCE_PRIORITY['other']


def classify(original, mimetype='text/html'):
    """Map an original URL to (canonical_key, object_type, object_id, page, resource_type, priority).

    Rules are deliberately conservative: anything not clearly recognised keeps its
    own normalized URL as its identity instead of being merged with something else.
    """
    parsed = urllib.parse.urlsplit(original)
    host = normalized_host(parsed.netloc)
    path = parsed.path or '/'
    lowered = path.lower()
    pairs = split_query(parsed.query)
    query = parsed.query or ''
    resource_type, priority = classify_resource(mimetype, path, query)
    tail = lowered.rsplit('/', 1)[-1]

    def result(key, object_type, object_id, page=None, resource=None, prio=None):
        return (key, object_type, object_id, page,
                resource or resource_type, priority if prio is None else prio)

    # Discuz rewritten thread: thread-<tid>-<page>-<extra>.html / viewthread-<tid>.html
    match = re.fullmatch(r'(?:thread|viewthread)-(\d+)-(\d+)-(\d+)\.html', tail)
    if match:
        tid, page = match.group(1), max(1, integer(match.group(2)) or 1)
        kind = 'thread' if page == 1 else 'thread_page'
        return result(f'thread:{tid}:page:{page}', kind, tid, page, kind, 0)
    match = re.fullmatch(r'viewthread-(\d+)\.html', tail)
    if match:
        return result(f'thread:{match.group(1)}:page:1', 'thread', match.group(1), 1, 'thread', 0)
    match = re.fullmatch(r'forum-(\d+)-(\d+)\.html', tail)
    if match:
        fid, page = match.group(1), max(1, integer(match.group(2)) or 1)
        return result(f'forum:{fid}:page:{page}', 'forum', fid, page, 'forum', 0)
    match = re.fullmatch(r'space-uid-(\d+)\.html', tail)
    if match:
        return result(f'user:{match.group(1)}', 'user', match.group(1), 1, 'user', 2)

    tid = integer(query_get(pairs, 'tid'))
    if 'viewthread.php' in lowered or 'viewthread' in tail:
        if tid:
            page = max(1, integer(query_get(pairs, 'page')) or 1)
            kind = 'thread' if page == 1 else 'thread_page'
            return result(f'thread:{tid}:page:{page}', kind, str(tid), page, kind, 0)
        return result(f'generic:{host}{lowered}?{clean_query(pairs)}', 'generic_html', None, None)

    fid = integer(query_get(pairs, 'fid'))
    if 'forumdisplay.php' in lowered or ('forum.php' in lowered and fid):
        if fid:
            page = max(1, integer(query_get(pairs, 'page')) or 1)
            return result(f'forum:{fid}:page:{page}', 'forum', str(fid), page, 'forum', 0)

    uid = integer(query_get(pairs, 'uid'))
    if 'space.php' in lowered or '/home.php' in lowered or lowered.endswith('home.php'):
        if uid:
            page = max(1, integer(query_get(pairs, 'page')) or 1)
            op = query_get(pairs, 'op') or query_get(pairs, 'do')
            suffix = f':{percent_key(op)}' if op else ''
            return result(f'user:{uid}{suffix}:page:{page}', 'user', str(uid), page, 'user', 2)

    aid = integer(query_get(pairs, 'aid') or query_get(pairs, 'attachmentid'))
    if aid and 'attachment' in lowered:
        return result(f'attachment:{aid}', 'attachment', str(aid), None, 'attachment', 1)

    # Legacy ASP article system.
    newsid = integer(query_get(pairs, 'newsid') or query_get(pairs, 'articleid'))
    match = re.fullmatch(r'shownews_?(\d+)\.asp', tail) or re.fullmatch(r'(\d+)\.html', tail)
    if match and 'article' in lowered:
        newsid = newsid or integer(match.group(1))
    if newsid and ('article' in lowered or 'shownews' in tail or 'showauthor' in tail):
        variant = ''
        if 'comment.asp' in lowered:
            variant = ':comment'
        elif 'print.asp' in lowered:
            variant = ':print'
        elif 'sendmail.asp' in lowered:
            variant = ':mail'
        return result(f'article:{newsid}{variant}', 'article', str(newsid), None, 'article', 0)
    if newsid and tail.endswith('.html') and '/article/' in lowered:
        return result(f'article:{newsid}', 'article', str(newsid), None, 'article', 0)

    author = query_get(pairs, 'authorname')
    if author and 'showauthor' in tail:
        channel = query_get(pairs, 'channelid') or ''
        return result(f'author:{author}:channel:{channel}', 'user', author, None, 'user', 2)

    name = query_get(pairs, 'name')
    if name and tail.startswith('tag.php'):
        return result(f'tag:{name}', 'generic_html', None, None, 'generic_html', 0)

    if resource_type in ('attachment', 'image', 'avatar'):
        identifier = hashlib.sha1(original.encode('utf-8', 'replace')).hexdigest()[:12]
        return result(f'{resource_type}:{identifier}', resource_type, identifier, None)

    normalized = f'{host}{lowered}'
    cleaned = clean_query(pairs)
    if cleaned:
        normalized = f'{normalized}?{cleaned}'
    return result(f'generic:{normalized}', resource_type, None, None)


def capture_score(row, canonical_page=None):
    """Explainable capture score; higher is better. Used for preferred selection."""
    score = 0
    status = str(row['statuscode'])
    if status == '200':
        score += 50
    elif status == '-':
        score -= 25
    else:
        score -= 40
    digest = (row['digest'] or '').strip()
    if not digest or digest == '-':
        score -= 20
    mimetype = (row['mimetype'] or '').lower()
    if mimetype == 'warc/revisit':
        score -= 15
    if mimetype.startswith('text/html'):
        score += 10
    length = integer(row['length'])
    if length is None or length <= 0:
        score -= 10
    elif length < 200:
        score -= 5
    if length and length > 5_000_000:
        score -= 5
    original = row['original']
    if re.search(r'(?i)(thread|viewthread|forum)-\d+-\d+-\d+\.html$', urllib.parse.urlsplit(original).path):
        score += 8  # rewritten Discuz URL: the plainest representation
    elif '?' not in original:
        score += 4
    if canonical_page and re.search(r'(?i)viewthread\.php', original) and 'page=' not in original:
        score += 3
    state = row['state'] if 'state' in row.keys() else 'pending'
    if state == 'downloaded':
        score += 30
    parsed_host = normalized_host(urllib.parse.urlsplit(original).netloc)
    if parsed_host in ('xiasl.net', 'bbs.xiasl.net'):
        score += 2  # keep the canonical host ahead of deep subdomains on ties
    return score  # ties are broken by newest timestamp in SQL


# ------------------------------------------------------------------- database

def open_database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS captures (
            timestamp TEXT NOT NULL, original TEXT NOT NULL, mimetype TEXT NOT NULL,
            statuscode TEXT NOT NULL, digest TEXT NOT NULL, length TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', path TEXT, error TEXT,
            sha256 TEXT, bytes INTEGER, replay_url TEXT,
            PRIMARY KEY (timestamp, original)
        );
        CREATE INDEX IF NOT EXISTS capture_order ON captures(original, timestamp DESC);
        CREATE TABLE IF NOT EXISTS urls (
            original TEXT PRIMARY KEY, canonical_key TEXT NOT NULL, object_type TEXT,
            object_id TEXT, page INTEGER, scheme TEXT, host TEXT, url_path TEXT, query TEXT,
            resource_type TEXT NOT NULL, priority INTEGER NOT NULL,
            capture_count INTEGER DEFAULT 0, digest_count INTEGER DEFAULT 0,
            first_timestamp TEXT, last_timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS logical_objects (
            canonical_key TEXT PRIMARY KEY, object_type TEXT, object_id TEXT, page INTEGER,
            resource_type TEXT NOT NULL, priority INTEGER NOT NULL,
            url_count INTEGER DEFAULT 0, capture_count INTEGER DEFAULT 0, digest_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS logical_priority ON logical_objects(priority, canonical_key);
        CREATE TABLE IF NOT EXISTS object_digests (
            canonical_key TEXT NOT NULL, digest TEXT NOT NULL, capture_count INTEGER DEFAULT 0,
            best_score INTEGER, preferred_timestamp TEXT, preferred_original TEXT,
            length TEXT, mimetype TEXT, PRIMARY KEY (canonical_key, digest)
        );
        CREATE TABLE IF NOT EXISTS download_jobs (
            job_key TEXT PRIMARY KEY, canonical_key TEXT NOT NULL, digest TEXT NOT NULL,
            resource_type TEXT NOT NULL, priority INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
            preferred_timestamp TEXT, preferred_original TEXT, preferred_score INTEGER,
            blob_sha256 TEXT, blob_path TEXT, bytes INTEGER, replay_url TEXT,
            failure_class TEXT, last_error TEXT, last_attempt TEXT, next_retry_at TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS job_schedule ON download_jobs(state, priority, next_retry_at);
        CREATE TABLE IF NOT EXISTS job_candidates (
            job_key TEXT NOT NULL, timestamp TEXT NOT NULL, original TEXT NOT NULL,
            score INTEGER NOT NULL, position INTEGER NOT NULL, tried INTEGER DEFAULT 0,
            failure_class TEXT, digest TEXT, PRIMARY KEY (job_key, timestamp, original)
        );
        CREATE TABLE IF NOT EXISTS blobs (
            sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL,
            mimetype TEXT, source_timestamp TEXT, source_original TEXT, first_seen TEXT,
            ref_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS links (
            source_canonical_key TEXT NOT NULL, source_timestamp TEXT, target_url TEXT NOT NULL,
            target_canonical_key TEXT, link_type TEXT, seen_count INTEGER DEFAULT 1,
            last_seen TEXT, PRIMARY KEY (source_canonical_key, target_url)
        );
        CREATE TABLE IF NOT EXISTS unresolved_links (
            source_canonical_key TEXT NOT NULL, source_timestamp TEXT, target_url TEXT NOT NULL,
            target_canonical_key TEXT, link_type TEXT, seen_count INTEGER DEFAULT 1, last_seen TEXT,
            PRIMARY KEY (source_canonical_key, target_url)
        );
        CREATE TABLE IF NOT EXISTS suspected_gaps (
            canonical_key TEXT PRIMARY KEY, object_type TEXT, missing_pages TEXT NOT NULL,
            evidence TEXT, recorded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, url TEXT, outcome TEXT, latency REAL
        );
    ''')
    migrate(db)
    return db


CAPTURE_COLUMNS = {
    'canonical_key': 'TEXT', 'object_type': 'TEXT', 'object_id': 'TEXT', 'page': 'INTEGER',
    'resource_type': 'TEXT', 'priority': 'INTEGER', 'scheme': 'TEXT', 'host': 'TEXT',
    'url_path': 'TEXT', 'query': 'TEXT', 'score': 'INTEGER',
    'failure_class': 'TEXT', 'retry_count': 'INTEGER DEFAULT 0', 'last_attempt': 'TEXT',
    'next_retry_at': 'TEXT', 'job_key': 'TEXT', 'blob_sha256': 'TEXT', 'covered_by': 'TEXT',
}


def migrate(db):
    """Add the layered columns/tables to an existing checkpoint without losing rows."""
    existing = {row[1] for row in db.execute('PRAGMA table_info(captures)')}
    extra_tables = {
        'job_candidates': {'digest': 'TEXT'},
        'unresolved_links': {'target_canonical_key': 'TEXT'},
        'links': {'target_canonical_key': 'TEXT'},
    }
    for table, columns in extra_tables.items():
        present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for column, definition in columns.items():
            if column not in present:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    added = []
    for column, definition in CAPTURE_COLUMNS.items():
        if column not in existing:
            db.execute(f'ALTER TABLE captures ADD COLUMN {column} {definition}')
            added.append(column)
    with db:
        db.execute('CREATE INDEX IF NOT EXISTS capture_canonical ON captures(canonical_key, digest)')
        db.execute('CREATE INDEX IF NOT EXISTS capture_schedule ON captures(state, next_retry_at)')
        db.execute('CREATE INDEX IF NOT EXISTS candidate_schedule ON job_candidates(job_key, timestamp)')
        db.execute('CREATE INDEX IF NOT EXISTS capture_digest ON captures(digest)')
        db.execute('CREATE INDEX IF NOT EXISTS job_digest ON download_jobs(digest)')
        set_meta(db, 'schema_version', SCHEMA_VERSION)
    if added:
        LOG.info('Migrated checkpoint: added captures columns %s', ', '.join(added))
    return added


def get_meta(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, value))


def utcnow():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# ------------------------------------------------------------------ CDX index

def collect_index(db, client, args):
    if args.refresh_index:
        with db:
            set_meta(db, 'index_complete', '0')
            set_meta(db, 'resume_key', '')
    if get_meta(db, 'index_complete') == '1':
        LOG.info('Index already complete; use --refresh-index to query again')
        return
    resume = get_meta(db, 'resume_key') or ''
    pages = 0
    while True:
        params = {
            'url': args.domain, 'matchType': 'domain', 'output': 'json',
            'fl': FIELDS, 'filter': 'statuscode:200',
            'limit': str(args.page_size), 'showResumeKey': 'true', 'gzip': 'false',
        }
        if args.from_date:
            params['from'] = args.from_date
        if args.to_date:
            params['to'] = args.to_date
        if resume:
            params['resumeKey'] = resume
        body, _, _ = client.get(CDX_ENDPOINT + '?' + urllib.parse.urlencode(params), 64 * 1024 * 1024)
        records, next_key = parse_cdx(body.decode('utf-8', 'strict'))
        if next_key and next_key == resume:
            raise FetchError('CDX returned the same resume key; refusing an infinite loop', TEMPORARY)
        kept = 0
        with db:
            for record in records:
                if record['statuscode'] != '200' or not in_domain(record['original'], args.domain):
                    continue
                if args.html_only and record['mimetype'] not in ('text/html', 'application/xhtml+xml', 'warc/revisit'):
                    continue
                db.execute('''INSERT OR IGNORE INTO captures
                    (timestamp, original, mimetype, statuscode, digest, length)
                    VALUES (:timestamp, :original, :mimetype, :statuscode, :digest, :length)''', record)
                kept += 1
            set_meta(db, 'resume_key', next_key or '')
            set_meta(db, 'index_complete', '0' if next_key else '1')
        pages += 1
        LOG.info('Index page %d: %d records, %d eligible; more=%s', pages, len(records), kept, bool(next_key))
        if not next_key:
            return
        if args.max_pages and pages >= args.max_pages:
            LOG.warning('Stopped at --max-pages; index will resume next run')
            return
        resume = next_key


# ------------------------------------------------------------- derived layers

def classify_rows(db, batch=5000):
    """Classify captures that have no canonical layer yet (idempotent, incremental).

    Updates are committed per batch: one transaction per capture would dominate the
    runtime on a six-figure inventory.
    """
    sql = '''SELECT timestamp, original, mimetype, statuscode, digest, length, state
             FROM captures WHERE canonical_key IS NULL LIMIT ?'''
    total = 0
    while True:
        rows = db.execute(sql, (batch,)).fetchall()
        if not rows:
            return total
        updates = []
        for row in rows:
            key, object_type, object_id, page, resource_type, priority = classify(row['original'],
                                                                                  row['mimetype'])
            parsed = urllib.parse.urlsplit(row['original'])
            updates.append((key, object_type, object_id, page, resource_type, priority,
                            parsed.scheme, normalized_host(parsed.netloc), parsed.path, parsed.query,
                            capture_score(row, page), row['timestamp'], row['original']))
        with db:
            db.executemany('''UPDATE captures SET canonical_key=?, object_type=?, object_id=?, page=?,
                resource_type=?, priority=?, scheme=?, host=?, url_path=?, query=?, score=?
                WHERE timestamp=? AND original=?''', updates)
        total += len(rows)


def rebuild_layers(db):
    """Rebuild urls / logical_objects / object_digests from the capture inventory."""
    with db:
        db.execute('DELETE FROM urls')
        db.execute('''INSERT INTO urls (original, canonical_key, object_type, object_id, page,
                scheme, host, url_path, query, resource_type, priority, capture_count,
                digest_count, first_timestamp, last_timestamp)
            SELECT original, MAX(canonical_key), MAX(object_type), MAX(object_id), MAX(page),
                   MAX(scheme), MAX(host), MAX(url_path), MAX(query), MAX(resource_type),
                   MIN(priority), COUNT(*), COUNT(DISTINCT digest), MIN(timestamp), MAX(timestamp)
            FROM captures GROUP BY original''')
        db.execute('DELETE FROM logical_objects')
        db.execute('''INSERT INTO logical_objects (canonical_key, object_type, object_id, page,
                resource_type, priority, url_count, capture_count, digest_count)
            SELECT canonical_key, MAX(object_type), MAX(object_id), MAX(page),
                   (SELECT resource_type FROM captures c2 WHERE c2.canonical_key = c.canonical_key
                     ORDER BY priority, timestamp LIMIT 1),
                   MIN(priority), COUNT(DISTINCT original), COUNT(*), COUNT(DISTINCT digest)
            FROM captures c WHERE canonical_key IS NOT NULL GROUP BY canonical_key''')
        db.execute('DELETE FROM object_digests')
        db.execute('''INSERT INTO object_digests (canonical_key, digest, capture_count, best_score,
                preferred_timestamp, preferred_original, length, mimetype)
            WITH ranked AS (
                SELECT canonical_key, digest, timestamp, original, score, length, mimetype,
                       COUNT(*) OVER (PARTITION BY canonical_key, digest) AS cnt,
                       MAX(score) OVER (PARTITION BY canonical_key, digest) AS best,
                       ROW_NUMBER() OVER (PARTITION BY canonical_key, digest
                                          ORDER BY score DESC, timestamp DESC) AS rn
                FROM captures WHERE digest IS NOT NULL AND digest <> '' AND canonical_key IS NOT NULL
            )
            SELECT canonical_key, digest, cnt, best, timestamp, original, length, mimetype
            FROM ranked WHERE rn = 1''')
    return {
        'urls': db.execute('SELECT COUNT(*) FROM urls').fetchone()[0],
        'logical_objects': db.execute('SELECT COUNT(*) FROM logical_objects').fetchone()[0],
        'object_digests': db.execute('SELECT COUNT(*) FROM object_digests').fetchone()[0],
    }


def build_layers(db, replan=False):
    started = time.monotonic()
    classified = classify_rows(db)
    stats = rebuild_layers(db)
    stats['classified'] = classified
    stats['seconds'] = round(time.monotonic() - started, 1)
    LOG.info('Layers: %(classified)d classified, %(urls)d urls, %(logical_objects)d logical objects, '
             '%(object_digests)d object digests (%(seconds)ss)', stats)
    return stats


def detect_gaps(db):
    """Record explicit page-sequence holes as suspected gaps (evidence-based only)."""
    families = collections.defaultdict(set)
    for key, object_id, page in db.execute('''SELECT canonical_key, object_id, page FROM logical_objects
            WHERE object_type IN ('thread', 'thread_page', 'forum') AND page IS NOT NULL'''):
        families[(object_id, key.split(':')[0] if ':' in key else '')].add(page)
    recorded = 0
    with db:
        for (object_id, prefix), pages in families.items():
            if not pages or len(pages) < 2:
                continue
            highest = max(pages)
            missing = [page for page in range(1, highest + 1) if page not in pages]
            if missing:
                key = f'{prefix}:{object_id}'
                db.execute('''INSERT OR REPLACE INTO suspected_gaps
                    (canonical_key, object_type, missing_pages, evidence, recorded_at)
                    VALUES (?, ?, ?, ?, ?)''',
                    (key, 'thread', ','.join(str(page) for page in missing),
                     f'pages present: {",".join(str(page) for page in sorted(pages))}', utcnow()))
                recorded += 1
    return recorded


# --------------------------------------------------------------------- blobs

def blob_relative_path(sha256, mimetype):
    mime = (mimetype or '').split(';')[0].strip().lower()
    suffix = EXTENSION_BY_MIMETYPE.get(mime, '.bin')
    return Path('blobs') / sha256[:2] / (sha256 + suffix)


def atomic_write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    try:
        with temporary.open('wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def register_blob(db, output, sha256, relative, mimetype, size, timestamp=None, original=None):
    with db:
        db.execute('''INSERT INTO blobs (sha256, path, bytes, mimetype, source_timestamp,
                source_original, first_seen, ref_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(sha256) DO UPDATE SET ref_count = ref_count + 1''',
            (sha256, str(relative), size, mimetype, timestamp, original, utcnow()))
    return relative


def register_existing_files(db, output, verify=False):
    """Register already-downloaded files as blobs so their content is never re-fetched."""
    rows = db.execute('''SELECT timestamp, original, mimetype, path, sha256, bytes, digest
        FROM captures WHERE state='downloaded' AND path IS NOT NULL AND sha256 IS NOT NULL''').fetchall()
    registered = verified = missing = 0
    with db:
        for row in rows:
            file_path = output / row['path']
            if not file_path.is_file():
                missing += 1
                continue
            if verify:
                size_ok = row['bytes'] is None or file_path.stat().st_size == row['bytes']
                if not size_ok:
                    missing += 1
                    continue
                verified += 1
            db.execute('''INSERT INTO blobs (sha256, path, bytes, mimetype, source_timestamp,
                    source_original, first_seen, ref_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(sha256) DO UPDATE SET ref_count = ref_count + 1''',
                (row['sha256'], str(row['path']), row['bytes'] or file_path.stat().st_size,
                 row['mimetype'], row['timestamp'], row['original'], utcnow()))
            db.execute('UPDATE captures SET blob_sha256=? WHERE timestamp=? AND original=?',
                       (row['sha256'], row['timestamp'], row['original']))
            registered += 1
    if registered or missing:
        LOG.info('Registered %d existing files as blobs (%d unreadable/missing)%s',
                 registered, missing, f', {verified} size-verified' if verify else '')
    return registered


def verify_blobs(db, output, verify=True):
    """Detect missing/corrupted local blobs and re-queue whatever depended on them."""
    broken = 0
    rows = db.execute('SELECT sha256, path, bytes FROM blobs').fetchall()
    for row in rows:
        file_path = output / row['path']
        intact = file_path.is_file()
        if intact and row['bytes'] is not None:
            intact = file_path.stat().st_size == row['bytes']
        if intact and verify:
            digest = hashlib.sha256()
            with file_path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            intact = digest.hexdigest() == row['sha256']
        if intact:
            continue
        broken += 1
        LOG.warning('Local blob %s is missing or corrupt (%s); it will be fetched again',
                    row['sha256'][:12], row['path'])
        with db:
            db.execute('DELETE FROM blobs WHERE sha256=?', (row['sha256'],))
            db.execute("UPDATE captures SET blob_sha256=NULL, covered_by=NULL, state='pending', "
                       "failure_class=?, error='local blob missing or corrupt' WHERE blob_sha256=?",
                       (LOCAL_CORRUPTION, row['sha256']))
            db.execute("UPDATE download_jobs SET state='pending', blob_sha256=NULL, blob_path=NULL, "
                       "updated_at=? WHERE blob_sha256=?", (utcnow(), row['sha256']))
    if broken:
        LOG.info('Blob verification: %d of %d local blobs need to be fetched again', broken, len(rows))
    return broken


def exhaust_jobs(db, max_attempts=EXHAUST_ATTEMPTS):
    """Stop retrying jobs that have failed too often (e.g. indexed but replay 404).

    Without this, a handful of permanently unavailable captures keep a worker
    alive forever: it wakes up, retries, backs off, exits, and gets restarted.
    """
    rows = db.execute("SELECT job_key FROM download_jobs WHERE state='pending' "
                      "AND attempts >= ? AND failure_class IS NOT NULL", (max_attempts,)).fetchall()
    if not rows:
        return 0
    keys = [(row['job_key'],) for row in rows]
    with db:
        db.executemany("UPDATE download_jobs SET state='failed', next_retry_at=NULL, updated_at=? "
                       "WHERE job_key=?", [(utcnow(), key[0]) for key in keys])
        db.executemany("UPDATE captures SET state='filtered', next_retry_at=NULL, "
                       "error=COALESCE(error, '') || ' (retry attempts exhausted)' "
                       "WHERE job_key=? AND state='error'", keys)
    LOG.info('Exhausted retries for %d jobs (>= %d attempts)', len(rows), max_attempts)
    return len(rows)


def reuse_blob_for_digest(db, digest, sha256):
    """Every capture sharing a CDX digest is covered by one verified blob."""
    with db:
        db.execute('UPDATE captures SET blob_sha256=?, covered_by=? WHERE digest=?',
                   (sha256, sha256, digest))
        db.execute('''UPDATE download_jobs SET state='downloaded', blob_sha256=?, updated_at=?,
                failure_class=NULL, last_error=NULL
            WHERE digest=? AND state <> 'downloaded' ''', (sha256, utcnow(), digest))


# ------------------------------------------------------------------ planning

def job_key_for(canonical_key, digest):
    return f'{canonical_key}|{digest}'


def select_digests(rows, mode):
    """latest: best score (newest first on ties); all: every distinct digest."""
    # Stable two-pass sort: score first, newest preferred capture on ties.
    ordered = sorted(rows, key=lambda row: (str(row['preferred_timestamp'] or ''), row['digest']),
                     reverse=False)
    ordered.sort(key=lambda row: str(row['preferred_timestamp'] or ''), reverse=True)
    ordered.sort(key=lambda row: -(row['best_score'] or 0))
    if mode == 'latest':
        return ordered[:1]
    return ordered


def filter_clause(args, alias='c'):
    where, params = [], []
    if args.min_timestamp:
        where.append(f'{alias}.timestamp >= ?')
        params.append(args.min_timestamp)
    if args.max_timestamp:
        where.append(f'{alias}.timestamp <= ?')
        params.append(args.max_timestamp)
    if getattr(args, 'priority_max', None) is not None:
        where.append(f'{alias}.priority <= ?')
        params.append(args.priority_max)
    if getattr(args, 'resource_type', None):
        kinds = [kind.strip() for kind in args.resource_type.split(',') if kind.strip()]
        where.append(f"{alias}.resource_type IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    return (' AND ' + ' AND '.join(where)) if where else '', params


def plan_jobs(db, args, replan=False, batch=2000):
    """Create download jobs: one per (logical object, digest) for the selected mode.

    All layers are read in a single pass and inserted in batches; a per-job
    transaction would dominate the runtime on a six-figure inventory.
    """
    clause, params = filter_clause(args)
    if replan:
        with db:
            db.execute("DELETE FROM download_jobs WHERE state='pending'")
    now = utcnow()
    digests_by_object = collections.defaultdict(list)
    objects = {}
    for row in db.execute(f'''SELECT od.canonical_key, od.digest, od.best_score,
                od.preferred_timestamp, od.preferred_original, od.capture_count,
                lo.resource_type, lo.priority
            FROM object_digests od JOIN logical_objects lo USING (canonical_key)
            JOIN captures c ON c.canonical_key = od.canonical_key AND c.digest = od.digest {clause}
            GROUP BY od.canonical_key, od.digest
            ORDER BY lo.priority, od.canonical_key''', params):
        digests_by_object[row['canonical_key']].append(row)
        objects[row['canonical_key']] = (row['resource_type'], row['priority'])

    captures_by_object = collections.defaultdict(list)
    for row in db.execute('''SELECT canonical_key, digest, timestamp, original, score
            FROM captures WHERE statuscode='200' AND canonical_key IS NOT NULL
            ORDER BY canonical_key, score DESC, timestamp DESC'''):
        captures_by_object[row['canonical_key']].append(row)

    job_rows, candidate_rows, created = [], [], 0
    for canonical_key, digests in digests_by_object.items():
        resource_type, priority = objects[canonical_key]
        for chosen in select_digests(digests, args.mode):
            key = job_key_for(canonical_key, chosen['digest'])
            job_rows.append((key, canonical_key, chosen['digest'], resource_type, priority,
                             chosen['preferred_timestamp'], chosen['preferred_original'],
                             chosen['best_score'], now, now))
            same_digest = [c for c in captures_by_object[canonical_key]
                           if c['digest'] == chosen['digest']]
            candidates = same_digest
            if args.mode == 'latest':
                # A replay can fail even though the capture is fine, and the newest
                # version may itself be a captured failure page; other digests of the
                # same object therefore stay available as later candidates.
                others = [c for c in captures_by_object[canonical_key]
                          if c['digest'] != chosen['digest']][:args.fallback_candidates]
                candidates = same_digest + others
            for position, candidate in enumerate(candidates):
                candidate_rows.append((key, candidate['timestamp'], candidate['original'],
                                       candidate['score'] or 0, position, candidate['digest']))
            created += 1
    with db:
        for start in range(0, len(job_rows), batch):
            db.executemany('''INSERT OR IGNORE INTO download_jobs (job_key, canonical_key, digest,
                    resource_type, priority, state, preferred_timestamp, preferred_original,
                    preferred_score, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)''', job_rows[start:start + batch])
        for start in range(0, len(candidate_rows), batch * 4):
            db.executemany('''INSERT OR IGNORE INTO job_candidates
                (job_key, timestamp, original, score, position, digest)
                VALUES (?, ?, ?, ?, ?, ?)''', candidate_rows[start:start + batch * 4])
    stats = {
        'objects': len(digests_by_object),
        'jobs_created': created,
        'jobs_total': db.execute('SELECT COUNT(*) FROM download_jobs').fetchone()[0],
    }
    LOG.info('Planned jobs: %(jobs_created)d new, %(jobs_total)d total across %(objects)d objects', stats)
    return stats


def apply_object_coverage(db):
    """A logical object that already has *any* verified content needs no replay.

    The planner picks one preferred digest per object, but the merged archives may
    hold a different archived version of the same object. The object is covered
    either way: re-downloading it would only duplicate content we already store.
    """
    with db:
        db.execute('DROP TABLE IF EXISTS temp.object_blob')
        db.execute('''CREATE TEMP TABLE object_blob AS
            SELECT canonical_key, MIN(blob_sha256) AS sha256, COUNT(*) AS files
            FROM captures WHERE blob_sha256 IS NOT NULL AND canonical_key IS NOT NULL
            GROUP BY canonical_key''')
        objects = db.execute('SELECT COUNT(*) FROM object_blob').fetchone()[0]
        if not objects:
            db.execute('DROP TABLE IF EXISTS temp.object_blob')
            return 0
        cursor = db.execute('''UPDATE download_jobs SET state='downloaded',
                blob_sha256=(SELECT sha256 FROM object_blob o
                             WHERE o.canonical_key = download_jobs.canonical_key),
                failure_class=NULL, last_error='covered by another archived version',
                updated_at=?
            WHERE state <> 'downloaded'
              AND canonical_key IN (SELECT canonical_key FROM object_blob)''', (utcnow(),))
        updated = cursor.rowcount
        db.execute('DROP TABLE IF EXISTS temp.object_blob')
    if updated:
        LOG.info('Object coverage: %d jobs satisfied by an already-stored version', updated)
    return updated


def covered_digests(db):
    """digest -> blob sha256 for content already verified locally."""
    return {row['digest']: row['sha256'] for row in db.execute('''
        SELECT c.digest, b.sha256 FROM captures c JOIN blobs b ON b.sha256 = c.blob_sha256
        WHERE c.digest IS NOT NULL AND c.digest <> '' GROUP BY c.digest''')}


def apply_digest_coverage(db):
    """Jobs whose CDX digest already has a verified blob need no replay.

    Written as two set-based statements: a per-digest loop would scan the whole
    capture table once per digest and dominate the runtime.
    """
    with db:
        db.execute('DROP TABLE IF EXISTS temp.digest_blob')
        db.execute('''CREATE TEMP TABLE digest_blob AS
            SELECT digest, MIN(blob_sha256) AS sha256 FROM captures
            WHERE blob_sha256 IS NOT NULL AND digest IS NOT NULL AND digest <> ''
            GROUP BY digest''')
        covered = db.execute('SELECT COUNT(*) FROM digest_blob').fetchone()[0]
        if not covered:
            db.execute('DROP TABLE IF EXISTS temp.digest_blob')
            return 0
        db.execute('''UPDATE captures SET covered_by = (
                SELECT sha256 FROM digest_blob d WHERE d.digest = captures.digest)
            WHERE covered_by IS NULL AND digest IN (SELECT digest FROM digest_blob)''')
        cursor = db.execute('''UPDATE download_jobs SET state='downloaded',
                blob_sha256=(SELECT sha256 FROM digest_blob d WHERE d.digest = download_jobs.digest),
                updated_at=?, failure_class=NULL, last_error=NULL
            WHERE state <> 'downloaded' AND digest IN (SELECT digest FROM digest_blob)''',
            (utcnow(),))
        updated = cursor.rowcount
        db.execute('DROP TABLE IF EXISTS temp.digest_blob')
    if updated:
        LOG.info('Digest coverage: %d jobs satisfied from existing verified blobs', updated)
    return updated


# ---------------------------------------------------------------- download

def validate_replay(capture, body, headers, final_url):
    lower_headers = {key.lower(): value for key, value in headers.items()}
    runtime = lower_headers.get('x-archive-wayback-runtime-error')
    if runtime:
        raise FetchError('Wayback runtime error: ' + runtime, RUNTIME_ERROR)
    parsed = urllib.parse.urlsplit(final_url)
    match = re.fullmatch(r'/web/(\d{14})id_/(.+)', parsed.path)
    if parsed.hostname != 'web.archive.org' or not match:
        raise FetchError(f'unexpected replay location: {final_url}', INVALID_CAPTURE)
    if match.group(1) != capture['timestamp']:
        raise FetchError(f'Wayback substituted a different timestamp: {match.group(1)}', INVALID_CAPTURE)
    replay_original = match.group(2) + ('?' + parsed.query if parsed.query else '')
    try:
        expected = urllib.parse.quote(capture['original'], safe=':/?&=%;+,@!$()*[]~')
        if source_key(replay_original) != source_key(expected):
            raise FetchError('Wayback redirected to a different original URL', INVALID_CAPTURE)
    except ValueError as exc:
        raise FetchError(f'malformed replay URL: {exc}', INVALID_CAPTURE) from exc
    historical_status = lower_headers.get('x-archive-orig-status', '')
    if historical_status and not re.match(r'^200(?:\s|$)', historical_status):
        raise RejectedCapture(f'original response status was {historical_status}', HISTORICAL_FAILURE)
    prefix = body[:131072].decode('utf-8', 'replace').lower()
    content_type = lower_headers.get('content-type', capture['mimetype']).lower()
    looks_html = 'html' in content_type or prefix.lstrip().startswith(('<!doctype html', '<html'))
    if looks_html:
        title = re.search(r'<title\b[^>]*>(.*?)</title\s*>', prefix, re.S)
        title_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', title.group(1))).strip() if title else ''
        archive_brand = title_text in ('wayback machine', 'internet archive', 'wayback machine error',
                                       'internet archive: wayback machine')
        archive_template = any(marker in prefix for marker in (
            'id="error"', "id='error'", 'class="error-container"', 'id="wb-logo"'))
        failure_messages = (
            'wayback machine has not archived that url',
            "wayback machine doesn't have that page archived",
            'this url has been excluded from the wayback machine',
            'this page is not available on the web',
            'cannot be displayed due to robots.txt',
            'the machine that serves this file is down',
            'failed to load the archived',
        )
        if archive_brand and archive_template and any(s in prefix for s in failure_messages):
            raise FetchError('Wayback error template, not original page content', TEMPORARY)
        proxy_titles = {'502 bad gateway', '503 service unavailable', '504 gateway time-out',
                        '504 gateway timeout', 'proxy error'}
        if title_text in proxy_titles and len(body) < 16384:
            if lower_headers.get('memento-datetime'):
                raise RejectedCapture(f'captured short proxy failure page: {title_text}', HISTORICAL_FAILURE)
            raise FetchError(f'proxy failure page without capture provenance: {title_text}', TEMPORARY)
    provenance = lower_headers.get('memento-datetime')
    try:
        captured_at = parsedate_to_datetime(provenance) if provenance else None
        valid_provenance = captured_at is not None and captured_at.tzinfo is not None
    except (TypeError, ValueError, OverflowError):
        valid_provenance = False
    if not valid_provenance:
        raise FetchError('missing/invalid Memento-Datetime; cannot verify archived content', TEMPORARY)
    if not body:
        raise RejectedCapture('empty proven capture body', HISTORICAL_FAILURE)
    return content_type


def local_path(capture):
    """Legacy files/ layout, still used for compatibility checks."""
    key = hashlib.sha256(capture['original'].encode('utf-8')).hexdigest()
    mime = capture['mimetype'].split(';', 1)[0]
    suffix = EXTENSION_BY_MIMETYPE.get(mime, '.bin')
    return Path('files') / key[:2] / key / (capture['timestamp'] + suffix)


def next_retry_delay(attempts):
    return min(6 * 3600, 60 * (2 ** min(attempts, 8)))


def record_failure(db, capture, job, kind, message, retryable):
    now = utcnow()
    attempts = (job['attempts'] or 0) + 1 if job is not None else 1
    next_at = None if not retryable else time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + next_retry_delay(attempts)))
    capture_state = 'filtered' if not retryable else 'error'
    with db:
        db.execute('''UPDATE captures SET state=?, error=?, failure_class=?, retry_count=?,
                last_attempt=?, next_retry_at=? WHERE timestamp=? AND original=?''',
            (capture_state, message, kind, attempts, now, next_at,
             capture['timestamp'], capture['original']))
        if job is not None:
            state = 'failed' if not retryable else 'pending'
            db.execute('''UPDATE download_jobs SET state=?, attempts=?, failure_class=?,
                    last_error=?, last_attempt=?, next_retry_at=?, updated_at=?
                WHERE job_key=?''',
                (state, attempts, kind, message, now, next_at, now, job['job_key']))
        if job is not None:
            db.execute('''UPDATE job_candidates SET tried=1, failure_class=?
                WHERE job_key=? AND timestamp=? AND original=?''',
                (kind, job['job_key'], capture['timestamp'], capture['original']))


def in_window(timestamp, args):
    if getattr(args, 'min_timestamp', None) and timestamp < args.min_timestamp:
        return False
    if getattr(args, 'max_timestamp', None) and timestamp > args.max_timestamp:
        return False
    return True


def job_candidate_list(db, job, args=None):
    rows = db.execute('''SELECT * FROM job_candidates WHERE job_key=?
        ORDER BY tried, position''', (job['job_key'],)).fetchall()
    if args is None:
        return rows
    return [row for row in rows if in_window(row['timestamp'], args)]


def scan_links(db, canonical_key, timestamp, original, body, mimetype):
    """Record relationships found inside downloaded HTML; never fetches anything."""
    if 'html' not in (mimetype or '') or len(body) > 1_500_000:
        return 0
    text = body.decode('utf-8', 'replace')[:400000]
    found = 0
    for match in re.finditer(r'(?:href|src)\s*=\s*["\']([^"\'>\s]+)["\']', text, re.I):
        reference = match.group(1)
        if reference.startswith(('javascript:', 'mailto:', '#', 'data:')):
            continue
        target = urllib.parse.urljoin(original, reference)
        if not target.startswith(('http://', 'https://')):
            continue
        if not in_domain(target, 'xiasl.net'):
            continue
        key, _, _, _, _, _ = classify(target)
        known = db.execute('SELECT 1 FROM urls WHERE original=?', (target,)).fetchone()
        table = 'links' if known else 'unresolved_links'
        with db:
            db.execute(f'''INSERT INTO {table} (source_canonical_key, source_timestamp, target_url,
                    target_canonical_key, link_type, seen_count, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(source_canonical_key, target_url) DO UPDATE SET
                    seen_count = seen_count + 1, last_seen = excluded.last_seen''',
                (canonical_key, timestamp, target, key, 'href', utcnow()))
        found += 1
    return found


class Progress:
    def __init__(self, db, args):
        self.db = db
        self.args = args
        self.started = time.monotonic()
        self.completed = 0
        self.attempts = 0
        self.last_report = self.started

    def totals(self):
        job_states = dict(self.db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state'))
        return {
            'captures': self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0],
            'logical_objects': self.db.execute('SELECT COUNT(*) FROM logical_objects').fetchone()[0],
            'jobs_total': sum(job_states.values()),
            'jobs_done': job_states.get('downloaded', 0),
            'jobs_pending': job_states.get('pending', 0),
            'jobs_failed': job_states.get('failed', 0),
            'blobs': self.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0],
            'bytes': self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM blobs').fetchone()[0],
        }

    def report(self, client=None, force=False):
        now = time.monotonic()
        if not force and now - self.last_report < 60:
            return
        self.last_report = now
        totals = self.totals()
        elapsed = max(1e-6, now - self.started)
        rate = self.completed / elapsed
        remaining = totals['jobs_pending']
        eta = (remaining / rate) if rate > 0 else None
        pacer = client.pacer.snapshot() if client is not None and hasattr(client, 'pacer') else {}
        LOG.info('progress: captures=%(captures)d objects=%(logical_objects)d jobs=%(jobs_total)d '
                 'done=%(jobs_done)d pending=%(jobs_pending)d failed=%(jobs_failed)d blobs=%(blobs)d '
                 'bytes=%(bytes)d', totals)
        LOG.info('runtime: req/min=%.1f delay=%.2fs avg_latency=%.2fs 429=%d 503=%d network=%d '
                 'temporary_failures=%d permanent_failures=%d eta=%s',
                 rate * 60, pacer.get('delay', 0.0), pacer.get('avg_latency', 0.0),
                 pacer.get('rate_limited', 0), pacer.get('temporary', 0), pacer.get('network', 0),
                 self.db.execute("SELECT COUNT(*) FROM download_jobs WHERE failure_class IN "
                                 "('rate_limited','temporary_wayback_error','network_error',"
                                 "'replay_missing','wayback_runtime_error')").fetchone()[0],
                 self.db.execute("SELECT COUNT(*) FROM download_jobs WHERE failure_class IN "
                                 "('historical_failure','invalid_capture','permanent_error')").fetchone()[0],
                 f'{eta / 3600:.1f}h' if eta else 'n/a')


def pending_jobs(db, args, limit=None):
    """Pending jobs in priority order, honouring the timestamp window."""
    where = ["j.state='pending'", '(j.next_retry_at IS NULL OR j.next_retry_at <= ?)']
    params = [utcnow()]
    # A job belongs to a machine's slice when it has at least one capture inside
    # the window; candidates outside it are skipped in execute_job.
    window = ''
    if getattr(args, 'min_timestamp', None):
        window += ' AND k.timestamp >= ?'
        params.append(args.min_timestamp)
    if getattr(args, 'max_timestamp', None):
        window += ' AND k.timestamp <= ?'
        params.append(args.max_timestamp)
    if window:
        where.append(f'EXISTS (SELECT 1 FROM job_candidates k '
                     f'WHERE k.job_key = j.job_key{window})')
    if getattr(args, 'priority_max', None) is not None:
        where.append('j.priority <= ?')
        params.append(args.priority_max)
    if getattr(args, 'resource_type', None):
        kinds = [kind.strip() for kind in args.resource_type.split(',') if kind.strip()]
        where.append(f"j.resource_type IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    sql = ('SELECT j.* FROM download_jobs j WHERE ' + ' AND '.join(where)
           + ' ORDER BY j.priority, j.canonical_key')
    if limit:
        sql += f' LIMIT {int(limit)}'
    return db.execute(sql, params).fetchall()


def execute_job(db, client, args, job, progress):
    """Try the preferred capture, then same-digest fallbacks. Returns 'saved'|'failed'|'retry'."""
    candidates = job_candidate_list(db, job, args)
    if not candidates:
        record_failure(db, {'timestamp': job['preferred_timestamp'], 'original': job['preferred_original']},
                       job, PERMANENT, 'no candidates for job', retryable=False)
        return 'failed'
    attempt_budget = max(1, args.retries + 1)
    last_kind, last_error = None, None
    for candidate in candidates[:attempt_budget]:
        candidate_digest = candidate['digest'] or job['digest']
        capture = {'timestamp': candidate['timestamp'], 'original': candidate['original'],
                   'mimetype': job['resource_type'], 'digest': candidate_digest}
        row = db.execute('''SELECT * FROM captures WHERE timestamp=? AND original=?''',
                         (candidate['timestamp'], candidate['original'])).fetchone()
        if row is not None:
            capture['mimetype'] = row['mimetype']
        replay = REPLAY_BASE + capture['timestamp'] + 'id_/' + urllib.parse.quote(
            capture['original'], safe=':/?&=%;+,@!$()*[]~')
        try:
            progress.attempts += 1
            body, headers, final_url = client.get(replay, args.max_file_bytes)
            content_type = validate_replay(capture, body, headers, final_url)
            sha256 = hashlib.sha256(body).hexdigest()
            relative = blob_relative_path(sha256, content_type or capture['mimetype'])
            atomic_write(args.output / relative, body)
            register_blob(db, args.output, sha256, relative, content_type, len(body),
                          capture['timestamp'], capture['original'])
            with db:
                db.execute('''UPDATE download_jobs SET state='downloaded', blob_sha256=?, blob_path=?,
                        bytes=?, replay_url=?, failure_class=NULL, last_error=NULL,
                        attempts=attempts+1, last_attempt=?, next_retry_at=NULL, updated_at=?
                    WHERE job_key=?''',
                    (sha256, str(relative), len(body), final_url, utcnow(), utcnow(), job['job_key']))
                db.execute('''UPDATE captures SET state='downloaded', path=?, sha256=?, bytes=?,
                        replay_url=?, blob_sha256=?, covered_by=?, error=NULL, failure_class=NULL,
                        last_attempt=?, next_retry_at=NULL WHERE timestamp=? AND original=?''',
                    (str(relative), sha256, len(body), final_url, sha256, sha256, utcnow(),
                     capture['timestamp'], capture['original']))
                db.execute('''UPDATE job_candidates SET tried=1 WHERE job_key=? AND timestamp=? AND original=?''',
                           (job['job_key'], candidate['timestamp'], candidate['original']))
            reuse_blob_for_digest(db, candidate_digest, sha256)
            if 'html' in (content_type or ''):
                scan_links(db, job['canonical_key'], capture['timestamp'], capture['original'],
                           body, content_type)
            LOG.info('Saved job %s <- %s %s (%d bytes)', job['job_key'], capture['timestamp'],
                     capture['original'][:90], len(body))
            progress.completed += 1
            return 'saved'
        except RejectedCapture as exc:
            last_kind, last_error = exc.kind, str(exc)
            record_failure(db, capture, job, exc.kind, str(exc), retryable=False)
            with db:
                db.execute('''UPDATE job_candidates SET tried=1, failure_class=?
                    WHERE job_key=? AND timestamp=? AND original=?''',
                    (exc.kind, job['job_key'], capture['timestamp'], capture['original']))
            LOG.warning('Rejected %s %s: %s', capture['timestamp'], capture['original'][:80], exc)
        except FetchError as exc:
            last_kind, last_error = exc.kind, str(exc)
            retryable = exc.kind not in PERMANENT_CLASSES
            record_failure(db, capture, job, exc.kind, str(exc), retryable=retryable)
            with db:
                db.execute('''UPDATE job_candidates SET tried=1, failure_class=?
                    WHERE job_key=? AND timestamp=? AND original=?''',
                    (exc.kind, job['job_key'], capture['timestamp'], capture['original']))
            LOG.warning('Unavailable %s %s: %s', capture['timestamp'], capture['original'][:80], exc)
            if exc.kind in PERMANENT_CLASSES:
                break
    # Every candidate failed: keep the job retryable only if a temporary class was seen.
    retryable = last_kind in TEMP_CLASSES and last_kind is not None
    with db:
        db.execute('''UPDATE download_jobs SET state=?, failure_class=?, last_error=?,
                updated_at=? WHERE job_key=?''',
            ('pending' if retryable else 'failed', last_kind, last_error, utcnow(), job['job_key']))
    return 'retry' if retryable else 'failed'


def download(db, client, args):
    """Plan (if needed) and execute jobs in priority order."""
    if db.execute('SELECT COUNT(*) FROM captures WHERE canonical_key IS NULL').fetchone()[0]:
        build_layers(db)
    if args.replan or db.execute('SELECT COUNT(*) FROM download_jobs').fetchone()[0] == 0:
        plan_jobs(db, args, replan=args.replan)
    register_existing_files(db, args.output)
    verify_blobs(db, args.output, verify=not args.no_verify_blobs)
    apply_digest_coverage(db)
    apply_object_coverage(db)
    exhaust_jobs(db, args.max_attempts)
    progress = Progress(db, args)
    saved = 0
    while True:
        jobs = pending_jobs(db, args, limit=64)
        if not jobs:
            break
        for job in jobs:
            state = db.execute('SELECT state FROM download_jobs WHERE job_key=?',
                               (job['job_key'],)).fetchone()
            if state is None or state[0] != 'pending':
                continue  # satisfied in the meantime by a shared digest
            if args.max_downloads and progress.attempts >= args.max_downloads:
                LOG.info('Reached --max-downloads (%d replay attempts, %d jobs saved)',
                         progress.attempts, progress.completed)
                progress.report(client, force=True)
                return saved
            outcome = execute_job(db, client, args, job, progress)
            if outcome == 'saved':
                saved += 1
            progress.report(client)
        if args.max_downloads and progress.attempts >= args.max_downloads:
            break
    progress.report(client, force=True)
    return saved


# ------------------------------------------------------------------ reports

def analyze(db, args):
    """Read-only inventory analysis; never touches the replay endpoint."""
    build_layers(db)
    detect_gaps(db)
    scalar = lambda sql, *params: db.execute(sql, params).fetchone()[0]
    report = {
        'generated_at': utcnow(),
        'captures': scalar('SELECT COUNT(*) FROM captures'),
        'unique_urls': scalar('SELECT COUNT(DISTINCT original) FROM captures'),
        'unique_digests': scalar("SELECT COUNT(DISTINCT digest) FROM captures WHERE digest <> ''"),
        'unique_url_digests': scalar("SELECT COUNT(*) FROM (SELECT DISTINCT original, digest FROM captures WHERE digest <> '')"),
        'logical_objects': scalar('SELECT COUNT(*) FROM logical_objects'),
        'unique_object_digests': scalar('SELECT COUNT(*) FROM object_digests'),
        'download_jobs': scalar('SELECT COUNT(*) FROM download_jobs'),
        'urls_per_object_avg': round(scalar('SELECT AVG(url_count) FROM logical_objects') or 0, 3),
        'captures_per_url_avg': round(scalar('SELECT AVG(capture_count) FROM urls') or 0, 3),
        'captures_per_object_avg': round(scalar('SELECT AVG(capture_count) FROM logical_objects') or 0, 3),
        'digest_reuse': {},
        'mimetypes': dict(db.execute('SELECT mimetype, COUNT(*) FROM captures GROUP BY mimetype ORDER BY 2 DESC LIMIT 30')),
        'hosts': dict(db.execute('SELECT host, COUNT(*) FROM urls GROUP BY host ORDER BY 2 DESC LIMIT 30')),
        'schemes': dict(db.execute('SELECT scheme, COUNT(*) FROM urls GROUP BY scheme ORDER BY 2 DESC')),
        'resource_types': dict(db.execute('SELECT resource_type, COUNT(*) FROM urls GROUP BY resource_type ORDER BY 2 DESC')),
        'priority_urls': dict(db.execute('SELECT priority, COUNT(*) FROM urls GROUP BY priority ORDER BY 1')),
        'priority_jobs': dict(db.execute('SELECT priority, COUNT(*) FROM download_jobs GROUP BY priority ORDER BY 1')),
        'object_types': dict(db.execute('SELECT object_type, COUNT(*) FROM logical_objects GROUP BY object_type ORDER BY 2 DESC')),
        'top_urls_by_captures': [dict(row) for row in db.execute(
            'SELECT original, capture_count, digest_count, canonical_key FROM urls ORDER BY capture_count DESC LIMIT 20')],
        'top_objects_by_urls': [dict(row) for row in db.execute(
            'SELECT canonical_key, url_count, capture_count, digest_count FROM logical_objects ORDER BY url_count DESC LIMIT 20')],
        'top_objects_by_digests': [dict(row) for row in db.execute(
            'SELECT canonical_key, digest_count, capture_count FROM logical_objects ORDER BY digest_count DESC LIMIT 20')],
        'sample_urls': [row[0] for row in db.execute('SELECT original FROM urls ORDER BY RANDOM() LIMIT 20')],
        'failures': dict(db.execute("SELECT failure_class, COUNT(*) FROM captures WHERE failure_class IS NOT NULL GROUP BY failure_class ORDER BY 2 DESC")),
        'job_states': dict(db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state')),
        'blobs': scalar('SELECT COUNT(*) FROM blobs'),
        'blob_bytes': scalar('SELECT COALESCE(SUM(bytes),0) FROM blobs'),
        'suspected_gaps': db.execute('SELECT COUNT(*) FROM suspected_gaps').fetchone()[0],
        'unresolved_links': scalar('SELECT COUNT(*) FROM unresolved_links'),
        'internal_links': scalar('SELECT COUNT(*) FROM links'),
    }
    # Digest reuse: how much a single payload can serve.
    total = report['captures'] or 1
    captured = scalar("SELECT COUNT(*) FROM captures WHERE digest <> ''")
    reused = scalar("SELECT COUNT(*) FROM captures WHERE digest IN "
                    "(SELECT digest FROM captures WHERE digest <> '' GROUP BY digest HAVING COUNT(*) > 1)")
    report['digest_reuse'] = {
        'captures_with_digest': captured,
        'captures_in_shared_digest_groups': reused,
        'duplicate_share': round(reused / total, 4),
    }
    report['mimetype_groups'] = {
        'html': scalar("SELECT COUNT(*) FROM captures WHERE mimetype LIKE 'text/html%' OR mimetype IN ('application/xhtml+xml','warc/revisit')"),
        'image': scalar("SELECT COUNT(*) FROM captures WHERE mimetype LIKE 'image/%'"),
        'css': scalar("SELECT COUNT(*) FROM captures WHERE mimetype = 'text/css'"),
        'javascript': scalar("SELECT COUNT(*) FROM captures WHERE mimetype LIKE '%javascript%'"),
        'attachment': scalar("SELECT COUNT(*) FROM urls WHERE resource_type = 'attachment'"),
        'document_archive': scalar("SELECT COUNT(*) FROM urls WHERE resource_type IN ('document','archive')"),
    }
    destination = args.output / 'analysis.json'
    temporary = destination.with_suffix('.json.part')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=False)
    temporary.replace(destination)
    LOG.info('Wrote %s', destination)
    return report


def print_analysis(report):
    print(f"captures                       : {report['captures']}")
    print(f"unique original URLs           : {report['unique_urls']}")
    print(f"unique CDX digests             : {report['unique_digests']}")
    print(f"unique (url, digest)           : {report['unique_url_digests']}")
    print(f"canonical logical objects      : {report['logical_objects']}")
    print(f"unique (object, digest)        : {report['unique_object_digests']}")
    print(f"download jobs                  : {report['download_jobs']}")
    print(f"avg URLs per object            : {report['urls_per_object_avg']}")
    print(f"avg captures per URL           : {report['captures_per_url_avg']}")
    print(f"avg captures per object        : {report['captures_per_object_avg']}")
    print(f"mimetype groups                : {report['mimetype_groups']}")
    print(f"resource types                 : {report['resource_types']}")
    print(f"priority (URLs)                : {report['priority_urls']}")
    print(f"priority (jobs)                : {report['priority_jobs']}")
    print(f"object types                   : {report['object_types']}")
    print(f"digest reuse                   : {report['digest_reuse']}")
    print(f"blobs / bytes                  : {report['blobs']} / {report['blob_bytes']}")
    print(f"suspected gaps / unresolved    : {report['suspected_gaps']} / {report['unresolved_links']}")
    print('hosts                          :')
    for host, count in list(report['hosts'].items())[:12]:
        print(f'    {count:8d}  {host}')
    print('top objects by URL variants    :')
    for row in report['top_objects_by_urls'][:10]:
        print(f'    {row["url_count"]:5d} urls  {row["capture_count"]:6d} captures  {row["canonical_key"][:70]}')
    print('top objects by distinct digests:')
    for row in report['top_objects_by_digests'][:10]:
        print(f'    {row["digest_count"]:4d} digests {row["capture_count"]:6d} captures  {row["canonical_key"][:70]}')
    print('top URLs by capture count      :')
    for row in report['top_urls_by_captures'][:10]:
        print(f'    {row["capture_count"]:5d} captures  {row["original"][:80]}')


def dry_run(db, args):
    """Plan without any network access; compare the old and new request budgets."""
    build_layers(db, replan=args.replan)
    detect_gaps(db)
    plan_jobs(db, args, replan=args.replan)
    register_existing_files(db, args.output)
    verify_blobs(db, args.output, verify=not args.no_verify_blobs)
    covered = apply_digest_coverage(db)
    covered += apply_object_coverage(db)
    clause, params = filter_clause(args)
    captures = db.execute(f'SELECT COUNT(*) FROM captures c WHERE 1=1 {clause}', params).fetchone()[0]
    urls = db.execute(f'SELECT COUNT(DISTINCT c.original) FROM captures c WHERE 1=1 {clause}', params).fetchone()[0]
    objects = db.execute(f'''SELECT COUNT(DISTINCT c.canonical_key) FROM captures c WHERE 1=1 {clause}''', params).fetchone()[0]
    object_digests = db.execute(f'''SELECT COUNT(*) FROM (SELECT DISTINCT c.canonical_key, c.digest
        FROM captures c WHERE 1=1 {clause})''', params).fetchone()[0]
    jobs = db.execute('SELECT COUNT(*) FROM download_jobs').fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM download_jobs WHERE state='downloaded'").fetchone()[0]
    old_latest = urls
    old_all = captures
    new = jobs - done
    summary = {
        'A_raw_captures': captures,
        'B_unique_urls': urls,
        'C_logical_objects': objects,
        'D_unique_object_digests': object_digests,
        'E_planned_replay_jobs': jobs,
        'jobs_already_satisfied': done,
        'jobs_still_to_replay': new,
        'old_algorithm_requests_latest': old_latest,
        'old_algorithm_requests_all': old_all,
        'reduction_vs_latest': round(1 - jobs / old_latest, 4) if old_latest else None,
        'reduction_vs_all': round(1 - jobs / old_all, 4) if old_all else None,
        'reduction_vs_latest_strict': round(1 - new / old_latest, 4) if old_latest else None,
        'reduction_vs_all_strict': round(1 - new / old_all, 4) if old_all else None,
        'priority_job_counts': dict(db.execute('SELECT priority, COUNT(*) FROM download_jobs GROUP BY priority ORDER BY 1')),
        'priority_pending_counts': dict(db.execute(
            "SELECT priority, COUNT(*) FROM download_jobs WHERE state='pending' GROUP BY priority ORDER BY 1")),
        'mode': args.mode,
        'existing_downloaded_captures': db.execute("SELECT COUNT(*) FROM captures WHERE state='downloaded'").fetchone()[0],
        'registered_blobs': db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0],
        'jobs_covered_by_existing_blobs': covered,
        'duplicate_url_candidates': db.execute('''SELECT COUNT(*) FROM (SELECT canonical_key
            FROM logical_objects WHERE url_count > 1)''').fetchone()[0],
    }
    print('A raw captures                :', summary['A_raw_captures'])
    print('B unique original URLs        :', summary['B_unique_urls'])
    print('C canonical logical objects   :', summary['C_logical_objects'])
    print('D unique logical digests      :', summary['D_unique_object_digests'])
    print('E planned replay jobs         :', summary['E_planned_replay_jobs'])
    print('  already satisfied by blobs  :', summary['jobs_already_satisfied'])
    print('  still to replay             :', summary['jobs_still_to_replay'])
    print('old algorithm (latest mode)   :', summary['old_algorithm_requests_latest'])
    print('old algorithm (all mode)      :', summary['old_algorithm_requests_all'])
    print('reduction vs latest           :', f"{summary['reduction_vs_latest']:.1%}" if summary['reduction_vs_latest'] is not None else 'n/a')
    print('reduction vs all              :', f"{summary['reduction_vs_all']:.1%}" if summary['reduction_vs_all'] is not None else 'n/a')
    print('priority job counts           :', summary['priority_job_counts'])
    print('priority pending counts       :', summary['priority_pending_counts'])
    return summary


def scan_downloaded_links(db, output):
    """Parse already-downloaded HTML for in-inventory relations (no requests)."""
    build_layers(db)
    rows = db.execute("""SELECT c.canonical_key, c.timestamp, c.original, c.mimetype, b.path
        FROM captures c JOIN blobs b ON b.sha256 = c.blob_sha256
        WHERE c.state='downloaded' AND c.mimetype LIKE '%html%'""").fetchall()
    scanned = 0
    for row in rows:
        file_path = output / row['path']
        if not file_path.is_file():
            continue
        try:
            body = file_path.read_bytes()
        except OSError:
            continue
        scan_links(db, row['canonical_key'], row['timestamp'], row['original'], body, row['mimetype'])
        scanned += 1
    return scanned


def export_manifests(db, output, mode):
    """Success/failure manifests with full traceability back to the CDX capture."""
    destination = output / 'manifest.jsonl'
    temporary = destination.with_suffix('.jsonl.part')
    rows = db.execute('''SELECT c.original, c.canonical_key, c.object_type, c.object_id, c.page,
            c.timestamp, c.digest, c.length, c.mimetype, c.resource_type, c.priority,
            c.replay_url, c.blob_sha256, c.state, c.path, c.bytes, c.failure_class,
            b.path AS blob_path FROM captures c LEFT JOIN blobs b ON b.sha256 = c.blob_sha256
        WHERE c.state = 'downloaded' ORDER BY c.canonical_key, c.timestamp''')
    last = None
    with temporary.open('w', encoding='utf-8') as stream:
        for row in rows:
            if mode == 'latest' and row['canonical_key'] == last:
                continue
            last = row['canonical_key']
            stream.write(json.dumps({
                'original_url': row['original'], 'canonical_key': row['canonical_key'],
                'object_type': row['object_type'], 'object_id': row['object_id'], 'page': row['page'],
                'timestamp': row['timestamp'], 'cdx_digest': row['digest'],
                'cdx_length': row['length'], 'mimetype': row['mimetype'],
                'resource_type': row['resource_type'], 'priority': row['priority'],
                'replay_url': row['replay_url'], 'blob_sha256': row['blob_sha256'],
                'blob_path': row['blob_path'] or row['path'], 'bytes': row['bytes'],
                'state': row['state'],
            }, ensure_ascii=False) + '\n')
    temporary.replace(destination)
    destination = output / 'failures.jsonl'
    temporary = destination.with_suffix('.jsonl.part')
    rows = db.execute('''SELECT original, canonical_key, object_type, object_id, page, timestamp,
            digest, length, mimetype, resource_type, priority, state, error, failure_class,
            retry_count, next_retry_at, replay_url FROM captures
        WHERE state IN ('filtered', 'error') ORDER BY canonical_key, timestamp''')
    with temporary.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps({
                'original_url': row['original'], 'canonical_key': row['canonical_key'],
                'object_type': row['object_type'], 'object_id': row['object_id'], 'page': row['page'],
                'timestamp': row['timestamp'], 'cdx_digest': row['digest'],
                'cdx_length': row['length'], 'mimetype': row['mimetype'],
                'resource_type': row['resource_type'], 'priority': row['priority'],
                'state': row['state'], 'failure_class': row['failure_class'], 'error': row['error'],
                'retry_count': row['retry_count'], 'next_retry_at': row['next_retry_at'],
                'replay_url': row['replay_url'],
            }, ensure_ascii=False) + '\n')
    temporary.replace(destination)


# ----------------------------------------------------------------------- CLI

def default_output():
    """Android/Termux uses public Downloads; other systems use ~/Downloads."""
    if os.environ.get('TERMUX_VERSION') and Path('/storage/emulated/0').is_dir():
        return Path('/storage/emulated/0/Download/xiasl_archive')
    return Path.home() / 'Downloads' / 'xiasl_archive'


def acquire_output_lock(output):
    """Android shared storage lacks flock; coordinate via a private lock instead."""
    import fcntl
    lock = (output / '.crawler.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except OSError as exc:
        lock.close()
        if exc.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    lock_dir = Path.home() / '.cache' / 'xiasl_wayback' / 'locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(output.resolve()).encode('utf-8')).hexdigest()
    lock = (lock_dir / (key + '.lock')).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


def parser():
    p = argparse.ArgumentParser(description='按 Wayback 已归档内容整理 xiasl.net（分层 inventory + digest 规划，标准库）')
    p.add_argument('--domain', default='xiasl.net', help='包含所有子域名，默认 xiasl.net')
    p.add_argument('--output', type=Path, default=default_output(), help='输出和断点目录；默认公共 Download/xiasl_archive')
    p.add_argument('--mode', choices=('latest', 'all'), default='latest', help='每对象最新有效 digest，或全部历史 digest')
    p.add_argument('--from', dest='from_date', help='起始时间，1–14 位数字')
    p.add_argument('--to', dest='to_date', help='结束时间，1–14 位数字')
    p.add_argument('--html-only', action='store_true', help='索引阶段仅收 HTML/XHTML/revisit')
    p.add_argument('--index-only', action='store_true', help='只获取索引，不下载正文')
    p.add_argument('--skip-index', action='store_true', help='跳过索引扫描，直接规划并下载')
    p.add_argument('--analyze', action='store_true', help='只读分析 inventory，输出 analysis.json（不访问 replay）')
    p.add_argument('--dry-run', action='store_true', help='只做分层与规划，输出请求预算（不访问 replay）')
    p.add_argument('--replan', action='store_true', help='重新生成待下载 job')
    p.add_argument('--fallback-candidates', type=int, default=8, dest='fallback_candidates',
                   help='latest 模式下，同一对象其他 digest 的备用 capture 数上限（默认 8）')
    p.add_argument('--priority-max', type=int, dest='priority_max', help='只处理优先级不大于该值的资源（0–4）')
    p.add_argument('--resource-type', dest='resource_type', help='只处理指定资源类型，逗号分隔，如 thread,attachment')
    p.add_argument('--scan-links', action='store_true', help='分析已下载 HTML 的链接关系（不新增请求）')
    p.add_argument('--refresh-index', action='store_true', help='重新扫描索引，保留已有下载和过滤状态')
    p.add_argument('--min-timestamp', help='只处理不早于该时间戳（14 位数字）')
    p.add_argument('--max-timestamp', help='只处理不晚于该时间戳（14 位数字）')
    p.add_argument('--page-size', type=int, default=1000)
    p.add_argument('--max-pages', type=int, default=0, help='本次最多索引页数；0 为不限')
    p.add_argument('--max-downloads', type=int, default=0, help='本次最多尝试的存档数；0 为不限')
    p.add_argument('--max-attempts', type=int, default=EXHAUST_ATTEMPTS, dest='max_attempts',
                   help='同一 job 重试多少次后判定为永久不可用（默认 6）')
    p.add_argument('--exhaust-only', action='store_true', dest='exhaust_only',
                   help='只做重试耗尽清理，不下载')
    p.add_argument('--delay', type=float, default=1.5, help='请求最小间隔秒数')
    p.add_argument('--max-delay', type=float, default=None, help='adaptive pacing 的上限间隔秒数')
    p.add_argument('--no-adaptive', action='store_true', help='关闭自适应调速，固定使用 --delay')
    p.add_argument('--no-verify-blobs', action='store_true', dest='no_verify_blobs',
                   help='启动时跳过校验本地 blob（默认逐个校验 SHA-256）')
    p.add_argument('--timeout', type=float, default=45)
    p.add_argument('--retries', type=int, default=4)
    p.add_argument('--max-file-bytes', type=int, default=50 * 1024 * 1024)
    p.add_argument('--user-agent', help='可填写带联系方式的 User-Agent')
    p.add_argument('--verbose', action='store_true')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    args.domain = args.domain.lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', args.domain) or '.' not in args.domain:
        p.error('--domain 必须为裸域名，不含协议、路径或通配符')
    for name in ('from_date', 'to_date'):
        if getattr(args, name) and not re.fullmatch(r'\d{1,14}', getattr(args, name)):
            p.error('--from/--to 必须为 1–14 位数字')
    for name in ('min_timestamp', 'max_timestamp'):
        if getattr(args, name) and not re.fullmatch(r'\d{14}', getattr(args, name)):
            p.error('--min-timestamp/--max-timestamp 必须为 14 位数字')
    if args.min_timestamp and args.max_timestamp and args.min_timestamp > args.max_timestamp:
        p.error('--min-timestamp 不能晚于 --max-timestamp')
    if not 1 <= args.page_size <= 10000 or args.timeout <= 0 or args.max_file_bytes < 1:
        p.error('page-size 必须为 1–10000；timeout 和 max-file-bytes 必须为正数')
    if args.priority_max is not None and not 0 <= args.priority_max <= 4:
        p.error('--priority-max 必须在 0–4 之间')
    if args.index_only and args.skip_index:
        p.error('--index-only 与 --skip-index 不能同时使用')
    if any(getattr(args, name) < 0 for name in ('max_pages', 'max_downloads', 'delay', 'retries')):
        p.error('限制值、delay 和 retries 不可为负数')
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    args.output = args.output.expanduser().resolve()
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        lock = acquire_output_lock(args.output)
    except BlockingIOError:
        LOG.error('Another crawler is using %s', args.output)
        return 1
    except OSError as exc:
        LOG.error('无法使用输出目录 %s: %s；Termux 请先运行 termux-setup-storage 并允许存储访问',
                  args.output, exc)
        return 1
    db = open_database(args.output / 'state.sqlite3')
    scope_matches = False
    try:
        scope = json.dumps({'domain': args.domain, 'from': args.from_date, 'to': args.to_date,
                            'html_only': args.html_only}, sort_keys=True)
        previous = get_meta(db, 'scope')
        if previous and previous != scope:
            LOG.error('输出目录已有不同的域名/日期/HTML 范围；请使用新的 --output 目录')
            return 1
        scope_matches = True
        with db:
            set_meta(db, 'scope', scope)
        client = Client(args.delay, args.timeout, args.retries, args.user_agent,
                        max_delay=args.max_delay, adaptive=not args.no_adaptive)
        if args.exhaust_only:
            build_layers(db)
            stopped = exhaust_jobs(db, args.max_attempts)
            LOG.info('Exhaust cleanup: %d jobs marked failed; %d still pending', stopped,
                     db.execute("SELECT COUNT(*) FROM download_jobs WHERE state='pending'").fetchone()[0])
            return 0
        if args.analyze:
            report = analyze(db, args)
            print_analysis(report)
            return 0
        if args.dry_run:
            dry_run(db, args)
            return 0
        if args.scan_links:
            scan_downloaded_links(db, args.output)
            LOG.info('Link scan: %d internal relations, %d unresolved targets',
                     db.execute('SELECT COUNT(*) FROM links').fetchone()[0],
                     db.execute('SELECT COUNT(*) FROM unresolved_links').fetchone()[0])
            return 0
        if args.skip_index:
            if get_meta(db, 'index_complete') != '1':
                LOG.warning('索引尚未标记完成；--skip-index 只下载现有候选，之后请再运行一次索引')
            else:
                LOG.info('Skipping index scan (already complete)')
        else:
            collect_index(db, client, args)
        if get_meta(db, 'index_complete') == '1':
            detect_gaps(db)
        if not args.index_only:
            download(db, client, args)
        return 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted; committed progress retained. Rerun the same command to resume.')
        return 130
    except (FetchError, sqlite3.Error, OSError, UnicodeError) as exc:
        LOG.error('%s; progress retained', exc)
        return 1
    finally:
        try:
            if scope_matches:
                export_manifests(db, args.output, args.mode)
            if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='download_jobs'").fetchone()[0]:
                counts = dict(db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state'))
                LOG.info('Jobs: %s; index_complete=%s; output=%s', counts,
                         get_meta(db, 'index_complete'), args.output)
        finally:
            db.close()
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
