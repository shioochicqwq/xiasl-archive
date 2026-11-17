#!/usr/bin/env python3
"""Mark jobs that another archive has already completed as covered.

The three archives plan their own job tables, so an object whose captures span a
slice boundary can be queued on two machines at once. This reads the union of
completed job keys (job_key = canonical_key|digest, identical everywhere) and
marks them `covered_elsewhere` in the target database, which keeps the scheduler
from replaying content that already exists on disk somewhere.

Usage:
    python sync_coverage.py <target-state.sqlite3> <keys-file> [<keys-file> ...]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

INSERT = ("INSERT OR IGNORE INTO download_jobs (job_key, canonical_key, digest, resource_type, "
          "priority, state) VALUES (?, substr(?, 1, instr(?, '|') - 1), "
          "substr(?, instr(?, '|') + 1), 'other', 4, 'covered_elsewhere')")


def load_keys(paths):
    keys = set()
    for path in paths:
        for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line:
                keys.add(line)
    return keys


def main(argv):
    target, key_files = Path(argv[1]), argv[2:]
    keys = load_keys(key_files)
    if not keys:
        print('no keys supplied')
        return 1
    db = sqlite3.connect(target)
    before = dict(db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state'))
    with db:
        db.executemany("UPDATE download_jobs SET state='covered_elsewhere' "
                       "WHERE job_key=? AND state NOT IN ('downloaded','covered_elsewhere')",
                       [(key,) for key in keys])
        db.executemany(INSERT, [(key, key, key, key, key) for key in keys])
    after = dict(db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state'))
    print('before:', before)
    print('after :', after)
    print('pending now:', db.execute("SELECT COUNT(*) FROM download_jobs WHERE state='pending'").fetchone()[0])
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
