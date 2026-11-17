"""Report runnable remaining jobs and recent throughput for an archive directory.

Usage: worker_status.py <dir> <log-glob> <min-timestamp> <max-timestamp> <label>
"""
import datetime
import glob
import os
import sqlite3
import sys


def rate(path, label):
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return f'{label}: no log'
    saved = [line for line in lines if 'Saved job' in line]
    if len(saved) < 3:
        return f'{label}: saved={len(saved)}'
    stamp = lambda line: datetime.datetime.strptime(line[:19], '%Y-%m-%d %H:%M:%S')
    recent = [line for line in saved if (stamp(saved[-1]) - stamp(line)).total_seconds() <= 600]
    span = (stamp(recent[-1]) - stamp(recent[0])).total_seconds() or 1
    return f'{label}: saved={len(saved)} rate={(len(recent) - 1) / span * 60:.1f}/min last={saved[-1][:19]}'


def pending(db_path, minimum, maximum):
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        return db.execute(
            "SELECT COUNT(*) FROM download_jobs j WHERE j.state='pending' AND EXISTS ("
            "SELECT 1 FROM job_candidates k WHERE k.job_key = j.job_key "
            "AND k.timestamp >= ? AND k.timestamp <= ?)", (minimum, maximum)).fetchone()[0]
    finally:
        db.close()


if __name__ == '__main__':
    root, log_glob, minimum, maximum, label = sys.argv[1:6]
    logs = glob.glob(os.path.join(root, log_glob))
    log = max(logs, key=os.path.getmtime) if logs else os.path.join(root, 'none.log')
    print(rate(log, label))
    print(f'{label} 剩余: {pending(os.path.join(root, "state.sqlite3"), minimum, maximum)}')
