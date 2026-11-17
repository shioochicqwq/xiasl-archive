#!/usr/bin/env python3
"""Build a minimal state.sqlite3 from an exported TSV, for merge_archive.py.

merge_archive.py only reads timestamp/original/path/sha256/bytes of downloaded
rows, so a small stand-in database is enough and saves transferring the real
(hundreds of megabytes) checkpoint from a server.

Usage: python3 make_source_db.py <staging-dir> <rows.tsv>
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(argv):
    staging, rows_path = Path(argv[1]), Path(argv[2])
    db_path = staging / 'state.sqlite3'
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.executescript('''
        CREATE TABLE captures (
            timestamp TEXT NOT NULL, original TEXT NOT NULL, mimetype TEXT NOT NULL DEFAULT 'text/html',
            statuscode TEXT NOT NULL DEFAULT '200', digest TEXT NOT NULL DEFAULT '',
            length TEXT NOT NULL DEFAULT '0', state TEXT NOT NULL DEFAULT 'downloaded', path TEXT,
            error TEXT, sha256 TEXT, bytes INTEGER, replay_url TEXT,
            PRIMARY KEY (timestamp, original));
    ''')
    count = 0
    with db:
        for line in rows_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) != 5:
                continue
            timestamp, original, path, sha256, size = fields
            db.execute("INSERT OR IGNORE INTO captures "
                       "(timestamp, original, path, sha256, bytes, state) "
                       "VALUES (?, ?, ?, ?, ?, 'downloaded')",
                       (timestamp, original, path, sha256, int(size) if size.isdigit() else None))
            count += 1
    db.commit()
    print(f'{db_path}: {count} rows from {rows_path.name}')
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
