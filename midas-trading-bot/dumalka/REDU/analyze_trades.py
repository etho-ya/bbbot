#!/usr/bin/env python3
"""Score-WR correlation and Conviction Sizing backtest"""
import csv

with open('/tmp/trades.csv') as f:
    rows = list(csv.DictReader(f))

print(f"Total: {len(rows)} trades\n")

# Q1: Score -> WR
buckets = {}
for r in rows:
    sc = r['initial_signal_score']
    if not sc: continue
    sc = float(sc)
    pnl = float(r['realized_pnl_pct'] or 0)
    mfe = float(r['max_pnl_pct'] or 0)
    if sc < 0.40: b = '1:<0.40'
    elif sc < 0.55: b = '2:0.40-55'
    elif sc < 0.70: b = '3:0.55-70'
    elif sc < 0.85: b = '4:0.70-85'
    else: b = '5:>0.85'
    if b not in buckets: buckets[b] = {'c':0,'w':0,'p':0,'m':0}
    buckets[b]['c'] += 1
    if pnl > 0: buckets[b]['w'] += 1
    buckets[b]['p'] += pnl
    buckets[b]['m'] += mfe

print("=== SCORE -> WIN RATE ===")
print(f"{'bucket':<12} {'cnt':>4} {'WR%':>6} {'avgPnl':>8} {'sumPnl':>8} {'avgMFE':>7}")
for b in sorted(buckets):
    d = buckets[b]
    wr = 100*d['w']/d['c']
    print(f"{b:<12} {d['c']:>4} {wr:>6.1f} {d['p']/d['c']:>8.3f} {d['p']:>8.1f} {d['m']/d['c']:>7.2f}")

# Q2: Close reason
reasons = {}
for r in rows:
    cr = r['close_reason'] or 'N/A'
    pnl = float(r['realized_pnl_pct'] or 0)
    if cr not in reasons: reasons[cr] = {'c':0,'p':0}
    reasons[cr]['c'] += 1
    reasons[cr]['p'] += pnl

print("\n=== CLOSE REASON ===")
for cr in sorted(reasons, key=lambda x: reasons[x]['p']):
    d = reasons[cr]
    print(f"{cr:<18} cnt={d['c']:>3} avg={d['p']/d['c']:>+6.2f}% sum={d['p']:>+8.1f}%")

# Q3: Conviction backtest
flat = conv = 0; n = 0
for r in rows:
    sc = r['initial_signal_score']
    if not sc: continue
    sc = float(sc); pnl = float(r['realized_pnl_pct'] or 0)
    rec = r['initial_recommendation'] or ''
    n += 1; flat += pnl
    if rec == 'approve' and sc >= 0.70: conv += pnl * 1.5
    elif rec == 'approve': conv += pnl * 1.0
    elif rec == 'reduce': conv += pnl * 0.5
    else: conv += pnl * 0.3

print(f"\n=== CONVICTION BACKTEST ({n} trades) ===")
print(f"Flat:       {flat:>+8.1f}%")
print(f"Conviction: {conv:>+8.1f}%")
print(f"Delta:      {conv-flat:>+8.1f}%")

# Q4: By recommendation
recs = {}
for r in rows:
    rec = r['initial_recommendation'] or 'none'
    pnl = float(r['realized_pnl_pct'] or 0)
    if rec not in recs: recs[rec] = {'c':0,'w':0,'p':0}
    recs[rec]['c'] += 1
    if pnl > 0: recs[rec]['w'] += 1
    recs[rec]['p'] += pnl

print("\n=== BY RECOMMENDATION ===")
for rec in sorted(recs):
    d = recs[rec]
    print(f"{rec:<10} cnt={d['c']:>3} WR={100*d['w']/d['c']:>5.1f}% sum={d['p']:>+8.1f}% avg={d['p']/d['c']:>+6.3f}%")

print("\nDONE")
