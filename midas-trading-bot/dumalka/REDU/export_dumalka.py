import psycopg2
import json
from datetime import datetime, timezone

def export_logs():
    try:
        conn = psycopg2.connect('postgresql://riskengine:riskengine123@/riskengine_db', connect_timeout=5)
        conn.set_session(autocommit=True)
        cur = conn.cursor()

        query = '''
            SELECT timestamp, trace_id, symbol, action, mc_diagnostics
            FROM dumalka_audit_log
            WHERE timestamp > NOW() - INTERVAL '24 HOURS'
            ORDER BY timestamp DESC
        '''
        cur.execute(query)
        rows = cur.fetchall()
        
        output_path = '/opt/risk-engine/dumalka_logs_24h.md'
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('# 🧠 Логи Думалки за последние 24 часа\n\n')
            f.write(f'**Сгенерировано**: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}\n')
            f.write(f'**Всего записей**: {len(rows)}\n\n')
            f.write('---\n\n')
            
            # Group by symbol for readability
            by_symbol = {}
            for r in rows:
                ts, trace, sym, action, diag = r
                if sym not in by_symbol:
                    by_symbol[sym] = []
                by_symbol[sym].append({
                    'time': ts,
                    'trace': trace,
                    'action': action,
                    'diag': diag
                })
                
            for sym, logs in sorted(by_symbol.items()):
                f.write(f'## 🔸 {sym}\n\n')
                for log in logs:
                    f.write(f'- **Время**: `{log["time"]}`\n')
                    f.write(f'  - **Действие**: `{log["action"].upper()}`\n')
                    if log["trace"]:
                        f.write(f'  - **Trace ID**: `{log["trace"]}`\n')
                    if log["diag"]:
                        try:
                            diag_data = json.loads(log["diag"]) if isinstance(log["diag"], str) else log["diag"]
                            f.write(f'  - **Детали/Диагностика**: \n```json\n{json.dumps(diag_data, indent=2, ensure_ascii=False)}\n```\n')
                        except Exception:
                            f.write(f'  - **Детали**: `{log["diag"]}`\n')
                f.write('\n---\n\n')
                
        print(f'Done. Total records: {len(rows)}. Saved to {output_path}')
        conn.close()
    except Exception as e:
        print(f'Error: {e}')

if __name__ == '__main__':
    export_logs()
