#!/data/data/com.termux/files/usr/bin/bash
# Keep the phone's download worker alive: Termux gets killed by Android
# occasionally, and this relaunches the crawler while its slice still has work.
cd /data/data/com.termux/files/home/dsh || exit 1

OUT=/storage/emulated/0/Download/xiasl_phone
MIN=20110820000000
MAX=20111024235959
LOG=$OUT/watchdog.log

pending() {
  python3 - "$MIN" "$MAX" <<'PY'
import sqlite3, sys
db = sqlite3.connect('file:/storage/emulated/0/Download/xiasl_phone/state.sqlite3?mode=ro', uri=True)
print(db.execute("""SELECT COUNT(*) FROM download_jobs j WHERE j.state='pending' AND EXISTS (
  SELECT 1 FROM job_candidates k WHERE k.job_key = j.job_key
  AND k.timestamp >= ? AND k.timestamp <= ?)""", (sys.argv[1], sys.argv[2])).fetchone()[0])
PY
}

echo "[$(date '+%F %T')] watchdog start (window $MIN..$MAX)" >> "$LOG"
while true; do
  if ! pgrep -f '[w]ayback_xiasl.py' > /dev/null; then
    left=$(pending 2>/dev/null || echo 0)
    if [ "${left:-0}" -gt 0 ]; then
      echo "[$(date '+%F %T')] crawler not running, $left jobs left -> restarting" >> "$LOG"
      python wayback_xiasl.py --output "$OUT" --skip-index \
        --min-timestamp "$MIN" --max-timestamp "$MAX" \
        --delay 1.5 --timeout 40 --retries 4 >> "$OUT/run_phase2.log" 2>&1
      echo "[$(date '+%F %T')] crawler exited" >> "$LOG"
    else
      echo "[$(date '+%F %T')] window finished; watchdog idle" >> "$LOG"
      sleep 300
    fi
  fi
  sleep 60
done
