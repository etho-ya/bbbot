# Dumalka — Next Steps, Hypotheses & Strategic Roadmap

*Last updated: 2026-04-10 — v0.19.6.1: Close-event fix (VALID_EVENTS gate + /trade-outcome sync + Dmitry PR merge). 40/40 tests passed. Tech debt: 16 analytics SQL queries need new event types. Previous: ML Shadow Mode, scout spike tuning.*

Живой документ. Обновляется при каждом значимом анализе.

---

## 0. Видение продукта

```
┌────────────────────────────────────────────────────────────────────┐
│                   AUTONOMOUS TRADING SYSTEM                        │
│                                                                    │
│  1. РАЗВЕДКА (Скаут)    Сканирует рынок → 8 типов сигналов        │
│  2. ВХОД (Снайпер)      Оценивает с MC GPU, скоринг 7 компонент   │
│  3. УПРАВЛЕНИЕ (Думалка) 5-зонная политика + MC + SL Cap          │
│  4. АНАЛИЗ (Мозг)       ML обучается на каждой сделке             │
│                                                                    │
│  KPI: максимизировать capture ratio при минимальных потерях        │
└────────────────────────────────────────────────────────────────────┘
```

| Этап | Что умеет | Статус |
|------|----------|--------|
| **v2: Фильтр** | Risk Engine оценивает сигналы, отсекает плохие | ✅ WR 63%+ (approve) |
| **v3: Наблюдатель** | Думалка мониторит позиции, собирает данные | ✅ 103K+ снимков |
| **v4: Управляющий** | Думалка фиксирует прибыль, двигает SL | ✅ Active Mode LIVE |
| **v4.5: Аудит + ML Pipeline** | Audit Log, Exit Quality, ML Labeler, Scout | ✅ v0.18.6–v0.19.6 |
| **v5: Умный вход** | Анализ Setup Master, лимитки у уровней | ⏳ После checkpoint 21 апр |
| **v6: Автономный поиск** | Scout генерирует сигналы без Midas | ⏳ При WR Scout ≥ 55% |
| **v7: Полная автономия** | Поиск → вход → управление → обучение | 🎯 Финальная цель |

**Сейчас: v4.5 ACTIVE + v0.19.6.** 103K+ снимков, 34 символа, Scout (8 типов), ML Shadow Mode, Hard SL Cap 3.5%.

---

## 1. Ключевые метрики торговли

| Метрика | Значение |
|---------|----------|
| Win Rate (approve, Apr 2+) | **63%+** |
| Avg PnL SHORT | **+0.57%** (45 сделок, total +25.8%) |
| Avg PnL LONG | **+0.02%** (53 сделки, total +1.0%) |
| Hard SL Cap | 3.5% ACTIVE с 04.04.2026 |
| ML Shadow LOO AUC | 0.574 (prod seed) / 0.621 (EXP-7) |
| position_snapshots | 103K+ (34 колонки, 34 символа) |
| Kline coverage | 34 символа × 3 TF (15m/1h/4h) |

> **H6 вывод (требует подтверждения):** Вся прибыль генерируется SHORT-сделками. LONG — break-even. Checkpoint 21 апреля при N >= 100 LONG.

---

## 2. Активные гипотезы (ожидают подтверждения данными)

### H1: Triple Compression — ранние выходы для SIREN/NOM/STO
**Добавлена:** 2026-04-07 | **Статус:** ОЖИДАЕТ ДАННЫХ (checkpoint 21 апреля)

Три независимых модификатора стекаются мультипликативно:
- `adaptive_factor` = 0.5 (floor, т.к. p_tp/p_sl << 1 для высоковол активов)
- `vol_modifier` = 0.8 (т.к. volume_ratio < 0.5 у альткоинов с низкой абс. ликвидностью)
- `regime_dd_sensitivity` = 0.85-1.2

Итог: Zone 3 base 15% → adjusted 5.1–7.2% (сжатие 66–77%). Root cause ранних zone_full_exit.

**Counterfactual (N=4):** 4 post-HSC SIREN zone exits — 1 TOO_EARLY (#611, +14.1% через 1h), 3 CORRECT. Для H1a (af 0.5→0.6): 1 WIN, 0 HARM — недостаточно.

**Критерий закрытия:** N >= 10 zone_full_exit с vol >= 4.0 в enriched audit log.

```sql
SELECT
  (d.mc_diagnostics::json->>'volatility')::float AS vol,
  (d.mc_diagnostics::json->>'af')::float AS af,
  (d.mc_diagnostics::json->>'vol_mod')::float AS vol_mod,
  (d.mc_diagnostics::json->>'thresh')::float AS thresh,
  (d.mc_diagnostics::json->>'base_thresh')::float AS base_thresh,
  w.pnl_1h_after, w.max_pnl_24h
FROM dumalka_audit_log d
JOIN open_positions p ON p.id = d.pos_id
LEFT JOIN what_if_outcomes w ON w.pos_id = p.id
WHERE d.action = 'full_close'
  AND d.timestamp >= '2026-04-07'
  AND p.close_reason = 'zone_full_exit'
  AND (d.mc_diagnostics::json->>'volatility')::float >= 4.0
ORDER BY d.timestamp;
```

**Варианты фикса (только при подтверждении):**
- A) af floor 0.5 → 0.6 для vol >= 4.0
- B) Exempt vol >= 4.0 от vol_modifier
- C) Combined floor: `max(adjusted_threshold, base_thresh * 0.35)` для vol >= 4.0

---

### H2: Artifact Recovery в ML Labeler — ложные "hold" метки
**Добавлена:** 2026-04-05 | **Статус:** ОЖИДАЕТ ДАННЫХ (re-check ~14 апреля)

`recovery-hold` логика: `if future_pnl_max_24h >= 3% and drawdown <= MAX_LOSS_CAP → label = "hold"`.  
Проблема: выполняется если цена восстановилась от -1% к +3% (разворот) — это "artifact recovery", не ракета.

**Гипотеза:** Настоящий recovery требует `future_pnl_max_24h - drawdown > 1.0`.

**Критерий проверки:**
```sql
SELECT COUNT(*) FROM position_snapshots
WHERE optimal_action = 'hold'
  AND drawdown_pct < -1.0
  AND future_pnl_max_24h >= 3.0
  AND (future_pnl_max_24h - abs(drawdown_pct)) <= 1.0;
```

**Действие при подтверждении:** изменить условие в `label_optimal_actions.py`.

---

### H3: RIVER LONG — системно убыточное направление
**Добавлена:** 2026-04-07 | **Исправлена:** 2026-04-08 (предыдущая версия содержала ошибку в направлении)  
**Статус:** ОЖИДАЕТ ДАННЫХ (N >= 20 RIVER LONG)

RIVER stats с 27.03 (перепроверено 08.04.2026):
- **RIVER LONG: 8 сделок, 3 победы (38%), sum -17.95%** — убыточно
  - Без outlier #423: sum = -2.08%
- **RIVER SHORT: 13 сделок, 10 побед (77%), sum +13.19%** — прибыльно

Причина: при TRANSITIONAL vol (2.2–3.4) RIVER склонен к резким отскокам против LONG.

**Важно:** 08.04 все 4 сделки RIVER убыточны (3x HSC) — аномалия высокой волатильности рынка, не паттерн для LONG.

**Критерий:** N >= 20 RIVER LONG trades OR win_rate < 30% → повышение score threshold для RIVER LONG.

---

### H4: pnl_skewness как детектор ракеты — не подтверждена
**Добавлена:** 2026-04-06 | **Статус:** НЕДОСТАТОЧНО ДАННЫХ (пассивный мониторинг)

Гипотеза: высокая `pnl_skewness` предсказывает продолжение ракеты.  
Опровержение: #611 (TOO_EARLY, skew=1.68) vs #613 (CORRECT, skew=1.31) — разница мала.  
**Действие:** Накапливаем данные. Re-check: 21 апреля.

---

### H5: Scout volume filter слишком строгий
**Добавлена:** 2026-04-06 | **Статус:** НАБЛЮДЕНИЕ, не критично

`volume < 80% avg_vol_24h` → сигнал не генерируется. В периоды низкого рынка большинство символов не проходят.  
**Альтернатива:** `vol_threshold = 0.8 * median_vol_last_7d` (динамический).  
**Действие:** Пассивный мониторинг. Re-check: когда Scout покажет пропущенные прибыльные сигналы.

---

### H6: LONG системно слабее SHORT — глобальный паттерн
**Добавлена:** 2026-04-08 | **Статус:** ОЖИДАЕТ ДАННЫХ (N >= 100 LONG, checkpoint 21 апреля)

ML EXP-3/4: `side_is_long = -0.36` — сильнейший коэффициент LogReg. На 98 позициях:
- **SHORT**: 45 сделок, WR **73.3%**, avg PnL **+0.57%**, total **+25.8%**
- **LONG**: 53 сделки, WR **54.7%**, avg PnL **+0.02%**, total **+1.0%**

Вся прибыль системы — SHORT. LONG на уровне break-even.

**Возможные причины:** медвежий рынок Apr 2+, BTC-корреляция альткоинов, tight SL на downside.  
**Риск:** если паттерн ситуационный — менять систему не нужно.  
**Критерий:** N >= 100 LONG сделок с Apr 2+, split по market regime.

---

## 3. Закрытые гипотезы (проверены)

### C1: Adaptive factor floor 0.5 вреден для SIREN ✓ ОПРОВЕРГНУТА
**Закрыта:** 2026-04-06 — af=0.5 ЗАЩИЩАЕТ от потерь в 2 из 3 случаев. Текущий floor оставлен.

### C2: ATR Soft BE для TP1 был сломан ✓ ПОДТВЕРЖДЕНА И ИСПРАВЛЕНА
**Закрыта:** 2026-04-05 (v0.19.4) — формула всегда возвращала 0.5% из-за cap. Исправлено на ATR-адаптивную 0.5–3.0% + skip при tp1_dist < 2×ATR. Validated: 7/7 случаев, +21.9% PnL.

### C3: RIVER/CUSDT нуждаются в более широких порогах как SIREN ✓ ОПРОВЕРГНУТА
**Закрыта:** 2026-04-07 — RIVER/CUSDT при высокой vol показывают ХУДШИЕ результаты (RIVER: -0.51% vs +0.81%). Расширение только для vol >= 4.0 (SIREN/NOM/STO).

### C4: Bot sync desync — причина phantom-закрытий ✓ ПОДТВЕРЖДЕНА И ИСПРАВЛЕНА
**Закрыта:** 2026-04-05 (v0.19.3) — 6 из 11 "ракет" — phantom exits. После фикса Telegram proxy 0 phantom exits.

### C5: Entry snapshot selection + MC sentinel data ✓ ПОДТВЕРЖДЕНА И ИСПРАВЛЕНА
**Закрыта:** 2026-04-08 (v0.19.5-fix) — три бага:
- `MIN(id)` → `DISTINCT ON ... WHERE snapshot_at >= opened_at`
- MC sentinel values (p_sl=1.0) до 01.04: 22,509 снапшотов исключены фильтром `(p_tp + p_sl) <= 1.01`
- Volatility запрос некорректно применял MC-фильтр (удалён)

С 08.04: `position_snapshots` записывает `current_sl/tp1/tp3` для воспроизводимости MC.

### C6: Scout spike_consolidation_breakout порог слишком высок ✓ ПОДТВЕРЖДЕНА И ИСПРАВЛЕНА
**Закрыта:** 2026-04-09 — backtest на 34 символах (548+ свечей 1h):

| Порог | Сигналов | WR @4h | Avg PnL @4h |
|-------|----------|--------|-------------|
| 3.0 (было) | 32 | 34% | **-0.5%** |
| **2.5 (стало)** | **47** | **36%** | **+2.1%** |
| 2.0 (отклонён) | 76 | 39% | +1.4% |

Причина: spike 3.0x ATR ловит "выгоревшие" импульсы, а 2.5x — реальные вторые волны.
SIREN-ракета 04-04 (spike=2.76x, +107% за 4h) — пропускалась при 3.0, поймана при 2.5.
Изменение: `_SPIKE_ATR_MULT = 3.0 → 2.5` в `scout.py`. Shadow-only, zero prod risk.

**Мониторинг (checkpoint 21 апреля):**
```sql
-- Проверить реальный исход новых сигналов (2.5x, т.е. spike < 3.0x ATR):
SELECT signal_type, COUNT(*) as n,
       ROUND(AVG(CASE WHEN shadow_pnl_1h > 0 THEN 1.0 ELSE 0.0 END)*100, 1) AS wr_1h,
       ROUND(AVG(shadow_pnl_1h)::numeric, 3) AS avg_pnl_1h,
       ROUND(AVG(shadow_pnl_4h)::numeric, 3) AS avg_pnl_4h
FROM scout_signals
WHERE signal_type = 'spike_consolidation_breakout'
  AND created_at >= '2026-04-09'
GROUP BY signal_type;
```
Критерий пользы: **WR >= 40% и avg_pnl_1h > 0** при N >= 5 сигналов.
Критерий отката: **avg_pnl_1h < -1%** при N >= 5 → вернуть `_SPIKE_ATR_MULT = 3.0`.

### TD1: Analytics SQL queries — hardcoded close-event lists (v0.19.6.1 tech debt)
**Обнаружен:** 2026-04-10 при ревью PR Дмитрия.
**ЗАКРЫТ:** 2026-04-12 — исправлено все 14 SQL-запросов в `main.py` (9), `db.py` (5), `watchlist_scanner.py` (1).
**Эффект:** 22 сделки (manual_close=18, dumalka_close=4) теперь включены в аналитику.
Avg PnL изменился: `-0.25%` (355 trades, старый фильтр) → `+0.04%` (377 trades, полный фильтр).
Включены события: `dumalka_close`, `manual_close`, `flip_close` + `tp3_hit` восстановлен в `get_pending_opportunity_costs`.

---

## 4. ML Shadow Mode — DEPLOYED v0.19.6 (2026-04-08)

**Цель:** Валидация ML-предсказаний на реальных out-of-sample данных без влияния на торговлю.

**Реализация:**
- Модель: `ExtraTreesClassifier` + Optuna×200 (5-fold CV), 98 позиций (Apr 2+)
- Файл модели: `src/models/et_shadow_v1.pkl` (27 KB)
- Загрузка при старте: `main.py startup` → `position_tracker._ml_shadow_model`
- Предсказание: после 5+ снапшотов (~2.5 мин), однократно на позицию
- Запись: `ml_predictions` (pos_id, prob_profit, prediction, features_json)
- API: `GET /api/ml-shadow`
- Config: `ML_SHADOW_ENABLED=true`, `ML_SHADOW_CONFIDENCE_THRESHOLD=0.65`

**Production метрики модели:**
- LOO AUC: 0.5735 (production seed), Inner CV AUC: 0.6103
- Best params: n_estimators=202, max_depth=8, min_samples_leaf=20

**Ожидаемый результат:**
- До 21 апреля: N >= 100 out-of-sample предсказаний
- Метрика успеха: AUC >= 0.55 на новых данных
- Метрика провала: AUC < 0.52 = не лучше монетки

```sql
-- Проверка out-of-sample performance:
SELECT mp.prediction, mp.prob_profit,
       p.realized_pnl_pct,
       CASE WHEN p.realized_pnl_pct > 0 THEN 'profit' ELSE 'loss' END AS actual
FROM ml_predictions mp
JOIN open_positions p ON p.id = mp.pos_id
WHERE p.status = 'closed'
ORDER BY mp.created_at;
```

---

## 5. ML-эксперименты (журнал)

### EXP-1: Snapshot-level classifier — 2026-04-08
**Данные:** 15,132 снапшотов | **Модель:** LightGBM GroupKFold-5 | **AUC:** 0.565 (Baseline 0.802)  
**Вывод:** Дисбаланс 80/20 + 101 позиция = недостаточно. Feature importance: volatility, hours_open, mc_p_tp.

### EXP-2: Position-level classifier + regression — 2026-04-08
**Данные:** 98 позиций (Apr 2+) | **Модели:** LightGBM binary + regression, LOO CV  
**AUC:** 0.532 (baseline 0.633). Уверенные предсказания prob > 0.8: 26/38 правильных (68%).  
**Топ фичи:** early_min_pnl, early_pnl_std, entry_rsi, entry_signal_score.

### EXP-3: LightGBM vs XGBoost vs LogReg — 2026-04-08
**Лучший AUC:** LogReg L2 = 0.578. При confidence >70%: LogReg 75% win rate (16 сделок).  
**Ключевая находка:** `side_is_long = -0.36` — сильнейший коэффициент → H6.  
**Profit factors:** entry_rsi (+0.26), entry_trend (+0.18), early_avg_pnl (+0.17).

### EXP-4: Full Model Zoo — 2026-04-08
**Модели:** LightGBM, XGBoost, CatBoost, LogReg, RandomForest, SVM, Ridge, Stacking, SoftVoting  
**Рейтинг:** LogReg L2 (0.578) = CatBoost (0.577) > RandomForest (0.568). SVM/Ridge — AUC < 0.5, бесполезны.  
**Лучший filter:** RandomForest @ 0.65 → +1.15% avg PnL, 76.5% win, 34 сделки.  
**SHORT vs LONG подтверждено:** SHORT +25.8% total vs LONG +1.0%.

### EXP-5: HistGBM + ExtraTrees — 2026-04-08
**Новый лидер:** ExtraTrees AUC **0.606**, Brier 0.235.  
**ET @ 0.65:** 87% win rate (15 сделок), avg PnL +1.20%.  
**ET @ 0.70:** 83% win (6 сделок), avg PnL +2.09%.  
HistGBM слабый при N=98.

### EXP-6: TabICL + FLAML + GaussianProcess + Optuna-ET — 2026-04-08
**ET+Optuna @ 0.75:** 92% win (13 сделок). **@ 0.80: 100% win (9 сделок)** — первый раз.  
TabICL (AUC 0.497), GaussianProcess (0.469), FLAML (0.431) — все хуже базовой ET при N=98.  
Optuna переструктурировал вероятности, не изменив AUC.

### EXP-7: GPU Neural Nets + Optuna×200 — 2026-04-08 🏆
**Итоговый рейтинг:**

| # | Модель | AUC | Brier |
|---|--------|-----|-------|
| **1** | **ET+Optuna×200** | **0.621** | **0.231** |
| 2 | ExtraTrees (base) | 0.606 | 0.235 |
| 3 | LogReg L2 | 0.578 | 0.255 |
| 4 | CatBoost | 0.577 | 0.275 |
| 5 | FT-Transformer (GPU) | 0.560 | 0.315 |
| 6 | TabNet (GPU) | 0.479 | 0.267 |

**ET+Optuna×200 @ 0.80:** 9 сделок, **100% win rate**, avg PnL +1.96%.  
**GPU нейросети нужны 1K+ строк.** TabNet: `corrected_approve` = feature #1 (деревья ставили на #19).

**Постоянные выводы (все 7 экспериментов):**
- ExtraTrees + Optuna × 200 лучший при малом N (AUC 0.621)
- Foundation models (TabICL, TabPFN, TabM, FT-Transformer) = нужно 300+ позиций
- GPU бесполезен для GBDT при N=98

---

## 6. Следующий прогон: ~21 апреля 2026

**Условие:** N >= 200 позиций (закрытых с Apr 2+)

| Задача | Условие |
|--------|---------|
| **ET+Optuna×200** — повторить | N >= 200 |
| **ML Shadow validation** — AUC на out-of-sample | N >= 100 ml_predictions закрытых |
| **TabICL** — перезапустить | N >= 300 |
| **TabM** — исправить API | N >= 200 |
| `corrected_approve` TreeSHAP analysis | — |

**Запуск:**
```bash
cd /opt/risk-engine/src
python3 scripts/train_shadow_model.py        # переобучить модель
python3 scripts/ml_experiment_v7.py          # основной: ET+Optuna×200 + GPU neural nets
python3 scripts/ml_experiment_v4.py          # full zoo для сравнения
```

---

## 7. Запланированные checkpoints

| Дата | Тема | Минимум данных |
|------|------|----------------|
| **21 апреля 2026** | H1 Triple Compression — Phase 2 decision | N >= 10 zone_full_exit с vol >= 4.0 |
| **21 апреля 2026** | H4 pnl_skewness rocket detection | N >= 20 zone_full_exit в enriched audit |
| **21 апреля 2026** | **ML EXP-8** — ET+Optuna повтор + TabICL + TabM | N >= 200 closed, N >= 300 для TabICL |
| **21 апреля 2026** | **H6 LONG vs SHORT** — подтвердить | N >= 100 LONG trades с Apr 2+ |
| **21 апреля 2026** | **ML Shadow validation** — AUC out-of-sample | N >= 100 ml_predictions закрытых |
| **14 апреля 2026** | H2 Artifact recovery labeler | SQL проверку, оценить count |
| **по данным** | H3 RIVER LONG signal filtering | N >= 20 RIVER LONG trades |

---

## 8. Техдолг

| Задача | Приоритет | Версия |
|--------|-----------|--------|
| Phase 2: conditional DD threshold для vol >= 4.0 (H1) | HIGH | ~v0.19.7 после checkpoint |
| Artifact recovery fix в ML labeler (H2) | MEDIUM | ~v0.19.7 |
| RIVER LONG score threshold (H3) | LOW | по данным |
| Scout dynamic volume filter (H5) | LOW | по данным |
| Backfill `what_if_outcomes` из Bybit klines (не OKX) | MEDIUM | отдельная задача |
| ML Shadow EXP-8 при N >= 200 | HIGH | 21 апреля |

---

## 9. 3-Tier Volatility Classification (validated v0.19.5)

| Tier | Vol range | Символы | Поведение | Threshold impact |
|------|-----------|---------|-----------|------------------|
| DEFINITE HIGH | >= 4.0 | SIREN, NOM, STO | Rockets possible | Triple compression: af×vol_mod×regime → 66-77% reduction |
| TRANSITIONAL | 2.0-4.0 | RIVER, CUSDT, KERNEL | Oscillate. HIGH phase = danger | Текущие пороги корректны |
| NORMAL | < 2.0 | Все остальные (19 символов) | Стабильны | Работают как задумано |

Ключевая находка: RIVER/CUSDT ХУЖЕ при высокой vol (RIVER: -0.51% vs +0.81%, CUSDT: -2.95%).  
Phase 2 threshold changes — ТОЛЬКО для vol >= 4.0. Checkpoint 21 апреля.

```sql
-- Analyze zone_full_exit by volatility tier (v0.19.5 enriched diagnostics)
SELECT
  CASE WHEN (d.mc_diagnostics::json->>'volatility')::float >= 4.0 THEN 'HIGH'
       WHEN (d.mc_diagnostics::json->>'volatility')::float >= 2.0 THEN 'TRANS'
       ELSE 'NORMAL' END AS tier,
  COUNT(*) AS n,
  ROUND(AVG((d.mc_diagnostics::json->>'af')::float), 3) AS avg_af,
  ROUND(AVG((d.mc_diagnostics::json->>'vol_mod')::float), 3) AS avg_vol_mod
FROM dumalka_audit_log d
WHERE d.action = 'full_close' AND d.timestamp >= '2026-04-07'
GROUP BY 1;
```

---

## 10. Зонная политика (Zone Policy v0.18.9)

| Зона | TP Progress | Порог DD от max_pnl | Действие | Фракция |
|------|-------------|---------------------|----------|---------|
| **0** (начало) | 0–5% | — | Ничего, обычный SL | 0% |
| **1** (early profit) | 5–20% | >40% | Hold (v0.18.9: было sl_breakeven) | 0% |
| **2** (защита) | 20–40% | >30% | Частичная фиксация | 25% |
| **3** (фиксация) | 40–70% | >25% | Основная фиксация | 55% |
| **4** (максимум) | 70%+ | >15% | Почти полная фиксация | 90% (dynamic 5-30% moonbag) |

Hard SL Cap 3.5% ACTIVE с v0.18.8 — срабатывает независимо от зоны.  
ATR-adaptive TP1 Soft BE: skip когда tp1_dist < 2×ATR(1h) — v0.19.4.

---

## 11. ML Reality Check (baseline, Apr 4, 2026)

| Метрика | Значение |
|---------|----------|
| Snapshots (Mar 27+) | 34,284 total, 33,659 labeled (98.2%) |
| Label distribution | hold 79.6%, close 16.6%, partial_close 3.8% |
| Feature coverage | 100% core, 86% intelligence (Mar 29+), 100% MC Reform (Apr 2+) |
| Market regimes seen | normal 9d, ranging 7d, trending 2d, volatile 1d |
| XGBoost v3 assessment | Accuracy 81.8%, zone-baseline 93.3% — **NOT ready for production** |

Приоритеты:
```
СЕЙЧАС     → Собираем данные. ML Shadow Mode логирует. Ничего не трогаем.
~21 апр    → EXP-8 + ML Shadow validation при N >= 200
~May 1+    → Meta-Labeling M2 (XGBoost как "фильтр решений Думалки")
~May-June  → Offline RL prototype (CQL на d3rlpy) при 50K+ golden rows
```

---

## 12. Стратегический Roadmap (Phase 5–7)

| Фаза | Задача | Статус |
|------|--------|--------|
| **Phase 5** | Regression model (predict future_pnl_1h continuous) | ⏳ ~May 1 |
| **Phase 5** | Triple Barrier labels (adaptive SL/TP/timeout) | ⏳ ~May |
| **Phase 5** | Meta-Labeling M2 — XGBoost filters Dumalka decisions | ⏳ ~May 1 |
| **Phase 5** | ML Shadow → Production (если AUC >= 0.58 на 200+ trades) | ⏳ ~21 апр |
| **Phase 6** | Scout WR validation — когда shadow WR >= 55% | ⏳ |
| **Phase 6** | Scout → Real Signals (параллельно с Midas, shadow) | ⏳ |
| **Phase 6** | Offline RL Agent (CQL/Decision Transformer) | ⏳ ~May-June |
| **Phase 7** | Полная автономия без Midas | 🎯 |

---

*Примечание: Этот файл объединяет `dumalka_next_steps.md` (рабочие гипотезы и ML-эксперименты) и `DUMALKA_NEXT_STEPS.md` (стратегическое видение и продуктовый roadmap). Слияние: 2026-04-09 v0.19.6.*
