#!/usr/bin/env python3
"""Check a generated static site: are the internal links resolvable?

Usage: python3 verify_site.py --site <dir> [--archive <dir>] [--limit N] [--report <file>]
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import urllib.parse
from pathlib import Path

LINK_RE = re.compile(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', re.I)


def target_file(site: Path, url: str):
    """Map a site-absolute URL to the file that should serve it."""
    path = urllib.parse.urlsplit(url).path
    candidate = site / urllib.parse.unquote(path).lstrip('/')
    if path.endswith('/'):
        return candidate / 'index.html'
    if candidate.is_file():
        return candidate
    if not candidate.suffix:
        return candidate / 'index.html'
    return candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--report')
    args = parser.parse_args()
    site = Path(args.site)

    counts = collections.Counter()
    broken = collections.Counter()
    samples = []
    pages = 0
    for html in site.rglob('*.html'):
        if args.limit and pages >= args.limit:
            break
        pages += 1
        try:
            text = html.read_text(encoding='utf-8', errors='replace')
        except OSError:
            counts['unreadable_pages'] += 1
            continue
        for url in LINK_RE.findall(text):
            if url.startswith(('http://', 'https://', '//', 'mailto:', 'javascript:', 'data:', '#')):
                counts['external_links' if url.startswith(('http', '//')) else 'inline_links'] += 1
                continue
            if not url.startswith('/'):
                counts['relative_links'] += 1
                continue
            if url.startswith('/missing/'):
                counts['missing_page_links'] += 1
                continue
            counts['internal_links'] += 1
            if target_file(site, url).exists():
                counts['resolved'] += 1
            else:
                counts['broken'] += 1
                broken[url.split('?')[0]] += 1
                if len(samples) < 20:
                    samples.append((str(html.relative_to(site)), url))

    files = sum(1 for _ in site.rglob('*') if _.is_file())
    report = {
        'site': str(site),
        'files': files,
        'pages_scanned': pages,
        'counts': dict(counts),
        'resolution_rate': round(counts['resolved'] / counts['internal_links'], 4) if counts['internal_links'] else None,
        'top_broken': broken.most_common(20),
        'broken_samples': samples,
    }
    if args.archive:
        db = sqlite3.connect(f'file:{Path(args.archive) / "state.sqlite3"}?mode=ro', uri=True)
        objects = db.execute("SELECT COUNT(*) FROM download_jobs WHERE state='downloaded'").fetchone()[0]
        report['archived_objects'] = objects
        report['pages_per_object'] = round(files / objects, 3) if objects else None
        db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
