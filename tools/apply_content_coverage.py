#!/usr/bin/env python3
"""Mark archive rows as downloaded when the identical content already exists locally.

Used when merging archives by content hash: a capture whose payload is already
stored here (under any path) counts as downloaded, so the site build and the
coverage statistics include it without transferring the file twice.

Usage: python3 apply_content_coverage.py <state.sqlite3> <rows.tsv>
       (rows.tsv: timestamp<TAB>original<TAB>sha256)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(argv):
    db_path, rows_path = argv[1], Path(argv[2])
    db = sqlite3.connect(db_path)
    known = {row[0]: (row[1], row[2]) for row in db.execute(
        "SELECT sha256, path, bytes FROM captures "
        "WHERE sha256 IS NOT NULL AND path IS NOT NULL GROUP BY sha256")}
    print('locally stored payloads:', len(known))
    updated = missing = 0
    with db:
        for line in rows_path.read_text(encoding='utf-8', errors='replace').splitlines():
            fields = line.split('\t')
            if len(fields) != 3:
                continue
            timestamp, original, sha256 = fields
            stored = known.get(sha256)
            if stored is None:
                missing += 1
                continue
            cursor = db.execute(
                "UPDATE captures SET state='downloaded', path=?, sha256=?, bytes=?, blob_sha256=? "
                "WHERE timestamp=? AND original=? AND state<>'downloaded'",
                (stored[0], sha256, stored[1], sha256, timestamp, original))
            updated += cursor.rowcount
    total = db.execute("SELECT COUNT(DISTINCT canonical_key) FROM captures "
                       "WHERE state='downloaded' AND canonical_key IS NOT NULL").fetchone()[0]
    print(f'rows marked downloaded: {updated}, payload not stored here: {missing}')
    print(f'objects with content now: {total}')
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
