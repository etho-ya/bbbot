#!/usr/bin/env python3
"""Night trade analysis - complete."""
import psycopg2, json

conn = psycopg2.connect(
    dbname='riskengine_db', user='riskengine', password='riskengine123',
    host='127.0.0.1', port=5432, connect_timeout=5,
    options='-c statement_timeout=10000'
)
conn.set_session(autocommit=True)
cur = conn.cursor()

print("=" * 80)
print("ПОЗИЦИИ ОТКРЫТЫЕ НОЧЬЮ (>=29.03 18:00 UTC)")
print("=" * 80)

cur.execute("""
SELECT id, symbol, side, entry_price, current_price, current_sl,
       current_tp1, status, current_pnl_pct, max_pnl_pct, 
       zone, close_reason, close_reason_detailed, opened_at, closed_at,
       signal_hash, realized_pnl_pct, realized_pnl_usdt, original_sl, 
       initial_signal_score, initial_recommendation, drawdown_from_peak_pct
FROM open_positions 
WHERE opened_at >= '2026-03-29 15:00:00' 
ORDER BY opened_at
""")
rows = cur.fetchall()
cols = [d[0] for d in cur.description]
for r in rows:
    d = dict(zip(cols, r))
    print(json.dumps(d, default=str, ensure_ascii=False))

print("\n" + "=" * 80)
print("TRADE OUTCOMES НОЧНЫЕ")
print("=" * 80)

cur.execute("""
SELECT id, signal_hash, event_type, event_at, symbol, side, 
       price_at_event, pnl_pct, size_remaining, source, 
       re_recommendation, re_signal_score
FROM trade_outcomes 
WHERE event_at >= '2026-03-29 15:00:00' 
ORDER BY event_at
""")
rows = cur.fetchall()
cols = [d[0] for d in cur.description]
for r in rows:
    d = dict(zip(cols, r))
    print(json.dumps(d, default=str, ensure_ascii=False))

print("\n" + "=" * 80)
print("AUDIT LOG (команды Думалки)")
print("=" * 80)

cur.execute("""
SELECT id, timestamp, trace_id, symbol, action, 
       mc_diagnostics, market_state
FROM dumalka_audit_log
WHERE timestamp >= '2026-03-29 15:00:00'
ORDER BY timestamp
LIMIT 80
""")
rows = cur.fetchall()
cols = [d[0] for d in cur.description]
for r in rows:
    d = dict(zip(cols, r))
    print(json.dumps(d, default=str, ensure_ascii=False))

# Recent closed positions for context
print("\n" + "=" * 80)
print("ПОСЛЕДНИЕ ЗАКРЫТЫЕ ПОЗИЦИИ (все)")
print("=" * 80)

cur.execute("""
SELECT id, symbol, side, entry_price, current_price, current_sl,
       status, current_pnl_pct, max_pnl_pct, zone, 
       close_reason, close_reason_detailed, 
       opened_at, closed_at, realized_pnl_pct, realized_pnl_usdt
FROM open_positions 
WHERE status = 'closed' AND closed_at >= '2026-03-29 00:00:00'
ORDER BY closed_at DESC
LIMIT 30
""")
rows = cur.fetchall()
cols = [d[0] for d in cur.description]
for r in rows:
    d = dict(zip(cols, r))
    print(json.dumps(d, default=str, ensure_ascii=False))

# Currently open positions
print("\n" + "=" * 80)
print("ТЕКУЩИЕ ОТКРЫТЫЕ ПОЗИЦИИ (active now)")
print("=" * 80)

cur.execute("""
SELECT id, symbol, side, entry_price, current_price, current_sl,
       current_tp1, status, current_pnl_pct, max_pnl_pct, zone,
       opened_at, initial_signal_score, initial_recommendation
FROM open_positions 
WHERE status = 'open'
ORDER BY opened_at DESC
""")
rows = cur.fetchall()
cols = [d[0] for d in cur.description]
for r in rows:
    d = dict(zip(cols, r))
    print(json.dumps(d, default=str, ensure_ascii=False))

conn.close()
print("\nDONE")
