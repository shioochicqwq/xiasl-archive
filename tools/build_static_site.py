#!/usr/bin/env python3
"""Build one browsable static site out of the merged blob archive.

Defaults (as agreed):
  * semantic paths (/t/<tid>/<n>/, /f/<fid>/<n>/, /u/<uid>/, /a/<id>/, /asset/...)
  * latest archived version per logical object
  * CSS/JS/images included so pages keep their original look
  * external links kept, marked with data-external="1"
  * every page carries a provenance footer (original URL, capture time, digest, sha256)

Usage:
    python3 build_static_site.py --archive <dir> --out <dir> [--limit N] [--no-assets]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wayback_xiasl import classify, in_domain  # noqa: E402  (shared canonicalization)

DOMAIN = 'xiasl.net'
HTML_TYPES = ('text/html', 'application/xhtml+xml', 'warc/revisit', 'unk', '')
XML_TYPES = ('application/xml', 'text/xml', 'application/rss+xml', 'application/atom+xml')
CHARSET_BYTES = re.compile(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)
META_CHARSET = re.compile(r'(<meta[^>]+charset\s*=\s*["\']?\s*[A-Za-z0-9_\-]+[^>]*>)', re.I)
ATTR_RE = re.compile(r'(?P<pre>\b(?:href|src|action|background|poster)\s*=\s*)(?P<q>["\']?)'
                     r'(?P<url>[^"\'\s>]+)(?P=q)', re.I)
CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<url>[^\'")]+)(?P=q)\s*\)', re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)


def slug(text, limit=60):
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('_')
    return (cleaned[:limit] or 'x')


def safe_path_component(text, limit=100):
    text = urllib.parse.unquote(text, errors='replace')
    text = re.sub(r'[\x00-\x1f\\:*?"<>|]+', '_', text)
    text = text.strip('. ')
    if not text or text in ('.', '..'):
        text = '_'
    return text[:limit]


def detect_charset(body, content_type=''):
    header = (content_type or '').lower()
    if 'charset=' in header:
        name = header.split('charset=', 1)[1].split(';')[0].strip()
        if name:
            return 'gb18030' if name in ('gb2312', 'gbk', 'gb18030') else name
    match = CHARSET_BYTES.search(body[:4096])
    if match:
        name = match.group(1).decode('ascii', 'replace').lower()
        return 'gb18030' if name in ('gb2312', 'gbk', 'gb18030') else name
    return 'gb18030'  # this site is GB-family unless it says otherwise


def decode_body(body, content_type=''):
    charset = detect_charset(body, content_type)
    try:
        return body.decode(charset, errors='replace'), charset
    except LookupError:
        return body.decode('gb18030', errors='replace'), 'gb18030'


def force_utf8(text):
    if META_CHARSET.search(text):
        return META_CHARSET.sub('<meta charset="utf-8">', text, count=1)
    if re.search(r'<head[^>]*>', text, re.I):
        return re.sub(r'(<head[^>]*>)', r'\1<meta charset="utf-8">', text, count=1, flags=re.I)
    return '<meta charset="utf-8">' + text


def site_path_for(canonical_key, original, mimetype):
    """Map a logical object to its output path, relative to the site root."""
    parsed = urllib.parse.urlsplit(original)
    host = (parsed.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    path = urllib.parse.unquote(parsed.path or '/')
    parts = canonical_key.split(':')
    kind = parts[0]
    page = next((p.split(':', 1)[1] for p in parts if p.startswith('page:')), '')

    if kind == 'thread' and len(parts) > 1:
        return Path('t') / safe_path_component(parts[1]) / (page or '1') / 'index.html'
    if kind == 'forum' and len(parts) > 1:
        return Path('f') / safe_path_component(parts[1]) / (page or '1') / 'index.html'
    if kind == 'user' and len(parts) > 1:
        rest = [safe_path_component(p) for p in parts[2:] if not p.startswith('page:')]
        return Path('u').joinpath(safe_path_component(parts[1]), *rest, page or '1', 'index.html')
    if kind == 'article' and len(parts) > 1:
        variant = parts[2] if len(parts) > 2 else ''
        base = Path('a') / safe_path_component(parts[1])
        return (base / safe_path_component(variant) / 'index.html') if variant else (base / 'index.html')
    if kind in ('tag', 'author'):
        name = parts[1] if len(parts) > 1 else 'x'
        return Path(kind) / slug(safe_path_component(name)) / 'index.html'

    is_page = (mimetype or '').split(';')[0].lower() in HTML_TYPES and kind == 'generic_html'
    root = Path('p') if is_page else Path('asset')
    clean = [safe_path_component(segment) for segment in path.split('/') if segment]
    if not clean:
        clean = ['index']
    target = root.joinpath(host, *clean)
    if path.endswith('/') or not target.suffix:
        target = target / 'index.html'
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode('utf-8', 'replace')).hexdigest()[:8]
        target = target.parent / f'{target.stem}__q{digest}{target.suffix}'
    return target


def resolve_directory_conflicts(paths):
    """A path cannot be both a file and a directory ancestor of another page.

    For example an archived `.../tszrbbs/index.php` file and pages under
    `.../tszrbbs/index.php/archiver/...`. Children of such a path get an extra
    `__dir` segment so both can coexist on a filesystem.
    """
    paths = dict(paths)
    for _ in range(8):  # nested conflicts resolve in a few passes
        files = {str(path) for path in paths.values()}
        changed = False
        for key, path in paths.items():
            parts = list(path.parts)
            for index in range(1, len(parts)):
                ancestor = str(Path(*parts[:index]))
                if ancestor in files and ancestor != str(path):
                    parts[index - 1] = parts[index - 1] + '__dir'
                    paths[key] = Path(*parts)
                    changed = True
                    break
        if not changed:
            break
    return paths


def load_forum_threads(db):
    """forum fid -> thread ids, derived from links found inside archived forum pages."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'links' not in tables:
        return {}
    forums = {}
    try:
        rows = db.execute("""SELECT source_canonical_key, target_canonical_key FROM links
            WHERE source_canonical_key LIKE 'forum:%' AND target_canonical_key LIKE 'thread:%'""")
    except sqlite3.Error:
        return {}
    for source, target in rows:
        fid = source.split(':')[1].split(':')[0]
        tid = target.split(':')[1].split(':')[0]
        forums.setdefault(fid, set()).add(tid)
    return {fid: sorted(threads) for fid, threads in forums.items()}


def load_objects(db):
    """Latest downloaded capture per logical object, with its blob."""
    rows = db.execute('''
        SELECT c.canonical_key, c.object_type, c.resource_type, c.priority, c.timestamp,
               c.original, c.digest, c.mimetype, c.blob_sha256, c.path, c.bytes, b.path AS blob_path
        FROM captures c LEFT JOIN blobs b ON b.sha256 = c.blob_sha256
        WHERE c.state = 'downloaded' AND c.canonical_key IS NOT NULL
        ORDER BY c.canonical_key, c.timestamp DESC''').fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row['canonical_key'], row)
    return latest


def build_lookup(latest, paths=None):
    """Two maps for link resolution: exact URL variants and canonical keys."""
    exact, canonical = {}, {}
    for key, row in latest.items():
        path = (paths or {}).get(key) or site_path_for(key, row['original'], row['mimetype'])
        target = '/' + str(path).replace('index.html', '')
        canonical[key] = target
        parsed = urllib.parse.urlsplit(row['original'])
        bare_host = (parsed.hostname or '').lower()
        hosts = {parsed.netloc.lower(), bare_host, bare_host[4:] if bare_host.startswith('www.') else 'www.' + bare_host}
        for scheme in ('http', 'https'):
            for host in hosts:
                if not host:
                    continue
                for netloc in {host, host.replace(':80', '')}:
                    url = f'{scheme}://{netloc}{parsed.path or "/"}'
                    if parsed.query:
                        url += f'?{parsed.query}'
                    exact.setdefault(url, target)
    return exact, canonical


def resolve_target(target, base_original, lookup, stats):
    """Return (kind, value, reason, canonical_key) for a link inside archived HTML."""
    exact, canonical = lookup
    absolute = urllib.parse.urljoin(base_original, target)
    parsed = urllib.parse.urlsplit(absolute)
    if parsed.scheme not in ('http', 'https'):
        stats['skipped_scheme'] += 1
        return None, None, 'scheme', None
    if not in_domain(absolute, DOMAIN):
        stats['external'] += 1
        return 'external', absolute, 'external', None
    cleaned = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
    key, _, _, _, _, _ = classify(cleaned)
    hit = exact.get(cleaned)
    if hit is None:
        for candidate in (cleaned.replace('https://', 'http://'), cleaned.replace('http://', 'https://'),
                          cleaned.replace(':80', '')):
            hit = exact.get(candidate)
            if hit:
                break
    if hit is not None:
        stats['resolved_exact'] += 1
        return 'page', hit, 'exact', key
    hit = canonical.get(key)
    if hit is not None:
        stats['resolved_canonical'] += 1
        return 'page', hit, 'canonical', key
    stats['unresolved'] += 1
    reason = 'gap' if key in stats.setdefault('gaps', set()) else (
        'not_fetched' if cleaned in stats.setdefault('known_urls', set()) else 'unresolved')
    return 'missing', cleaned, reason, key


def rewrite_links(text, base_original, lookup, stats, css=False, source_key=None):
    pattern = CSS_URL_RE if css else ATTR_RE
    # Forum pages are also the place where the forum -> thread relation is visible,
    # so the mapping is collected here instead of scanning the archive again.
    record = stats.setdefault('forum_threads', {}) if (source_key or '').startswith('forum:') else None
    fid = source_key.split(':')[1] if record is not None else None

    def replace(match):
        target = match.group('url')
        if target.startswith(('data:', 'javascript:', 'mailto:', '#')):
            stats['skipped_inline'] += 1
            return match.group(0)
        if target.startswith('//'):
            stats['external'] += 1
            return match.group(0)
        kind, value, reason, target_key = resolve_target(target, base_original, lookup, stats)
        if kind is None:
            return match.group(0)
        if record is not None and (target_key or '').startswith('thread:'):
            record.setdefault(fid, set()).add(target_key.split(':')[1])
        if kind == 'external':
            return f'url("{value}")' if css else (
                f'{match.group("pre")}"{value}" data-external="1" rel="noopener"')
        if kind == 'page':
            return f'url("{value}")' if css else f'{match.group("pre")}"{value}"'
        missing = f'/missing/?url={urllib.parse.quote(value, safe="")}&reason={reason}'
        return f'url("{missing}")' if css else f'{match.group("pre")}"{missing}"'

    return pattern.sub(replace, text)


FOOTER_RE = re.compile(r'<div id="archive-footer".*?</div>\s*(?=</body>)', re.S | re.I)
GATE_RE = re.compile(r'您需要登录|请先登录|需要登录才能|阅读权限|没有权限|抱歉，本帖要求|提示信息|请登录后|登录后才能')
ERROR_RE = re.compile(r'page cannot be displayed|service unavailable|bad gateway|gateway time-?out|'
                      r'指定的主题不存在|已被删除或正在被审核|该用户已被删除|本帖已经被删除|'
                      r'无法找到该页|无法显示此页|HTTP 错误 \d{3}', re.I)
UTILITY_KEYS = ('gethits', 'advcache', 'relatekw', 'relatethread', 'xajax', 'batch.panel',
                'sendmail', 'comment.asp?action=js', '/ad/', 'do.php')
VERDICT_LABELS = {'login_gate': '仅登录/权限页', 'error': '错误页', 'empty': '空页',
                  'utility': '统计/工具页', 'no_content': '无正文', 'ok': ''}


def visible_text(text):
    """Rough visible text of a page, without scripts, styles or the injected footer."""
    text = FOOTER_RE.sub('', text)
    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', text, flags=re.S | re.I)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body)).strip()


def classify_quality(key, resource_type, text):
    """Decide whether a capture actually holds the page's content.

    Many captures are login gates, ISP/proxy error pages or one-line stubs: the
    archive only ever recorded that, so the page is flagged rather than presented
    as if it were the real page.
    """
    kind = key.split(':')[0]
    lowered = text.lower()
    head = visible_text(text)[:1500]
    if ERROR_RE.search(head):
        return 'error', '当年抓到的就是错误页'
    if kind in ('thread', 'thread_page'):
        has_content = ('id="postlist"' in lowered or 'class="t_f"' in lowered
                       or 'id="post_' in lowered)
    elif kind == 'forum':
        has_content = ('threadlisttableid' in lowered or 'class="datatable"' in lowered
                       or bool(re.search(r'thread-\d+-\d+-\d+\.html|viewthread\.php\?tid=', text)))
    elif kind == 'user':
        has_content = len(head) > 600
    elif kind == 'article':
        has_content = len(head) > 500
    else:
        has_content = len(head) > 250
    if GATE_RE.search(head[:500]) and not has_content:
        return 'login_gate', '当年该页需要登录/权限，档案库只抓到登录提示'
    if any(marker in key.lower() for marker in UTILITY_KEYS) or resource_type in ('tracker',):
        return 'utility', '统计/工具类页面，本身没有正文'
    if len(head) < 250:
        return 'empty', '正文近乎为空（当年即如此）'
    if not has_content:
        return 'no_content', '页面没有正文块，可能当年只抓到提示或框架'
    return 'ok', ''


NAV_TEMPLATE = '''
 <div id="archive-nav" style="margin:0 0 12px;padding:8px 12px;background:#eef4ff;border:1px solid #c8d8f0;
   font:13px/1.7 system-ui,-apple-system,sans-serif">
   {content}
 </div>
 '''

BANNER_TEMPLATE = '''
 <div id="archive-banner" data-verdict="{marker}" style="margin:0 0 12px;padding:12px 14px;background:#fff4e5;
   border-left:5px solid #e8a33d;font:14px/1.7 system-ui,-apple-system,sans-serif;color:#5a3c00">
   <b>⚠ 这一页没有真正的正文内容</b><br>
   原因：{reason}。本站已把这类页面标记出来，避免被误当成正文。
   <div style="margin-top:6px">
     <a href="{replay}" data-external="1" rel="noopener">查看 Wayback 原始快照</a>
     · <a href="{wayback_all}" data-external="1" rel="noopener">查看该网址的所有存档时间</a>
     {links}
   </div>
 </div>
 '''


def family_of(key):
    """Group the paginated pages of one thread/forum/user/article together."""
    parts = key.split(':')
    kind = parts[0]
    if kind in ('thread', 'forum', 'user', 'article') and len(parts) > 1:
        return kind, parts[1]
    return None, None


def build_families(latest, paths):
    """family -> {page -> site path} so paginated pages can link to each other."""
    families = {}
    for key in latest:
        kind, ident = family_of(key)
        if kind is None:
            continue
        parts = key.split(':')
        page = next((p.split(':', 1)[1] for p in parts if p.startswith('page:')), '')
        variant = parts[2] if kind == 'article' and len(parts) > 2 else ''
        label = page or variant or '1'
        families.setdefault((kind, ident), {})[label] = '/' + str(paths[key]).replace('index.html', '')
    return families


FOOTER_TEMPLATE = '''
<div id="archive-footer" style="margin:2em 0 1em;padding:10px 12px;border-top:1px solid #ccc;
  font:12px/1.6 system-ui,-apple-system,sans-serif;color:#555;background:#fafafa">
  <b>Internet Archive 存档副本</b> ·
  原网址 <a href="{original}" data-external="1" rel="noopener">{original_short}</a> ·
  抓取于 {timestamp} ·
  CDX digest <code>{digest}</code> ·
  SHA-256 <code>{sha256_short}</code> ·
  <a href="{replay}" data-external="1" rel="noopener">查看 Wayback 原始快照</a> ·
  <a href="/inventory/">全部已归档页面</a>
</div>
'''


def inject_footer(text, row, verdict='ok', reason='', navigation=None, banner_links=''):
    footer = FOOTER_TEMPLATE.format(
        original=row['original'], original_short=row['original'][:90],
        timestamp=(f"{row['timestamp'][:4]}-{row['timestamp'][4:6]}-{row['timestamp'][6:8]} "
                   f"{row['timestamp'][8:10]}:{row['timestamp'][10:12]}"),
        digest=row['digest'] or '-', sha256_short=(row['blob_sha256'] or '')[:12],
        replay=f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}")
    prefix = ''
    if verdict not in ('ok', ''):
        prefix += BANNER_TEMPLATE.format(
            marker=verdict, reason=reason or VERDICT_LABELS.get(verdict, verdict),
            replay=f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}",
            wayback_all=f"https://web.archive.org/web/*/{row['original']}",
            links=banner_links)
    if navigation:
        prefix += NAV_TEMPLATE.format(content=navigation)
    if prefix:
        if re.search(r'<body[^>]*>', text, re.I):
            text = re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + prefix, text, count=1, flags=re.I)
        else:
            text = prefix + text
    if re.search(r'</body\s*>', text, re.I):
        return re.sub(r'</body\s*>', footer + '</body>', text, count=1, flags=re.I)
    return text + footer


SKIP_EXISTING = False


def write_quality_table(db_path, quality):
    """Persist per-page verdicts so later runs and other tools can reuse them."""
    if not quality:
        return 0
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS page_quality (
                canonical_key TEXT PRIMARY KEY, verdict TEXT NOT NULL, reason TEXT,
                text_chars INTEGER, detected_at TEXT)''')
            stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            db.executemany('INSERT OR REPLACE INTO page_quality VALUES (?, ?, ?, ?, ?)',
                           [(key, verdict, reason, chars, stamp)
                            for key, (verdict, reason, chars) in quality.items()])
        return len(quality)
    except sqlite3.Error:
        return 0
    finally:
        db.close()


def write_bytes(destination, body):
    if SKIP_EXISTING and destination.is_file() and destination.stat().st_size == len(body):
        return len(body)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return len(body)


def main():
    parser = argparse.ArgumentParser(description='把合并后的存档构建成静态网站')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 个对象（试跑用）')
    parser.add_argument('--no-assets', action='store_true')
    parser.add_argument('--max-html-bytes', type=int, default=3_000_000)
    parser.add_argument('--skip-existing', action='store_true',
                        help='已生成过的文件不重复写入（断点续建）')
    args = parser.parse_args()

    global SKIP_EXISTING
    SKIP_EXISTING = args.skip_existing
    archive, out = Path(args.archive), Path(args.out)
    started = time.monotonic()
    db = sqlite3.connect(f'file:{archive / "state.sqlite3"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    latest = load_objects(db)
    if args.limit:
        latest = dict(list(latest.items())[:args.limit])
    # Compute every output path first so file/directory conflicts can be resolved
    # before any link is rewritten against those paths.
    paths = resolve_directory_conflicts(
        {key: site_path_for(key, row['original'], row['mimetype']) for key, row in latest.items()})
    lookup = build_lookup({key: row for key, row in latest.items()}, paths)
    quality = {}
    families = build_families(latest, paths)
    stats = {'objects': len(latest), 'pages': 0, 'assets': 0, 'missing_files': 0, 'too_big': 0,
             'quality': {}, 'thread_titles': {},
             'bytes': 0, 'resolved_exact': 0, 'resolved_canonical': 0, 'unresolved': 0,
             'external': 0, 'skipped_inline': 0, 'skipped_scheme': 0, 'collisions': 0,
             'encoding': {}, 'index': [], 'used_paths': {},
             'gaps': {row[0] for row in db.execute('SELECT canonical_key FROM suspected_gaps')},
             'known_urls': {row[0] for row in db.execute('SELECT original FROM captures')}}

    def disambiguate(relative, sha256):
        owner = stats['used_paths'].get(str(relative))
        if owner is None:
            stats['used_paths'][str(relative)] = sha256
            return relative
        if owner == sha256:
            return None  # identical content already written
        stats['collisions'] += 1
        return relative.with_name(f'{relative.stem}__{sha256[:8]}{relative.suffix}')

    for key, row in latest.items():
        blob = row['blob_path'] or row['path']
        if not blob or not (archive / blob).is_file():
            stats['missing_files'] += 1
            continue
        source = archive / blob
        relative = disambiguate(paths.get(key) or site_path_for(key, row['original'], row['mimetype']),
                                row['blob_sha256'] or '')
        if relative is None:
            continue
        destination = out / relative
        mimetype = (row['mimetype'] or '').split(';')[0].lower()
        if mimetype in HTML_TYPES:
            body = source.read_bytes()
            if len(body) > args.max_html_bytes:
                stats['too_big'] += 1
                stats['bytes'] += write_bytes(destination, body)
                stats['assets'] += 1
                continue
            text, charset = decode_body(body, row['mimetype'])
            stats['encoding'][charset] = stats['encoding'].get(charset, 0) + 1
            verdict, reason = classify_quality(key, row['resource_type'] or '', text)
            stats['quality'][verdict] = stats['quality'].get(verdict, 0) + 1
            quality[key] = (verdict, reason, len(visible_text(text)))
            text = rewrite_links(text, row['original'], lookup, stats, source_key=key)
            navigation, banner_links = '', ''
            kind, ident = family_of(key)
            if kind is not None:
                members = families.get((kind, ident), {})
                other = {page: url for page, url in members.items()
                         if url != '/' + str(relative).replace('index.html', '')}
                if len(other) > 1:
                    links = ' · '.join(
                        f'<a href="{url}">第 {page} 页</a>' if kind != 'article'
                        else f'<a href="{url}">{page or "正文"}</a>'
                        for page, url in sorted(other.items(), key=lambda kv: (len(kv[0]), kv[0])))
                    label = {'thread': f'《帖子 {ident}》', 'forum': f'《版块 {ident}》',
                             'user': f'《用户 {ident}》', 'article': f'《文章 {ident}》'}[kind]
                    index_link = '/forums/' if kind in ('thread', 'forum') else (
                        f'/{"u" if kind == "user" else "a"}/')
                    navigation = (f'{label}已归档的页面：{links} · '
                                  f'<a href="{index_link}">返回索引</a>')
                elif kind in ('thread', 'forum') or members:
                    navigation = ('<a href="/forums/">按版块浏览</a> · '
                                  '<a href="/inventory/">全部页面</a>')
            if verdict not in ('ok', ''):
                banner_links = (f' · <a href="/{"forums" if kind in ("thread", "forum") else "inventory"}/">'
                                f'查看已归档的其它页面</a>')
            text = force_utf8(inject_footer(text, row, verdict, reason, navigation, banner_links))
            payload = text.encode('utf-8', 'replace')
            stats['bytes'] += write_bytes(destination, payload)
            stats['pages'] += 1
            title = TITLE_RE.search(text)
            clean_title = re.sub(r'\s+', ' ', title.group(1)).strip()[:120] if title else ''
            if verdict not in ('ok', ''):
                clean_title = f'{clean_title} 〔{VERDICT_LABELS.get(verdict, verdict)}〕'.strip()
            stats['index'].append({
                'k': key, 'p': '/' + str(relative).replace('index.html', ''),
                't': clean_title, 'd': row['timestamp'], 'b': len(payload), 'q': verdict})
            if kind == 'thread':
                forum_name = re.search(r'[『【\[]([^』】\]]{2,20})[』】\]]', clean_title)
                stats['thread_titles'][ident] = (clean_title, forum_name.group(1) if forum_name else '')
        elif mimetype in XML_TYPES:
            # RSS/XML feeds keep their markup but their links must still resolve.
            text, charset = decode_body(source.read_bytes(), row['mimetype'])
            stats['encoding'][charset] = stats['encoding'].get(charset, 0) + 1
            text = rewrite_links(text, row['original'], lookup, stats)
            stats['bytes'] += write_bytes(destination, force_utf8(text).encode('utf-8', 'replace'))
            stats['assets'] += 1
        elif mimetype == 'text/css':
            text, charset = decode_body(source.read_bytes(), row['mimetype'])
            stats['encoding'][charset] = stats['encoding'].get(charset, 0) + 1
            text = rewrite_links(text, row['original'], lookup, stats, css=True)
            stats['bytes'] += write_bytes(destination, text.encode('utf-8', 'replace'))
            stats['assets'] += 1
        else:
            if args.no_assets:
                continue
            stats['bytes'] += write_bytes(destination, source.read_bytes())
            stats['assets'] += 1

    out.mkdir(parents=True, exist_ok=True)
    homepage = out / 'p' / 'xiasl.net' / 'index.html'
    if homepage.is_file():
        shutil.copyfile(homepage, out / 'index.html')
    if not (out / 'index.html').is_file():
        (out / 'index.html').write_text(INDEX_TEMPLATE.replace('{{TITLE}}', 'xiasl.net 存档'),
                                        encoding='utf-8')
    (out / 'missing').mkdir(parents=True, exist_ok=True)
    (out / 'missing' / 'index.html').write_text(MISSING_TEMPLATE, encoding='utf-8')
    (out / '404.html').write_text(MISSING_TEMPLATE, encoding='utf-8')
    (out / 'search').mkdir(parents=True, exist_ok=True)
    (out / 'search' / 'index.json').write_text(
        json.dumps(stats['index'], ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    for name, title, prefix in (('t', '帖子', 'thread:'), ('f', '版块', 'forum:'),
                                ('u', '用户', 'user:'), ('a', '文章', 'article:')):
        directory = out / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'index.html').write_text(
            INVENTORY_TEMPLATE.replace('{{TITLE}}', f'全部已归档{title}')
                              .replace('{{PREFIX}}', prefix), encoding='utf-8')
    observed = {fid: set(threads) for fid, threads in stats.get('forum_threads', {}).items()}
    for fid, threads in load_forum_threads(db).items():
        observed.setdefault(fid, set()).update(threads)
    forum_threads = {fid: sorted(threads) for fid, threads in observed.items()}
    titles = {entry['k']: entry['t'] for entry in stats['index']}
    (out / 'search').mkdir(parents=True, exist_ok=True)
    (out / 'search' / 'forums.json').write_text(json.dumps(
        {fid: [[tid, titles.get(f'thread:{tid}:page:1', ''), f'/t/{tid}/1/'] for tid in threads]
         for fid, threads in forum_threads.items()}, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8')
    (out / 'forums').mkdir(parents=True, exist_ok=True)
    (out / 'forums' / 'index.html').write_text(
        FORUMS_TEMPLATE.replace('{{TITLE}}', '全部版块'), encoding='utf-8')
    for fid, threads in forum_threads.items():
        directory = out / 'f' / safe_path_component(fid)
        directory.mkdir(parents=True, exist_ok=True)
        page = directory / 'index.html'
        if not page.is_file():
            page.write_text(FORUM_TEMPLATE.replace('{{FID}}', fid)
                            .replace('{{TITLE}}', f'版块 {fid} 的帖子'), encoding='utf-8')

    # Forum index by *name*, recovered from thread titles: most archived forum
    # list pages are login gates, so their own thread links are unusable.
    by_name = {}
    for tid, (title, forum_name) in stats['thread_titles'].items():
        if not forum_name:
            continue
        verdict = quality.get(f'thread:{tid}:page:1', ('ok', '', 0))[0]
        by_name.setdefault(forum_name, []).append((tid, title, verdict))
    (out / 'forums').mkdir(parents=True, exist_ok=True)
    name_rows = []
    for name, threads in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        slug_name = slug(name)
        target = out / 'forums' / slug_name
        target.mkdir(parents=True, exist_ok=True)
        rows = ''.join(
            f'<li><a href="/t/{tid}/1/">{title or ("帖子 " + tid)}</a>'
            + (f' <span class="bad">{VERDICT_LABELS.get(verdict, verdict)}</span>' if verdict != "ok" else "")
            + f' <span class="n">tid={tid}</span></li>'
            for tid, title, verdict in sorted(threads, key=lambda item: -len(item[1])))
        (target / 'index.html').write_text(
            FORUM_NAME_TEMPLATE.replace('{{TITLE}}', f'『{name}』的已归档帖子')
                               .replace('{{COUNT}}', str(len(threads))).replace('{{ROWS}}', rows),
            encoding='utf-8')
        name_rows.append((name, len(threads), f'/forums/{slug_name}/'))
    (out / 'search' / 'forums_by_name.json').write_text(json.dumps(
        [[name, count, url] for name, count, url in name_rows], ensure_ascii=False,
        separators=(',', ':')), encoding='utf-8')
    (out / 'forums' / 'index.html').write_text(
        FORUMS_TEMPLATE.replace('{{ROWS}}', ''.join(
            f'<li><a href="{url}">『{name}』</a> <span class="n">{count} 个帖子</span></li>'
            for name, count, url in name_rows)), encoding='utf-8')

    (out / 'inventory').mkdir(parents=True, exist_ok=True)
    (out / 'inventory' / 'index.html').write_text(
        INVENTORY_TEMPLATE.replace('{{TITLE}}', '全部已归档页面').replace('{{PREFIX}}', ''),
        encoding='utf-8')

    write_quality_table(archive / 'state.sqlite3', quality)
    report = {k: v for k, v in stats.items()
              if k not in ('index', 'used_paths', 'gaps', 'known_urls', 'forum_threads',
                           'thread_titles')}
    report['invalid_pages'] = sum(count for verdict, count in stats['quality'].items() if verdict != 'ok')
    report['threads_with_title'] = len(stats['thread_titles'])
    report['forums_with_threads'] = len(forum_threads)
    report['threads_linked_from_forums'] = sum(len(v) for v in forum_threads.values())
    report['gap_objects'] = len(stats['gaps'])
    report['known_urls'] = len(stats['known_urls'])
    report['seconds'] = round(time.monotonic() - started, 1)
    (out / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    db.close()
    return 0


FORUM_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>{{TITLE}}</title><style>body{font:15px/1.7 system-ui,sans-serif;margin:24px}
a{color:#0645ad;text-decoration:none}.n{color:#888;font-size:12px}</style></head><body>
<h1>{{TITLE}}</h1>
<p class="n" id="count"></p>
<ul id="list"></ul>
<p><a href="/forums/">全部版块</a> · <a href="/inventory/">全部已归档页面</a> · <a href="/">首页</a></p>
<script>
const FID = '{{FID}}';
fetch('/search/forums.json').then(r => r.json()).then(all => {
  const threads = all[FID] || [];
  document.getElementById('count').textContent = threads.length + ' 个已归档帖子';
  document.getElementById('list').innerHTML = threads.map(t =>
    `<li><a href="${t[2]}">${t[1] || ('帖子 ' + t[0])}</a> <span class="n">tid=${t[0]}</span></li>`).join('');
});
</script></body></html>'''

FORUMS_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>全部版块</title><style>body{font:15px/1.7 system-ui,sans-serif;margin:24px}
a{color:#0645ad;text-decoration:none}.n{color:#888;font-size:12px}
ul{line-height:1.9}</style></head><body>
<h1>全部版块（按帖子标题里的版块名归类）</h1>
<p class="n">大多数版块列表页当年只抓到登录提示，所以这里改用帖子标题里出现的版块名来归类。</p>
<ul>{{ROWS}}</ul>
<p><a href="/inventory/">全部已归档页面</a> · <a href="/">首页</a></p>
</body></html>'''

FORUM_NAME_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>{{TITLE}}</title><style>body{font:15px/1.7 system-ui,sans-serif;margin:24px}
a{color:#0645ad;text-decoration:none}.n{color:#888;font-size:12px}
.bad{color:#b06a00;font-size:12px;border:1px solid #e8a33d;border-radius:3px;padding:0 4px}
ul{line-height:1.9}</style></head><body>
<h1>{{TITLE}}</h1>
<p class="n">{{COUNT}} 个已归档帖子；带标记的表示当年只抓到登录页/错误页/空页</p>
<ul>{{ROWS}}</ul>
<p><a href="/forums/">全部版块</a> · <a href="/inventory/">全部已归档页面</a> · <a href="/">首页</a></p>
</body></html>'''

INDEX_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>{{TITLE}}</title><style>body{font:15px/1.7 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}
a{color:#0645ad}code{background:#f2f2f2;padding:1px 4px}</style></head><body>
<h1>xiasl.net 存档站</h1>
<p>这是从 Internet Archive Wayback Machine 整理出的 xiasl.net 离线存档。</p>
<ul>
<li><a href="/inventory/">全部已归档页面</a>（可搜索的清单）</li>
<li><a href="/forums/">按版块浏览帖子</a></li>
<li><a href="/t/">帖子</a> · <a href="/f/">版块页</a> · <a href="/u/">用户</a> · <a href="/a/">文章</a></li>
</ul>
<p>未归档的页面会指向 <a href="/missing/">缺页说明页</a>。</p>
</body></html>'''

MISSING_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>该页面未被归档</title><style>body{font:15px/1.7 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px}
code{background:#f2f2f2;padding:1px 4px}.box{background:#fff8e1;border-left:4px solid #f0ad4e;padding:12px 14px;margin:16px 0}</style>
</head><body>
<h1>这一页没有被归档</h1>
<div class="box">
<p>原始网址：<code id="u">（未提供）</code></p>
<p>原因：<span id="r">未知</span></p>
</div>
<p id="explain"></p>
<p><a id="wayback" href="https://web.archive.org/" data-external="1" rel="noopener">在 Wayback Machine 上按原始网址查询</a></p>
<p><a href="/inventory/">返回全部已归档页面</a> · <a href="/">返回首页</a></p>
<script>
const q = new URLSearchParams(location.search);
const url = q.get('url') || '';
const reason = q.get('reason') || 'unknown';
document.getElementById('u').textContent = url || '（未提供）';
const reasons = {
  unresolved: '档案库里没有这一页（Internet Archive 未收录，或回放不可用）',
  gap: '该帖子的翻页序列缺了这一页，档案库没有收录',
  not_fetched: '索引里有记录，但没有成功下载到内容',
  external: '这是指向站外的链接'
};
document.getElementById('r').textContent = reasons[reason] || reason;
document.getElementById('explain').textContent =
  '本页只是说明页，不包含任何伪造内容。如果这一页在档案库中确实存在，可用下面的链接自行查看。';
document.getElementById('wayback').href = url
  ? 'https://web.archive.org/web/*/' + encodeURIComponent(url) : 'https://web.archive.org/';
</script></body></html>'''

INVENTORY_TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8">
<title>{{TITLE}}</title><style>body{font:15px/1.7 system-ui,sans-serif;margin:24px}
input{font-size:16px;padding:6px 10px;width:min(560px,90vw)}
table{border-collapse:collapse;width:100%;margin-top:14px}
td{padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top}
a{color:#0645ad;text-decoration:none}.n{color:#888;font-size:12px}</style></head><body>
<h1>全部已归档页面 <span class="n" id="count"></span></h1>
<p><input id="q" placeholder="搜索标题、canonical key 或日期（如 thread:10128、2005、forum）" autofocus>
<label class="n" style="margin-left:12px"><input type="checkbox" id="valid"> 只看有正文的页面</label></p>
<p class="n" id="verdicts"></p>
<table><tbody id="rows"></tbody></table>
<script>
let data = [];
const rows = document.getElementById('rows');
const count = document.getElementById('count');
const LABELS = {login_gate:'仅登录页', error:'错误页', empty:'空页', utility:'工具页', no_content:'无正文'};
function render(items) {
  rows.innerHTML = items.map(it => {
    const bad = it.q && it.q !== 'ok';
    const tag = bad ? ` <span style="color:#b06a00;border:1px solid #e8a33d;border-radius:3px;padding:0 4px;font-size:12px">${LABELS[it.q] || it.q}</span>` : '';
    return `<tr><td><a href="${it.p}">${it.t || it.k}</a>${tag}
    <div class="n">${it.k} · ${it.d.slice(0,8)} · ${(it.b/1024).toFixed(0)} KB</div></td></tr>`;
  }).join('');
}
const PREFIX = '{{PREFIX}}';
fetch('/search/index.json').then(r => r.json()).then(all => {
  data = PREFIX ? all.filter(it => it.k.startsWith(PREFIX)) : all;
  count.textContent = data.length + ' 页'; render(data.slice(0, 300)); summarise();
});
function apply() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const validOnly = document.getElementById('valid').checked;
  let hits = data;
  if (validOnly) hits = hits.filter(it => !it.q || it.q === 'ok');
  if (q) hits = hits.filter(it => (it.t + ' ' + it.k + ' ' + it.d).toLowerCase().includes(q));
  render(hits.slice(0, 300));
  count.textContent = hits.length + ((q || validOnly) ? ' 个匹配 / 共 ' : ' / ') + data.length + ' 页';
}
function summarise() {
  const tally = {};
  data.forEach(it => { const v = it.q || 'ok'; tally[v] = (tally[v] || 0) + 1; });
  document.getElementById('verdicts').textContent =
    Object.entries(tally).map(([v, c]) => `${v === 'ok' ? '有正文' : (LABELS[v] || v)} ${c}`).join(' · ');
}
document.getElementById('q').addEventListener('input', apply);
document.getElementById('valid').addEventListener('change', apply);
</script></body></html>'''


if __name__ == '__main__':
    raise SystemExit(main())
