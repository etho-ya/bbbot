"""
Backfill setup_master_text from TG Desktop export into signals DB.
Fixed: proper timezone handling and robust time matching.
"""
import json, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

TG_EXPORT = 'tg_export_report_group_result.json'
DB_PATH = 'src/data/signals.db'

RE_HEADER = re.compile(r'(\w+(?:\.\w)?)\s+\d+M\s*-\s*[🟢🔴🟡⚪]\s*(Strong\s+)?(BUY|SELL)', re.IGNORECASE)
RE_SETUP = re.compile(r'\*\*Setup Master[^*]*\*\*\s*\n(.*)', re.IGNORECASE | re.DOTALL)

def flatten_text(text_field):
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        return ''.join(
            item if isinstance(item, str) else item.get('text', '')
            for item in text_field
        )
    return str(text_field)

print("Loading TG export...")
with open(TG_EXPORT, 'r') as f:
    data = json.load(f)

messages = data.get('messages', [])
print(f"Total TG messages: {len(messages)}")

# Determine TG timezone offset by checking date_unixtime vs date
# The TG export "date" is in the local timezone of the exporter
sample_msg = None
for msg in messages:
    if 'date_unixtime' in msg and 'date' in msg:
        sample_msg = msg
        break

if sample_msg:
    unix_ts = int(sample_msg['date_unixtime'])
    local_str = sample_msg['date']
    utc_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    local_dt = datetime.fromisoformat(local_str)
    # offset = local - utc (in hours)
    offset_seconds = (local_dt - utc_dt.replace(tzinfo=None)).total_seconds()
    offset_hours = offset_seconds / 3600
    print(f"TG timezone offset: UTC{offset_hours:+.0f} (local: {local_str}, utc: {utc_dt.isoformat()})")
else:
    offset_hours = 0
    print("WARNING: Could not determine TG timezone, assuming UTC")

tg_offset = timedelta(hours=offset_hours)

# Extract Midas signals with Setup Master
tg_signals = []  # list of (symbol, side, utc_datetime, setup_text)
for msg in messages:
    text = flatten_text(msg.get('text', ''))
    if 'Setup Master' not in text:
        continue
    header_m = RE_HEADER.search(text)
    if not header_m:
        continue
    
    symbol = header_m.group(1).replace('.P', '').upper()
    side = 'long' if header_m.group(3).upper() == 'BUY' else 'short'
    
    sm = RE_SETUP.search(text)
    if not sm:
        continue
    
    setup_text = sm.group(1).strip()
    
    # Convert TG local time to UTC
    msg_date = msg.get('date', '')
    try:
        local_dt = datetime.fromisoformat(msg_date)
        utc_dt = local_dt - tg_offset  # Convert local to UTC
        tg_signals.append((symbol, side, utc_dt, setup_text))
    except:
        pass

print(f"TG signals with Setup Master: {len(tg_signals)}")

# Dedup: keep first per symbol+side within same hour
deduped = []
seen = set()
for sym, side, utc_dt, text in tg_signals:
    key = f"{sym}_{side}_{utc_dt.strftime('%Y-%m-%dT%H')}"
    if key not in seen:
        seen.add(key)
        deduped.append((sym, side, utc_dt, text))
print(f"After dedup (by hour): {len(deduped)}")

# Build lookup: for each (symbol, side) -> list of (utc_dt, text) sorted by time
from collections import defaultdict
tg_lookup = defaultdict(list)
for sym, side, utc_dt, text in deduped:
    tg_lookup[(sym, side)].append((utc_dt, text))
for k in tg_lookup:
    tg_lookup[k].sort()

# DB matching
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("""
SELECT id, symbol, side, created_at, setup_master_text
FROM signals WHERE source != 'backtest' ORDER BY created_at
""")
db_rows = cur.fetchall()
print(f"DB signals (live): {len(db_rows)}")

matched = 0
already_had = 0
not_matched = 0
match_details = []

for row in db_rows:
    sig_id, symbol, side, created_at, existing_text = row
    
    if existing_text:
        already_had += 1
        continue
    
    # Parse DB UTC time
    try:
        # Handle various formats: "2026-02-12T11:00:00+00:00" or "2026-03-21T20:00:12.725527+00:00"
        ca = created_at.replace('+00:00', '').replace('Z', '')
        if '.' in ca:
            db_utc = datetime.fromisoformat(ca)
        else:
            db_utc = datetime.fromisoformat(ca)
    except:
        not_matched += 1
        continue
    
    # Find closest TG signal by symbol+side within time window
    candidates = tg_lookup.get((symbol, side), [])
    best_text = None
    best_delta = timedelta(hours=999)
    
    for tg_utc, tg_text in candidates:
        delta = abs(db_utc - tg_utc)
        if delta < best_delta:
            best_delta = delta
            best_text = tg_text
    
    # Allow up to 4 hours window (signals can be batch-processed)
    if best_text and best_delta < timedelta(hours=4):
        cur.execute("UPDATE signals SET setup_master_text = ? WHERE id = ?",
                   (best_text, sig_id))
        matched += 1
        if matched <= 5:
            match_details.append(f"  {symbol} {side} | DB: {created_at} | delta: {best_delta}")
    else:
        not_matched += 1

conn.commit()

# Verify
cur.execute("SELECT COUNT(*) FROM signals WHERE setup_master_text IS NOT NULL AND setup_master_text != '' AND source != 'backtest'")
total_with = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM signals WHERE source != 'backtest'")
total_live = cur.fetchone()[0]

print(f"\n=== RESULTS ===")
print(f"Matched & updated: {matched}")
print(f"Already had text: {already_had}")
print(f"No TG match: {not_matched}")
print(f"Total with setup_master_text: {total_with}/{total_live}")

if match_details:
    print(f"\nSample matches:")
    for d in match_details:
        print(d)

conn.close()
