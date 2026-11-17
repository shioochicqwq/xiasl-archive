#!/usr/bin/env python3
"""Automatic work handoff between the three download workers.

The phone and mc-hk finish their slices before dev-sg does. This watcher notices
an idle worker, hands it the newest still-unclaimed block of dev-sg's remaining
jobs, and narrows dev-sg's window so the same work is not requested twice.

Idempotent and safe to restart: it acts only when a worker has no runnable job
left, never deletes data, and logs every decision.

Usage:
    python handoff.py             # dry-run: report what it would do
    python handoff.py --execute   # perform handoffs, keep watching
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PHONE_DIR = '/storage/emulated/0/Download/xiasl_phone'
PHONE_LOG = f'{PHONE_DIR}/run_handoff.log'
MC_HK_DIR = '$HOME/xiasl-wayback-b'
MC_HK_LOG = 'logs/handoff.log'
DEV_SG_DIR = '$HOME/xiasl-wayback'
DEV_SG_LOG = 'logs/handoff-slice.log'
DEV_SG_MIN = '20100616000000'
LOCAL_SCRIPT = '/data/data/com.termux/files/home/dsh/wayback_xiasl.py'
STATE_FILE = Path('/data/data/com.termux/files/home/dsh/handoff_state.json')

# Measured throughput in jobs/min, used to size each worker's share.
RATES = {'phone': 8.6, 'mc_hk': 9.7, 'dev_sg': 17.3}


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')


def log(message, execute):
    print(f'[{now()}]{"" if execute else " [dry-run]"} {message}', flush=True)


def ssh(host, command, timeout=90):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', host, command],
                          capture_output=True, text=True, timeout=timeout)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {'assigned': {}, 'dev_sg_min': DEV_SG_MIN}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def remaining_by_period(minimum, maximum=None):
    """dev-sg pending jobs that still have a candidate inside dev-sg's window.

    The script is piped over stdin: nesting quotes inside an `ssh host python3 -c`
    argument invites escaping bugs.
    """
    window = f"k.timestamp >= '{minimum}'"
    if maximum:
        window += f" AND k.timestamp <= '{maximum}'"
    script = '\n'.join([
        'import sqlite3',
        "db = sqlite3.connect('file:state.sqlite3?mode=ro', uri=True)",
        'rows = db.execute(',
        '    "SELECT substr(preferred_timestamp,1,8), COUNT(*) FROM download_jobs j "',
        '    "WHERE j.state=\'pending\' AND EXISTS (SELECT 1 FROM job_candidates k "',
        f'    "WHERE k.job_key = j.job_key AND {window}) GROUP BY 1 ORDER BY 1 DESC"',
        ').fetchall()',
        'print(sum(count for _, count in rows))',
        'for day, count in rows:',
        "    print(day, count)",
    ])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', 'dev-sg',
                             f'cd {DEV_SG_DIR} && python3 -'],
                            input=script, capture_output=True, text=True, timeout=120)
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if not lines or not lines[0].strip().isdigit():
        log(f'could not read dev-sg remaining: {result.stderr.strip()[:160]}', True)
        return None
    rows = []
    for line in lines[1:]:
        day, _, count = line.partition(' ')
        if day.strip().isdigit() and count.strip().isdigit():
            rows.append((day.strip(), int(count)))
    return sorted(rows, key=lambda item: item[0], reverse=True)


def previous_day_end(day):
    """Last timestamp of the day before `day` (YYYYMMDD)."""
    from datetime import date, timedelta
    earlier = date(int(day[:4]), int(day[4:6]), int(day[6:8])) - timedelta(days=1)
    return earlier.strftime('%Y%m%d') + '235959'


def pick_boundary(rows, quota):
    """Newest-first day blocks until the quota is met; returns (low, high, jobs).

    Day granularity keeps the handover close to the quota; whole years or months
    overshoot badly because single periods hold thousands of jobs.
    """
    taken, low, high = 0, None, None
    for day, count in rows:
        if taken >= quota:
            break
        taken += count
        high = f'{day}235959' if high is None else high
        low = f'{day}000000'
    if low is None:
        return None
    return low, high, taken


def phone_runnable(window):
    """Jobs the phone can start right now: pending, inside its window, retry window elapsed.

    Jobs waiting out a retry backoff are not work the machine can do, so counting
    them would keep an idle worker looking busy for hours.
    """
    connection = sqlite3.connect(f'file:{PHONE_DIR}/state.sqlite3?mode=ro', uri=True)
    try:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if window:
            return connection.execute(
                "SELECT COUNT(*) FROM download_jobs j WHERE j.state='pending' "
                "AND (j.next_retry_at IS NULL OR j.next_retry_at <= ?) AND EXISTS "
                "(SELECT 1 FROM job_candidates k WHERE k.job_key=j.job_key "
                "AND k.timestamp>=? AND k.timestamp<=?)", (now, *window)).fetchone()[0]
        return connection.execute(
            "SELECT COUNT(*) FROM download_jobs WHERE state='pending' "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?)", (now,)).fetchone()[0]
    finally:
        connection.close()


def local_crawler_running():
    result = subprocess.run(['pgrep', '-f', '[w]ayback_xiasl.py'], capture_output=True, text=True)
    return bool(result.stdout.strip())


def restart_dev_sg(minimum, maximum, execute):
    log(f'dev-sg window narrowed to {minimum}..{maximum}', execute)
    if not execute:
        return
    ssh('dev-sg', 'tmux kill-session -t xiasl 2>/dev/null; '
                  'for pid in $(pgrep -f "[w]ayback_xiasl.py"); do kill "$pid" 2>/dev/null; done; sleep 2')
    command = (f'python3 wayback_xiasl.py --skip-index --output ~/xiasl-wayback --min-timestamp {minimum} '
               f'--max-timestamp {maximum} --max-downloads 60000 --delay 1.5 --timeout 40 --retries 4 '
               f'--no-verify-blobs >> {DEV_SG_LOG} 2>&1')
    ssh('dev-sg', f'cd {DEV_SG_DIR} && tmux new-session -d -s xiasl "{command}"')


def start_phone(low, high, execute):
    log(f'handing {low}..{high} to the phone', execute)
    if not execute:
        return
    subprocess.run(['tmux', 'kill-session', '-t', 'xiasl-phone'], capture_output=True)
    subprocess.run(['pkill', '-f', 'wayback_xiasl.py'], capture_output=True)
    time.sleep(3)
    command = (f'cd /data/data/com.termux/files/home/dsh && python {LOCAL_SCRIPT} --output {PHONE_DIR} '
               f'--skip-index --replan --min-timestamp {low} --max-timestamp {high} '
               f'--delay 1.5 --timeout 40 --retries 4 >> {PHONE_LOG} 2>&1')
    subprocess.run(['tmux', 'new-session', '-d', '-s', 'xiasl-phone', command], check=True)


def start_mc_hk(low, high, execute):
    log(f'handing {low}..{high} to mc-hk', execute)
    if not execute:
        return
    ssh('mc-hk', 'for pid in $(pgrep -f "[w]ayback_xiasl.py"); do kill "$pid" 2>/dev/null; done; sleep 2')
    command = (f'python3 wayback_xiasl.py --skip-index --output {MC_HK_DIR} --min-timestamp {low} '
               f'--max-timestamp {high} --max-downloads 60000 --delay 1.5 --timeout 40 --retries 4 '
               f'>> {MC_HK_LOG} 2>&1')
    ssh('mc-hk', f'cd {MC_HK_DIR} && (setsid nohup {command} &) && echo started')


def main():
    parser = argparse.ArgumentParser(description='把 dev-sg 剩余的分片交给空出来的 worker')
    parser.add_argument('--execute', action='store_true', help='真正执行交接（默认为 dry-run）')
    parser.add_argument('--interval', type=float, default=120, help='轮询间隔秒数')
    parser.add_argument('--once', action='store_true', help='只跑一轮')
    args = parser.parse_args()

    state = load_state()
    while True:
        rows = remaining_by_period(state['dev_sg_min'], state.get('dev_sg_max'))
        if rows is None:
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        remaining = sum(count for _, count in rows)
        # A worker is idle when it has nothing runnable left and no crawler running.
        phone_running = local_crawler_running()
        phone_left = phone_runnable(state['assigned'].get('phone'))
        idle = {'phone': (not phone_running) or phone_left == 0,
                'mc_hk': not ssh('mc-hk', 'pgrep -f "[w]ayback_xiasl.py" | head -1').stdout.strip()}
        log(f'status: phone_idle={idle["phone"]} mc_hk_idle={idle["mc_hk"]} '
            f'dev_sg_remaining={remaining} assigned={sorted(state["assigned"])}', args.execute)

        total_rate = RATES['dev_sg'] + RATES['mc_hk'] + RATES['phone']
        cooldowns = state.setdefault('cooldowns', {})
        for worker in ('mc_hk', 'phone'):  # faster worker takes the newest block
            # A worker can be served again once it has finished its previous block;
            # the cooldown covers the seconds around its restart.
            if not idle[worker] or time.time() < cooldowns.get(worker, 0):
                continue
            # Quotas come from the work left at the first handoff, so that all three
            # machines end up finishing at roughly the same time.
            if 'plan_base' not in state:
                state['plan_base'] = remaining
            quota = int(state['plan_base'] * RATES[worker] / total_rate)
            if quota < 200:
                log(f'{worker} would only take {quota} jobs; leaving it idle', args.execute)
                continue
            chosen = pick_boundary(rows, quota)
            if not chosen:
                continue
            low, high, taken = chosen
            log(f'{worker} takes {taken} jobs in {low}..{high} (quota {quota})', args.execute)
            (start_phone if worker == 'phone' else start_mc_hk)(low, high, args.execute)
            state['assigned'][worker] = [low, high]
            state['history'] = state.get('history', []) + [
                {'worker': worker, 'low': low, 'high': high, 'jobs': taken, 'at': now()}]
            cooldowns[worker] = time.time() + 900
            if low[:6] > '201012':
                state['dev_sg_max'] = previous_day_end(low[:8])
                restart_dev_sg(state['dev_sg_min'], state['dev_sg_max'], args.execute)
            rows = [row for row in rows if row[0] < low[:8]]
            remaining = sum(count for _, count in rows)
        if args.execute:
            save_state(state)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
