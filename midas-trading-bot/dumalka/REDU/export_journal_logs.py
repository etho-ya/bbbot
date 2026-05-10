import subprocess
import re
from datetime import datetime, timezone

def export_journal_logs():
    output_path = '/opt/risk-engine/dumalka_detailed_logs_24h.md'
    
    # Get last 24h of risk-engine logs
    cmd = ["journalctl", "-u", "risk-engine", "--since", "24 hours ago"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print("Error fetching journal logs")
        return
        
    lines = result.stdout.splitlines()
    
    # Regex to find symbols in the log line
    symbol_regex = re.compile(r'\b([A-Z0-9]{2,10}USDT)\b')
    
    by_symbol = {}
    general_logs = []
    
    for line in lines:
        # Filter for Dumalka runtime logs
        if '[risk-engine.tracker]' not in line and 'Dumalka' not in line:
            continue
            
        # Clean up the journalctl prefix: 'Apr 01 10:14:51 rsk-eng python3[1346124]: '
        # The python logger also prepends its own timestamp: '2026-04-01 10:14:51,276 INFO ...'
        parts = line.split(']: ', 1)
        clean_line = parts[1] if len(parts) > 1 else line
            
        symbols_found = symbol_regex.findall(clean_line)
        if not symbols_found:
            general_logs.append(clean_line)
        else:
            # Add to the first matched symbol
            sym = symbols_found[0]
            if sym not in by_symbol:
                by_symbol[sym] = []
            by_symbol[sym].append(clean_line)
            
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('# 📋 Детальные системные логи Думалки (Journalctl)\n\n')
        f.write(f'**Период**: Последние 24 часа\n**Всего записей**: {len(general_logs) + sum(len(x) for x in by_symbol.values())}\n\n')
        f.write('Это полные raw-логи из systemd (stdout/stderr) за период.\n\n---\n\n')
        
        # Write per-symbol sections
        for sym, logs in sorted(by_symbol.items()):
            f.write(f'## 🔸 {sym}\n\n```text\n')
            for log in logs:
                f.write(f"{log}\n")
            f.write('```\n\n')
        
        # Write general section
        if general_logs:
            f.write('## 🌐 Охват и общие события\n\n```text\n')
            for log in general_logs:
                f.write(f"{log}\n")
            f.write('```\n\n')

    print(f"Extracted detailed logs to {output_path}")

if __name__ == '__main__':
    export_journal_logs()
