"""Simulate the handoff decision against dev-sg's live remaining-job distribution."""
import sys

sys.path.insert(0, '/data/data/com.termux/files/home/dsh')
import handoff as h

rows = h.remaining_by_period(h.DEV_SG_MIN)
if rows is None:
    raise SystemExit('could not read dev-sg remaining jobs')

remaining = sum(count for _, count in rows)
base = remaining
total = h.RATES['dev_sg'] + h.RATES['mc_hk'] + h.RATES['phone']
print(f'dev-sg remaining now: {remaining} jobs, newest month {rows[0][0]}, oldest {rows[-1][0]}')

take = {}
current = list(rows)
for worker in ('mc_hk', 'phone'):
    quota = int(base * h.RATES[worker] / total)
    chosen = h.pick_boundary(current, quota)
    if not chosen:
        continue
    low, high, taken, _quota = chosen[0], chosen[1], chosen[2], quota
    take[worker] = (low, high, taken, quota)
    current = [row for row in current if row[0] < low[:8]]

kept = sum(count for _, count in current)
kept_high = h.previous_day_end(take['phone'][0][:8]) if 'phone' in take else None

print(f'\nplan_base={base}  combined_rate={total}/min')
for worker, (low, high, taken, quota) in take.items():
    print(f'  {worker:6s} quota={quota:6d} -> takes {taken:6d} jobs  {low}..{high}  ~{taken/h.RATES[worker]/60:.1f} h')
print(f'  dev-sg keeps {kept:6d} jobs  window {h.DEV_SG_MIN}..{kept_high}  ~{kept/h.RATES["dev_sg"]/60:.1f} h')

times = [take[w][2] / h.RATES[w] for w in take] + [kept / h.RATES['dev_sg']]
print('finish times: ' + ', '.join(f'{w} {take[w][2]/h.RATES[w]/60:.1f}h' for w in take)
      + f', dev-sg {kept/h.RATES["dev_sg"]/60:.1f}h')
print(f'makespan {max(times)/60:.1f} h  (dev-sg alone would need {remaining/h.RATES["dev_sg"]/60:.1f} h)')
