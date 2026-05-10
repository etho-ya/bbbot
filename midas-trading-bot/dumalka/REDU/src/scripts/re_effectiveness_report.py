"""
Midas Setup Master Effectiveness Analysis.
Compares Midas recommendations (structured + text) with actual trade outcomes.
Uses correct DB schema: signals + open_positions tables.
"""
import json, re, sqlite3, sys
from datetime import datetime
from collections import defaultdict

DB_PATH = 'src/data/signals.db'
REPORT_PATH = 'src/scripts/midas_effectiveness_report.md'

def classify_recommendation(setup_text):
    """Classify Setup Master text recommendation as SKIP/ENTER/NEUTRAL."""
    if not setup_text:
        return 'NO_TEXT'
    lower = setup_text.lower()
    skip_words = ['пропустить', 'не торопись', 'не входи', 'не лезь', 'не лучшая затея',
                  'лучше подожд', 'не торопи', 'лучше дождать']
    enter_words = ['действуй по классике', 'можно думать о']
    
    if any(w in lower for w in skip_words):
        return 'SKIP'
    elif any(w in lower for w in enter_words):
        return 'CAUTIOUS_ENTER'
    else:
        return 'NEUTRAL'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

cur = conn.cursor()

# Join signals with their trade results from open_positions
cur.execute("""
SELECT 
    s.id, s.symbol, s.side, s.created_at,
    s.re_signal_score, s.re_recommendation,
    s.risk_reward, s.midas_probability, s.midas_win_rate,
    s.midas_trend, s.trend_strength, s.volume_level,
    s.is_countertrend, s.setup_master_text,
    op.realized_pnl_pct, op.realized_pnl_usdt,
    op.close_reason, op.status as pos_status,
    op.entry_price, op.close_reason_detailed
FROM signals s
LEFT JOIN open_positions op ON op.signal_hash = s.signal_hash AND op.signal_hash IS NOT NULL
WHERE s.source != 'backtest'
  AND s.setup_master_text IS NOT NULL 
  AND s.setup_master_text != ''
ORDER BY s.created_at
""")
rows = [dict(r) for r in cur.fetchall()]
print(f"Signals with Setup Master text: {len(rows)}")

# Deduplicate: keep one row per signal (in case multiple positions per signal)
seen_ids = set()
signals = []
for row in rows:
    if row['id'] not in seen_ids:
        seen_ids.add(row['id'])
        row['midas_rec'] = classify_recommendation(row['setup_master_text'])
        signals.append(row)

closed = [s for s in signals if s['realized_pnl_pct'] is not None]
open_s = [s for s in signals if s['realized_pnl_pct'] is None]

print(f"Closed: {len(closed)}, Open/No-trade: {len(open_s)}")

# ─── BUILD REPORT ────────────────────────────────────────────────────
R = []
R.append("# 📊 Midas Setup Master — Анализ Эффективности Рекомендаций")
R.append(f"\n> **Дата отчёта:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
R.append(f"> **Сигналов с Setup Master текстом:** {len(signals)}")
R.append(f"> **Закрытых сделок для анализа:** {len(closed)}")
R.append(f"> **Открытых / без сделки:** {len(open_s)}\n")

# ── 1. OVERALL STATS ──
R.append("## 📈 Общая статистика по закрытым сделкам\n")
if closed:
    wins = [s for s in closed if s['realized_pnl_pct'] > 0]
    losses = [s for s in closed if s['realized_pnl_pct'] <= 0]
    avg_pnl = sum(s['realized_pnl_pct'] for s in closed) / len(closed)
    sum_pnl = sum(s['realized_pnl_pct'] for s in closed)
    
    R.append(f"| Метрика | Значение |")
    R.append(f"|---------|----------|")
    R.append(f"| Всего закрытых | {len(closed)} |")
    R.append(f"| Win / Loss | {len(wins)} / {len(losses)} |")
    R.append(f"| Win Rate | {len(wins)/len(closed)*100:.0f}% |")
    R.append(f"| Средний PnL% | {avg_pnl:.2f}% |")
    R.append(f"| Суммарный PnL% | {sum_pnl:.2f}% |")

# ── 2. MIDAS REC: SKIP vs ENTER ──
R.append("\n## 🎯 Мидас рекомендация SKIP vs ENTER vs NEUTRAL\n")
R.append("Классификация текста Setup Master:\n")
R.append("- **SKIP** — «пропустить», «не торопись», «не входи», «не лучшая затея»")
R.append("- **CAUTIOUS_ENTER** — «действуй по классике», «можно думать о»")
R.append("- **NEUTRAL** — нет явной рекомендации пропустить или войти\n")

R.append("| Рекомендация | Сделок | Win | Loss | Win Rate | Сред. PnL% | Сумм. PnL% |")
R.append("|-------------|--------|-----|------|----------|-----------|-----------|")

for rec_type in ['SKIP', 'CAUTIOUS_ENTER', 'NEUTRAL']:
    group = [s for s in closed if s['midas_rec'] == rec_type]
    if not group:
        continue
    g_wins = len([s for s in group if s['realized_pnl_pct'] > 0])
    g_losses = len(group) - g_wins
    g_avg = sum(s['realized_pnl_pct'] for s in group) / len(group)
    g_sum = sum(s['realized_pnl_pct'] for s in group)
    wr = g_wins / len(group) * 100
    R.append(f"| {rec_type} | {len(group)} | {g_wins} | {g_losses} | {wr:.0f}% | {g_avg:.2f}% | {g_sum:.2f}% |")

# ── 3. PROBABILITY BUCKETS ──
R.append("\n## 📊 Midas вероятность vs Исход\n")
R.append("| Вероятность | Сделок | Win Rate | Сред. PnL% | Сумм. PnL% |")
R.append("|------------|--------|----------|-----------|-----------|")

for lo, hi in [(0, 40), (40, 60), (60, 80), (80, 101)]:
    bucket = [s for s in closed if s['midas_probability'] is not None and lo <= s['midas_probability'] < hi]
    if bucket:
        b_wins = len([s for s in bucket if s['realized_pnl_pct'] > 0])
        b_avg = sum(s['realized_pnl_pct'] for s in bucket) / len(bucket)
        b_sum = sum(s['realized_pnl_pct'] for s in bucket)
        R.append(f"| {lo}-{hi-1}% | {len(bucket)} | {b_wins/len(bucket)*100:.0f}% | {b_avg:.2f}% | {b_sum:.2f}% |")

# ── 4. R/R BUCKETS ──
R.append("\n## 📊 Risk/Reward Ratio vs Исход\n")
R.append("| R/R | Сделок | Win Rate | Сред. PnL% |")
R.append("|-----|--------|----------|-----------|")

for lo, hi in [(0, 4), (4, 7), (7, 10), (10, 100)]:
    bucket = [s for s in closed if s['risk_reward'] is not None and lo <= s['risk_reward'] < hi]
    if bucket:
        b_wins = len([s for s in bucket if s['realized_pnl_pct'] > 0])
        b_avg = sum(s['realized_pnl_pct'] for s in bucket) / len(bucket)
        R.append(f"| {lo}-{hi} | {len(bucket)} | {b_wins/len(bucket)*100:.0f}% | {b_avg:.2f}% |")

# ── 5. COUNTERTREND ──
R.append("\n## 📊 Контртренд vs Тренд\n")
R.append("| Тип | Сделок | Win Rate | Сред. PnL% |")
R.append("|-----|--------|----------|-----------|")

for ct_val, ct_label in [(0, 'По тренду'), (1, 'Контртренд')]:
    bucket = [s for s in closed if s['is_countertrend'] == ct_val]
    if bucket:
        b_wins = len([s for s in bucket if s['realized_pnl_pct'] > 0])
        b_avg = sum(s['realized_pnl_pct'] for s in bucket) / len(bucket)
        R.append(f"| {ct_label} | {len(bucket)} | {b_wins/len(bucket)*100:.0f}% | {b_avg:.2f}% |")

# ── 6. RE SCORE BUCKETS ──
R.append("\n## 📊 RE Score vs Исход\n")
R.append("| RE Score | Сделок | Win Rate | Сред. PnL% |")
R.append("|----------|--------|----------|-----------|")

for lo, hi, label in [(0, 0.35, 'reject'), (0.35, 0.50, 'reduce'), (0.50, 0.70, 'approve'), (0.70, 1.01, 'strong')]:
    bucket = [s for s in closed if s['re_signal_score'] is not None and lo <= s['re_signal_score'] < hi]
    if bucket:
        b_wins = len([s for s in bucket if s['realized_pnl_pct'] > 0])
        b_avg = sum(s['realized_pnl_pct'] for s in bucket) / len(bucket)
        R.append(f"| {lo:.2f}-{hi:.2f} ({label}) | {len(bucket)} | {b_wins/len(bucket)*100:.0f}% | {b_avg:.2f}% |")

# ── 7. DETAILED EXAMPLES ──
sorted_closed = sorted(closed, key=lambda s: s['realized_pnl_pct'])

R.append("\n## 🔍 Детальные примеры\n")
R.append("### 🏆 Топ-5 лучших сделок\n")

for s in reversed(sorted_closed[-5:]):
    pnl_emoji = "🟢" if s['realized_pnl_pct'] > 0 else "🔴"
    rec_emoji = "⛔" if s['midas_rec'] == 'SKIP' else ("✅" if s['midas_rec'] == 'CAUTIOUS_ENTER' else "⚪")
    
    R.append(f"#### {pnl_emoji} {s['symbol']} {s['side'].upper()} — PnL: {s['realized_pnl_pct']:.2f}%\n")
    R.append(f"- **Дата:** {s['created_at'][:19]}")
    R.append(f"- **RE Score:** {s['re_signal_score']:.3f} → {s['re_recommendation']}" if s['re_signal_score'] else "- **RE Score:** n/a")
    if s['midas_probability'] is not None:
        R.append(f"- **Midas вероятность:** {s['midas_probability']:.0f}%")
    if s['risk_reward'] is not None:
        R.append(f"- **R/R:** 1 к {s['risk_reward']:.1f}")
    if s['midas_win_rate'] is not None:
        R.append(f"- **Win-Rate (месяц):** {s['midas_win_rate']:.0f}%")
    R.append(f"- **Контртренд:** {'да' if s['is_countertrend'] else 'нет'}")
    R.append(f"- **Рекомендация Midas:** {rec_emoji} {s['midas_rec']}")
    if s['close_reason_detailed']:
        R.append(f"- **Причина закрытия:** {s['close_reason_detailed']}")
    
    # Extract the key parts of setup master text
    sm = s['setup_master_text'] or ''
    sit_m = re.search(r'🧭[^:]*:\s*(.*?)(?=🧠|\Z)', sm, re.DOTALL)
    act_m = re.search(r'🧠[^:]*:\s*(.*)', sm, re.DOTALL)
    if sit_m:
        R.append(f"\n> 🧭 **Ситуация:** {sit_m.group(1).strip()[:200]}")
    if act_m:
        R.append(f">\n> 🧠 **Рекомендация:** {act_m.group(1).strip()[:200]}")
    R.append("")

R.append("### 💀 Топ-5 худших сделок\n")

for s in sorted_closed[:5]:
    pnl_emoji = "🟢" if s['realized_pnl_pct'] > 0 else "🔴"
    rec_emoji = "⛔" if s['midas_rec'] == 'SKIP' else ("✅" if s['midas_rec'] == 'CAUTIOUS_ENTER' else "⚪")
    
    R.append(f"#### {pnl_emoji} {s['symbol']} {s['side'].upper()} — PnL: {s['realized_pnl_pct']:.2f}%\n")
    R.append(f"- **Дата:** {s['created_at'][:19]}")
    R.append(f"- **RE Score:** {s['re_signal_score']:.3f} → {s['re_recommendation']}" if s['re_signal_score'] else "- **RE Score:** n/a")
    if s['midas_probability'] is not None:
        R.append(f"- **Midas вероятность:** {s['midas_probability']:.0f}%")
    if s['risk_reward'] is not None:
        R.append(f"- **R/R:** 1 к {s['risk_reward']:.1f}")
    R.append(f"- **Контртренд:** {'да' if s['is_countertrend'] else 'нет'}")
    R.append(f"- **Рекомендация Midas:** {rec_emoji} {s['midas_rec']}")
    if s['close_reason_detailed']:
        R.append(f"- **Причина закрытия:** {s['close_reason_detailed']}")
    
    sm = s['setup_master_text'] or ''
    sit_m = re.search(r'🧭[^:]*:\s*(.*?)(?=🧠|\Z)', sm, re.DOTALL)
    act_m = re.search(r'🧠[^:]*:\s*(.*)', sm, re.DOTALL)
    if sit_m:
        R.append(f"\n> 🧭 **Ситуация:** {sit_m.group(1).strip()[:200]}")
    if act_m:
        R.append(f">\n> 🧠 **Рекомендация:** {act_m.group(1).strip()[:200]}")
    R.append("")

# ── 8. KEY INSIGHTS ──
R.append("\n## 💡 Ключевые выводы\n")

skips = [s for s in closed if s['midas_rec'] == 'SKIP']
non_skips = [s for s in closed if s['midas_rec'] != 'SKIP']

if skips and non_skips:
    skip_avg = sum(s['realized_pnl_pct'] for s in skips) / len(skips)
    non_skip_avg = sum(s['realized_pnl_pct'] for s in non_skips) / len(non_skips)
    skip_wr = len([s for s in skips if s['realized_pnl_pct'] > 0]) / len(skips) * 100
    non_skip_wr = len([s for s in non_skips if s['realized_pnl_pct'] > 0]) / len(non_skips) * 100
    skip_sum = sum(s['realized_pnl_pct'] for s in skips)
    
    R.append(f"| | Midas: SKIP | Midas: ENTER/NEUTRAL |")
    R.append(f"|--|------------|---------------------|")
    R.append(f"| Сделок | {len(skips)} | {len(non_skips)} |")
    R.append(f"| Win Rate | {skip_wr:.0f}% | {non_skip_wr:.0f}% |")
    R.append(f"| Средний PnL% | {skip_avg:.2f}% | {non_skip_avg:.2f}% |")
    R.append(f"| Суммарный PnL% | {skip_sum:.2f}% | {sum(s['realized_pnl_pct'] for s in non_skips):.2f}% |")
    R.append("")
    
    if skip_avg < non_skip_avg:
        R.append(f"> [!IMPORTANT]")
        R.append(f"> **Мидас прав:** сделки с рекомендацией SKIP показали худший PnL ({skip_avg:.2f}% vs {non_skip_avg:.2f}%).")
        R.append(f"> Если бы мы фильтровали SKIP-сигналы, сэкономили бы **{abs(skip_sum):.2f}%** суммарного PnL.")
    else:
        R.append(f"> [!WARNING]")
        R.append(f"> **Мидас ошибался:** SKIP-сигналы дали **лучший** PnL ({skip_avg:.2f}%) чем ENTER/NEUTRAL ({non_skip_avg:.2f}%).")
        R.append(f"> Текстовые рекомендации Midas не коррелируют с исходом — использовать как фильтр нецелесообразно.")

# What structured metrics actually predict outcomes?
R.append("\n\n### Какие структурированные метрики Midas предсказывают исход?\n")
R.append("*(Уже собираются в БД: `midas_probability`, `risk_reward`, `midas_win_rate`, `trend_strength`, `volume_level`, `is_countertrend`)*\n")

# Probability correlation
if closed:
    has_prob = [s for s in closed if s['midas_probability'] is not None]
    if has_prob:
        hi_prob = [s for s in has_prob if s['midas_probability'] >= 60]
        lo_prob = [s for s in has_prob if s['midas_probability'] < 40]
        if hi_prob and lo_prob:
            hi_avg = sum(s['realized_pnl_pct'] for s in hi_prob) / len(hi_prob)
            lo_avg = sum(s['realized_pnl_pct'] for s in lo_prob) / len(lo_prob)
            R.append(f"- **Midas вероятность ≥60%:** {len(hi_prob)} сделок, avg PnL = {hi_avg:.2f}%")
            R.append(f"- **Midas вероятность <40%:** {len(lo_prob)} сделок, avg PnL = {lo_avg:.2f}%")
            if hi_avg > lo_avg:
                R.append(f"  - ✅ Вероятность предсказывает исход (разница {hi_avg - lo_avg:.2f}%)")
            else:
                R.append(f"  - ❌ Вероятность НЕ предсказывает исход")

R.append("")

# Write
report_text = '\n'.join(R)
with open(REPORT_PATH, 'w') as f:
    f.write(report_text)

print(f"\nReport saved to {REPORT_PATH}")
print(f"Report: {len(report_text)} chars, {len(R)} lines")
conn.close()
