import json
import os
from datetime import datetime, timedelta, timezone

def export_detailed_logs():
    log_file = '/opt/risk-engine/logs/app.log'
    output_file = '/opt/risk-engine/dumalka_detailed_logs_24h.md'
    
    if not os.path.exists(log_file):
        print(f"Log file not found: {log_file}")
        return
        
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    # We will group by trace_id or symbol if possible, or just chronologically
    extracted_logs = []
    
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                # Check timeframe
                if 'asctime' in data:
                    try:
                        # Format: 2026-04-01 06:43:27,123
                        time_str = data['asctime'].split(',')[0]
                        log_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                        log_time = log_time.replace(tzinfo=timezone.utc)
                        if log_time < cutoff_time:
                            continue
                    except Exception:
                        pass
                
                # Check if it's related to Dumalka
                # It could be 'risk-engine.tracker' logger, or message containing 'Dumalka'
                is_dumalka = False
                if data.get('name') == 'risk-engine.tracker':
                    is_dumalka = True
                elif 'Dumalka' in data.get('message', ''):
                    is_dumalka = True
                    
                if is_dumalka:
                    extracted_logs.append(data)
            except json.JSONDecodeError:
                # Fallback if not JSON (though it should be based on config)
                if 'Dumalka' in line or 'risk-engine.tracker' in line:
                    extracted_logs.append({'message': line.strip(), 'raw': True})

    # Group by symbol
    by_symbol = {}
    for log in extracted_logs:
        sym = log.get('symbol') or log.get('symbol ') or 'UNKNOWN_SYMBOL'
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(log)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('# 📋 Детальные системные логи Думалки (app.log)\n\n')
        f.write(f'**Период**: Последние 24 часа\n**Всего записей**: {len(extracted_logs)}\n\n---\n\n')
        
        for sym, logs in sorted(by_symbol.items()):
            f.write(f'## 🔸 {sym}\n\n```text\n')
            for log in logs:
                if log.get('raw'):
                    f.write(f"{log['message']}\n")
                else:
                    t = log.get('asctime', '')
                    lvl = log.get('levelname', 'INFO')
                    msg = log.get('message', '')
                    trace = log.get('trace_id', '')
                    trace_str = f" [trace:{trace}]" if trace else ""
                    f.write(f"[{t}] {lvl:5s} {msg}{trace_str}\n")
            f.write('```\n\n')

    print(f"Extracted {len(extracted_logs)} detailed logs to {output_file}")

if __name__ == '__main__':
    export_detailed_logs()
