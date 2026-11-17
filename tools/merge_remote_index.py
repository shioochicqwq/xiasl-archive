#!/usr/bin/env python3
"""Merge a local index snapshot into the remote checkpoint, preserving downloads.

Runs on the server: `python3 merge_remote_index.py state_upload.sqlite3 state.sqlite3`.
Existing rows (including `downloaded` states) always win, so re-running is safe.
"""
from __future__ import annotations

import sqlite3
import sys

COLUMNS = 'timestamp, original, mimetype, statuscode, digest, length, state, path, error, sha256, bytes, replay_url'


def main(argv):
    incoming_path, checkpoint_path = argv[1], argv[2]
    db = sqlite3.connect(checkpoint_path)
    before = dict(db.execute('SELECT state, COUNT(*) FROM captures GROUP BY state'))
    with db:
        db.execute('ATTACH DATABASE ? AS incoming', (incoming_path,))
        db.execute(f'INSERT OR IGNORE INTO captures ({COLUMNS}) SELECT {COLUMNS} FROM incoming.captures')
        # Materialise before detaching: an open cursor would keep the database locked.
        metadata = db.execute('SELECT key, value FROM incoming.meta').fetchall()
        for key, value in metadata:
            if key != 'scope':  # The remote scope stays authoritative.
                db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, value))
    db.execute('DETACH DATABASE incoming')
    after = dict(db.execute('SELECT state, COUNT(*) FROM captures GROUP BY state'))
    print('added rows:', sum(after.values()) - sum(before.values()))
    print('states before:', before)
    print('states after:', after)
    print('total rows:', sum(after.values()))
    print('index_complete:', db.execute("SELECT value FROM meta WHERE key = 'index_complete'").fetchone()[0])
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
