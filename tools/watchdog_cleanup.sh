#!/data/data/com.termux/files/usr/bin/bash
# Keep the final cleanup pass alive on the phone (Android kills Termux now and then).
ARCHIVE=/storage/emulated/0/Download/xiasl_archive
LOG=$ARCHIVE/cleanup_watchdog.log

pending() {
  python3 - <<'PY'
import sqlite3
db = sqlite3.connect('file:/storage/emulated/0/Download/xiasl_archive/state.sqlite3?mode=ro', uri=True)
print(db.execute("SELECT COUNT(*) FROM download_jobs WHERE state='pending'").fetchone()[0])
PY
}

echo "[$(date '+%F %T')] cleanup watchdog start" >> "$LOG"
while true; do
  if ! pgrep -f '[w]ayback_xiasl.py.*xiasl_archive' > /dev/null; then
    left=$(pending 2>/dev/null || echo 0)
    if [ "${left:-0}" -gt 0 ]; then
      echo "[$(date '+%F %T')] crawler down, $left pending -> restart" >> "$LOG"
      cd /data/data/com.termux/files/home/dsh || exit 1
      python wayback_xiasl.py --output "$ARCHIVE" --skip-index --no-verify-blobs \
        --delay 1.5 --timeout 40 --retries 4 >> "$ARCHIVE/cleanup_run.log" 2>&1
      echo "[$(date '+%F %T')] crawler exited (rc=$?)" >> "$LOG"
    else
      echo "[$(date '+%F %T')] nothing pending; watchdog idle" >> "$LOG"
      sleep 600
    fi
  fi
  sleep 60
done
