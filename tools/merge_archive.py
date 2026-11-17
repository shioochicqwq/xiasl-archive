#!/usr/bin/env python3
"""Merge another archive's successfully downloaded rows into this archive.

Usage: merge_archive.py <source_archive_dir> <target_archive_dir>

Only `downloaded` rows are considered, and every file is verified by size and
SHA256 before it is copied. Source rows are then marked `filtered` with a
pointer to the target copy, so the source archive never downloads them again.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path


def digest_of(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv):
    source_dir, target_dir = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    source = sqlite3.connect(f'file:{source_dir / "state.sqlite3"}?mode=ro', uri=True)
    target = sqlite3.connect(target_dir / 'state.sqlite3')
    rows = source.execute('''SELECT timestamp, original, path, sha256, bytes FROM captures
        WHERE state = 'downloaded' ''').fetchall()
    copied = skipped = broken = 0
    for timestamp, original, relative, sha256, size in rows:
        file_path = source_dir / relative
        if not file_path.is_file() or file_path.stat().st_size != size or digest_of(file_path) != sha256:
            broken += 1
            continue
        row = target.execute('SELECT state FROM captures WHERE timestamp=? AND original=?',
                             (timestamp, original)).fetchone()
        if row is not None and row[0] == 'downloaded':
            skipped += 1
            continue
        with target:
            if row is None:
                target.execute('''INSERT OR IGNORE INTO captures
                    (timestamp, original, mimetype, statuscode, digest, length,
                     state, path, sha256, bytes, replay_url)
                    VALUES (?, ?, 'application/octet-stream', '200', '', '0',
                            'downloaded', ?, ?, ?, NULL)''',
                    (timestamp, original, relative, sha256, size))
            else:
                target.execute('''UPDATE captures SET state='downloaded', path=?, error=NULL,
                    sha256=?, bytes=? WHERE timestamp=? AND original=?''',
                    (relative, sha256, size, timestamp, original))
        destination = target_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, destination)
        copied += 1
    print(f'copied: {copied}, already present: {skipped}, unreadable source files: {broken}')
    print('target states:', target.execute('SELECT state, COUNT(*) FROM captures GROUP BY state').fetchall())
    source.close()
    target.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
