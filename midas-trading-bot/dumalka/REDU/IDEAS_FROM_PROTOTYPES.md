# Архитектурные идеи из прототипов (v0.9.x)

> Извлечены из 5 `*_v2_prototype.py` файлов перед архивацией.
> Файлы доступны в `src/_archive/` для детального изучения.

---

## 1. Position Tracker → Domain-Driven Design (из `position_tracker_v2_prototype.py`)

**Идея**: Разделить монолитный цикл (`track_open_positions`, 950+ LOC) на 4 сервиса:
- `MarketDataService` — запросы к Bybit/proxy
- `RiskEvaluator` — математика + GPU Monte Carlo
- `DecisionEngine` — зонная политика + ML inference
- `ExecutionService` — команды боту + аудит

**Ценность**: Позволит тестировать каждый компонент изолированно. Критично для ML integration — `DecisionEngine` должен принимать предсказания модели без пересборки всей думалки.

**Когда внедрять**: Когда ML модель v2 покажет >30% close recall и будет готова к live интеграции.

---

## 2. Scoring → Pluggable Strategy Pattern (из `scoring_v2_prototype.py`)

**Статус**: ✅ **УЖЕ РЕАЛИЗОВАНО** как `scoring_v2.py` (Strategy Pattern с `set_active_strategy()`).

---

## 3. DB → Repository Pattern (из `db_v2_prototype.py`)

**Идея**: Обёрнуть прямые SQL-запросы в Repository-классы:
- `SignalRepository.get_by_hash()`, `PositionRepository.get_open()`
- Type-safe результаты (Pydantic модели вместо `Dict[str, Any]`)
- Единая точка для кэширования и query optimization

**Ценность**: Снизит кол-во дублированного SQL (~15 мест в `main.py` содержат inline SQL).

**Когда внедрять**: При рефакторинге `main.py` (2,597 LOC → разбиение на роутеры).

---

## 4. Main → FastAPI Router Split (из `main_v2_prototype.py`)

**Идея**: Разбить `main.py` (2,597 LOC) на:
- `routers/signals.py` — webhook, scoring
- `routers/positions.py` — position management endpoints
- `routers/analytics.py` — analytics API
- `routers/admin.py` — health, config, control

**Ценность**: Чистая структура, лёгкий review, горизонтальная масштабируемость.

**Когда внедрять**: При следующем крупном рефакторинге или добавлении >3 новых endpoints.

---

## 5. Telegram Bridge → Event-Driven (из `telegram_bridge_v2_prototype.py`)

**Идея**: Заменить цепочку `if/elif` парсеров на Event Bus:
- `SignalParsedEvent`, `TradeExecutedEvent`, `PositionClosedEvent`
- Подписчики: scorer, position_tracker, daily_report, notifications

**Ценность**: Устраняет tight coupling между парсером и бизнес-логикой.

**Когда внедрять**: Когда добавляем 2+ новых источника сигналов (не только Midas).

---

## 6. WebSocket-Based SL→BE Trigger (из v0.15.1 — `core/bybit_ws.py`)

**Идея**: Использовать Bybit WS ticker stream (~1с латентность) для мгновенного SL→BE вместо 30с REST polling.

**Архитектура**:
```
WS ticker (каждый тик) → быстрая проверка: pnl > 0 И SL не на BE?
  → ДА: отправить move_sl
  → НЕТ: skip (0 CPU cost)
```

**Ценность**: Ловим SL→BE окно, которое при 30с цикле может быть пропущено. Из аудита: 12 позиций с peak > 0.5% всё равно ушли в SL.

**Риск**: Слишком быстрая реакция на шум. Нужен **confirmation period** (цена > порога ≥5-10с подряд).

**Когда внедрять**: После 1-2 недель Shadow Mode (подтвердили стабильность WS), если аудит покажет что 30с цикл всё ещё упускает SL→BE окна.

---

## 7. Forensic Insight: move_sl Fix = Largest Single PnL Improvement (28.03.2026)

**Факт**: move_sl success rate вырос с **9.5% → 91.6%** после фикса интеграции (conversation 82fcc070).

**Корневая причина**: Бот ожидал `newSl`, RE отправлял `new_sl`. Одна буква → 91% fail rate.

**Вывод для будущих интеграций**: Обязательный E2E validation при любых изменениях в dispatch schema. Добавить automated integration tests: mock bot → verify payload format.

**Гипотеза**: Оставшиеся ~8% failures — скорее всего orderbook liquidity issues (Bybit отклоняет SL слишком близко к текущей цене). Нужен буфер ≥ min_tick_size.

---

## 8. Partial Close Gate: Position Size Matters (29.03.2026 — Apollo Audit)

**Факт**: 99.96% partial_close failures (6,737 из 6,740) вызваны тем, что 30% от позиции < $5 (Bybit min notional).

**Решение (v0.15.0)**: `MIN_PARTIAL_CLOSE_USD=17` — ниже этого порога используется full_close.

**Вывод для масштабирования**: При росте депозита до $200+ этот gate автоматически перестанет активироваться. Partial close заработает "бесплатно" когда позиции станут больше.

---

## 9. TimescaleDB — Когда Переходить (29.03.2026 — Анализ БД)

**Текущие размеры**: 40 MB total, 32 MB snapshots. PostgreSQL справляется за микросекунды.

**TimescaleDB НЕ нужен сейчас** — наши проблемы от зависших psql-сессий, а не от объёма.

**TimescaleDB ПОНАДОБИТСЯ** когда:
- Начнём хранить WS tick data (23 символа × ~50K строк/час = 1.2M/день)
- Compression (10-20×) станет критична
- Continuous aggregates заменят ручной `analytics_cache`

**Trigger**: position_snapshots > 1M строк ИЛИ запуск tick data storage.

---

## 10. Кати: "Никакого Частичного Закрытия в Минусе" (29.03.2026)

**Цитата**: "частичное закрытие - когда ты в плюсе и часть фиксируешь, никакого частичного закрытия в минусе быть не может - это бред"

**Реализовано в v0.14.4**: Apollo Strict Profit Rule (`current_pnl_pct > 0.0`).

**Гипотеза для ML**: Constraint `action != partial_close IF pnl < 0` должен быть hardcoded в action space RL-агента (Phase 5), а не learned — это бизнес-правило, не паттерн.

