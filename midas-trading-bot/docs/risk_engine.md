# Risk Engine — Titan V, TradingView/Midas, Validation v3.0

## 0. Общая идея

Цель системы:
- Использовать Titan V как FP64‑ускоритель для риск‑модели (Monte Carlo VaR/CVaR).
- Принимать реальные сигналы с TradingView/Midas, переоценивать их качество (наш scoring + Monte Carlo).
- Логировать всё в БД и строить аналитику.
- В будущем — отдавать боту решения: что торговать, с каким размером, а что блокировать.

На текущем этапе **бот интегрирован для аудита**: Risk Engine работает как отдельный сервис оценки сигналов и риска. На этапе бэктеста 5 боевых сделок Bybit бота система успешно отклонила все высокорисковые входы.

---

## 1. Инфраструктура и железо

- **Proxmox Host**: E5‑2696v3, 128 GB RAM.
- **VM (Debian 13, VMID 106)**:
  - NVIDIA драйвер + CUDA 12.x.
  - Titan V проброшена через VFIO (IOMMU, `vfio-pci`, `hostpci0`), доступно 12 GB HBM2.
  - **FP64 Бенчмарк**: матмул 4096×4096 (CuPy, float64) ≈ 6300 GFLOPS (~80–90% от теоретического пика).
- **Пул технологий**:
  - Python 3 + venv
  - FastAPI + uvicorn
  - CuPy (cuda12x), Numpy (как CPU Fallback)
  - httpx, aiosqlite, pydantic
- **Сетевой доступ**: Cloudflare Quick Tunnels (`cloudflared`) для безопасного внешнего доступа к API и Дашборду.

---

## 2. Структура проекта

```text
/opt/risk-engine/src
├── main.py              # FastAPI app + endpoints (/tv-webhook, /signals, /analysis, /health)
├── models.py            # Pydantic модели (RiskRequest, WebhookPayload, EnrichedRiskResult)
├── core/
│   └── monte_carlo.py   # Monte Carlo VaR/CVaR (FP64 на Titan V + Numpy Fallback)
├── scoring.py           # Signal quality scoring (оценка сигналов по Midas-метрикам)
├── bybit.py             # Fetcher (Binance.US API для price + volatility + local fallback)
├── db.py                # aiosqlite: таблица signals + агрегация аналитики
├── config.py            # Конфиг (env, роутинг, дефолты)
├── analyze_bot_trades.py# Скрипт прогонки истории внешнего бота через Risk Engine
└── tests/
    └── test_risk.py     # pytest тесты Monte Carlo ядра
```

---

## 3. Модели данных (Ключевые сущности)

### 3.1. Торговые и Риск-Модели
- **Position**: `symbol`, `side`, `size`, `entry_price`
- **Portfolio**: `equity`, `positions`, `leverage`, `free_margin`
- **MarketData**: `prices`, `volatility` (annualized σ)
- **RiskLimits**: `max_var` (default 5%), `max_cvar` (default 7%), `max_liquidation_prob` (1%)
- **RiskResult**: `approved`, `var`, `cvar`, `liquidation_prob`, `drawdown_estimate`

### 3.2. Вектор Входа (Payload)
**WebhookPayload** (TradingView/Midas):
- `symbol`, `side`, `size`, `source`
- Midas-метаданные: `risk_reward`, `probability`, `win_rate`, `trend`, `trend_strength`, `volume_level`

**EnrichedRiskResult** (Выход системы):
Включает все метрики [RiskResult](file:///root/.gemini/antigravity/scratch/risk_engine_src/models.py#55-63) + [signal_score](file:///root/.gemini/antigravity/scratch/risk_engine_src/scoring.py#15-143) (0-1.0), `is_countertrend`, `recommendation` (approve/reduce/reject) и разбивку `score_components`.

---

## 4. Ядро Интеллекта

### 4.1. Monte Carlo Ядро ([core/monte_carlo.py](file:///root/.gemini/antigravity/scratch/risk_engine_src/core/monte_carlo.py))
- Генерация: 100,000 вероятных сценариев цены на базе нормального распределения (шоков) `Z`.
- Исполнение: `cp.random.randn` (GPU) или `np.random.randn` (CPU Fallback).
- Расчет: относительные изменения цен -> P&L сценария -> Сортировка P&L массивов -> Определение 99% VaR (Value at Risk) и CVaR.

### 4.2. Signal Scoring ([scoring.py](file:///root/.gemini/antigravity/scratch/risk_engine_src/scoring.py))
Сложная нормализация и оценка данных от Midas.
- **Штрафы**: `is_countertrend` (long против медвежьего тренда), высокая общая волатильность рынка.
- **Формула**: `Score = 0.30*WR + 0.25*Prob + 0.20*RR + 0.15*Trend + 0.10*VolOk`
- **Вердикты**: 
  - `< 0.35` → REJECT
  - `0.35 - 0.50` → REDUCE
  - `> 0.50` → APPROVE
- *Force-reject*: Контртренд + Probability < 30% = немедленный отказ (REJECT).

### 4.3. Market Data ([bybit.py](file:///root/.gemini/antigravity/scratch/risk_engine_src/bybit.py))
- Забирает цену и волатильность за последний месяц (`klines`) через Binance.US.
- **Fallback Logic**: Если монета (напр. ARC) не найдена, используется цена входа из сигнала (WebhookPayload `entry_low` `entry_high`), чтобы симуляция не останавливалась.

---

## 5. Интеграция с TradingView / Ботами (Инструкция)

### 5.1. URL и Доступ
Cloudflare Tunnel динамически выдает URL. Проверить статус доступности:
`curl https://<tunnel_url>/health` (Должно вернуть `healthy` и `NVIDIA TITAN V`).

### 5.2. TradingView Alert (Webhook)
Настроить Webhook URL в TradingView на `https://<tunnel_url>/tv-webhook`.
В `Message` вставить JSON:
```json
{
  "symbol": "SOLUSDT",
  "side": "long",
  "size": 0.5,
  "source": "tradingview",
  "risk_reward": 2.5,
  "probability": 65,
  "win_rate": 55,
  "trend": "moderate_bull"
}
```
*В заголовки HTTP запроса (если поддерживается) добавить `X-Webhook-Secret: 123QWEasd`.*

### 5.3. Ручной Импорт Истории
Эндпоинт `POST /import-signals` принимает массив JSON объектов [WebhookPayload](file:///root/.gemini/antigravity/scratch/risk_engine_src/models.py#79-104) для массовой исторической проверки (бэктестинг Midas логов).

---

## 6. Как использовать Дашборд
Мониторинг доступен по адресу `https://unfibbing-audria-unpertaining.ngrok-free.dev/dashboard`.
- **Top Stats**: Общее количество сигналов, % одобренных/отколоненных, средняя задержка аппаратного просчета.
- **GPU Metrics**: В реальном времени показывается `Current Load`, `Peak Load` (чтобы не пропускать всплески от 100k сценариев) и занятая память HBM2.
- **Signals Table**: Детальная разбивка по каждому сигналу с колонкой `Highlights` (индикация `100k GPU` при успешном аппарантом просчете).

---

## 7. Следующие шаги (Phase 3)
1. **Смена Провайдера Данных**: Переход с Binance.US на Bybit API для полного устранения расхождений в котировках щитков (AERO, ARC, FARTCOIN).
2. **Интеграция Real-time Портфеля**: Передача `current_equity` и массива `open_positions` из бота прямо внутрь WebhookPayload, чтобы Risk Engine видел "всю картину", а не только одну изолированную позицию.
3. **Execution Bot**: Написание HTTP-клиента `risk_client.py` для торгового бота, который будет блокировать ордера на бирже, если Risk Engine возвращает `recommendation: reject` или `approved: false`.
