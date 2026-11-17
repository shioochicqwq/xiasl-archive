#!/usr/bin/env python3
"""Print downloaded capture rows as TSV: timestamp, original, path, sha256, bytes.

Runs on a download server so the merge transfers payload files plus this small
text list instead of a multi-hundred-megabyte checkpoint database.

Usage: python3 export_rows.py <archive-dir>
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(argv):
    directory = Path(argv[1])
    db = sqlite3.connect(f'file:{directory / "state.sqlite3"}?mode=ro', uri=True)
    rows = db.execute("SELECT timestamp, original, path, sha256, bytes FROM captures "
                      "WHERE state='downloaded' AND path IS NOT NULL AND sha256 IS NOT NULL")
    count = 0
    for timestamp, original, path, sha256, size in rows:
        if any('\t' in str(value) for value in (timestamp, original, path, sha256)):
            continue  # a tab cannot be represented in this format
        print(f'{timestamp}\t{original}\t{path}\t{sha256}\t{size}')
        count += 1
    db.close()
    print(f'# rows: {count}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
