#!/bin/bash
# Deploy the crawler to dev-sg; --with-index additionally merges the completed
# local index into the remote checkpoint (running downloads are preserved).
set -euo pipefail

LOCAL=/data/data/com.termux/files/home/dsh
ARCHIVE=/storage/emulated/0/Download/xiasl_archive
REMOTE=dev-sg
REMOTE_DIR='$HOME/xiasl-wayback'
WITH_INDEX=${1:-}

ssh "$REMOTE" "mkdir -p $REMOTE_DIR/files $REMOTE_DIR/logs"

FILES=(wayback_xiasl.py test_wayback_xiasl.py README_wayback_xiasl.md merge_remote_index.py)

if [ "$WITH_INDEX" = "--with-index" ]; then
  # Stop the remote crawler so the checkpoint cannot change during the merge.
  # Terminate by PID: `pkill -f wayback_xiasl.py` would also match, and kill,
  # the shell running this very command.
  ssh "$REMOTE" "tmux kill-session -t xiasl 2>/dev/null || true; for pid in \$(pgrep -f '[p]ython3 wayback_xiasl.py'); do kill \"\$pid\" 2>/dev/null || true; done; sleep 2; pgrep -f '[p]ython3 wayback_xiasl.py' || echo 'remote crawler stopped'"
  python3 - "$ARCHIVE/state.sqlite3" "$LOCAL/state_upload.sqlite3" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1], sys.argv[2]
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
with sqlite3.connect(destination) as check:
    print('local snapshot rows:', check.execute('SELECT COUNT(*) FROM captures').fetchone()[0])
    print('local index_complete:', check.execute("SELECT value FROM meta WHERE key='index_complete'").fetchone())
PY
  rm -rf "$LOCAL/upload_files"
  cp -rL "$ARCHIVE/files" "$LOCAL/upload_files"
  FILES+=(state_upload.sqlite3)
fi

tar -czf - -C "$LOCAL" "${FILES[@]}" \
  | ssh "$REMOTE" "cd $REMOTE_DIR && tar -xzf -"

if [ "$WITH_INDEX" = "--with-index" ]; then
  tar -cf - -C "$LOCAL" upload_files | ssh "$REMOTE" "mkdir -p $REMOTE_DIR/files && tar -xf - -C $REMOTE_DIR/files --strip-components=1"
  ssh "$REMOTE" "cd $REMOTE_DIR && python3 merge_remote_index.py state_upload.sqlite3 state.sqlite3"
  rm -f "$REMOTE_DIR/state_upload.sqlite3" 2>/dev/null || ssh "$REMOTE" "rm -f $REMOTE_DIR/state_upload.sqlite3"
  rm -rf "$LOCAL/upload_files" "$LOCAL/state_upload.sqlite3"
fi

ssh "$REMOTE" "cd $REMOTE_DIR && ls -la | head -12"
