#!/data/data/com.termux/files/usr/bin/bash
# Publish a heartbeat from the phone so the server-side night watcher can tell
# whether the phone worker is still alive (the phone can be killed by Android).
LOG=/storage/emulated/0/Download/xiasl_phone/heartbeat.log
while true; do
  if ssh -o BatchMode=yes -o ConnectTimeout=15 dev-sg 'date +%s > ~/xiasl-wayback/.phone_heartbeat' 2>/dev/null; then
    echo "[$(date '+%F %T')] heartbeat sent" >> "$LOG"
  else
    echo "[$(date '+%F %T')] heartbeat failed (network?)" >> "$LOG"
  fi
  sleep 300
done
