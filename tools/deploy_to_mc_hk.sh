#!/bin/bash
# Send the 2003-2009 share of the index to mc-hk so both machines download
# disjoint URL ranges in parallel (dev-sg takes 2010+).
set -euo pipefail

LOCAL=/data/data/com.termux/files/home/dsh
ARCHIVE=/storage/emulated/0/Download/xiasl_archive
REMOTE=mc-hk
REMOTE_DIR='$HOME/xiasl-wayback'

python3 - "$ARCHIVE/state.sqlite3" "$LOCAL/state_mchk.sqlite3" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1], sys.argv[2]
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
with sqlite3.connect(destination) as db:
    with db:
        db.execute("DELETE FROM captures WHERE timestamp > '20091231235959'")
        db.execute("UPDATE captures SET state='pending', path=NULL, error=NULL, sha256=NULL, bytes=NULL, replay_url=NULL")
    print('mc-hk share rows:', db.execute('SELECT COUNT(*) FROM captures').fetchone()[0])
    print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
    db.execute('VACUUM')
PY

ssh "$REMOTE" "mkdir -p $REMOTE_DIR/files $REMOTE_DIR/logs"
tar -czf - -C "$LOCAL" wayback_xiasl.py test_wayback_xiasl.py README_wayback_xiasl.md \
    merge_archive.py state_mchk.sqlite3 \
  | ssh "$REMOTE" "cd $REMOTE_DIR && tar -xzf - && mv -f state_mchk.sqlite3 state.sqlite3"
rm -f "$LOCAL/state_mchk.sqlite3"

ssh "$REMOTE" "cd $REMOTE_DIR && python3 -c \"
import sqlite3
db = sqlite3.connect('file:state.sqlite3?mode=ro', uri=True)
print('rows:', db.execute('SELECT COUNT(*) FROM captures').fetchone()[0])
print('max timestamp:', db.execute('SELECT MAX(timestamp) FROM captures').fetchone()[0])
\" && rm -rf __pycache__"
