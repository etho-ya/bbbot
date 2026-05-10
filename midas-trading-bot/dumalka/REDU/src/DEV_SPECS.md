# Risk Engine — Development Specifications

> Conventions, module interfaces, and patterns for contributing to the Risk Engine codebase.

---

## 📐 Code Conventions

### Python Style
- **Python**: 3.10+ (f-strings, type hints, `match/case`)
- **Async**: All HTTP operations are `async/await` (httpx). DB via `asyncio.to_thread()` + psycopg2
- **Types**: Pydantic v2 models for all API contracts
- **Logging**: `logging.getLogger("risk-engine.<module>")` → JSON structured logs
- **No ORM**: Raw SQL through db_adapter (psycopg2 with auto SQLite→PG conversion)
- **GPU fallback**: Every CuPy call must have NumPy fallback via `xp = cp if _USE_GPU else np`

### File Naming
- Modules: `snake_case.py`
- Tests: `tests/test_<module>.py`
- Scripts: `scripts/<purpose>.py`
- Static pages: `static/<page>.html`

### Configuration
- All config through `config.py` → `os.getenv()` with sensible defaults
- No hardcoded URLs, secrets, or thresholds in business logic
- New config params: add to `Config` class, document in `ARCHITECTURE.md`

---

## 🧩 Module Interface Map

### Core Pipeline

```
main.py::webhook_handler(payload)
    │
    ├── bybit.fetch_market_data(symbol)        → (price, vol)
    ├── bybit.fetch_orderbook_depth(symbol)     → (spread_pct, slippage_pct)
    ├── bybit.fetch_multi_timeframe(symbol)     → {15m, 1h, 4h: direction}
    ├── bybit.fetch_funding_and_oi(symbol)      → (funding_rate, oi_change_pct)
    │
    ├── regime_detector.update_regime(klines)   → {regime, confidence, adjustments}
    │
    ├── scoring.compute_signal_score(...)       → {score, recommendation, components, kelly_f}
    │
    ├── monte_carlo.run_monte_carlo_risk(...)   → RiskResult(var, cvar, liquidation_prob)
    │
    ├── portfolio_allocator.check_portfolio_limits(...)  → {approved, warnings}
    │
    ├── db.insert_signal(record)        → 38 columns (32 ML features + 4 shadow PnL + meta)
    │     incl. funding_rate, oi_change_pct, market_regime, spread_pct,
    │     slippage_pct, multi_tf_trends_json (v0.14.2)
    │
    └── send_telegram_alert(result)

# Background Systems:
    shadow_pnl_backfill_loop()          → every 5min: price snapshots 1h/4h + SL/TP klines sim
    position_tracker_loop()             → every 60s: zone policy + GPU MC + position mgmt
    watchlist_scanner_loop()            → every 5min: pump/dump detection + accumulation
    sentinel_loop()                     → every 10s: BTC flash crash monitor

# ML Intelligence Layer (v0.13.2) — inside position_tracker_loop:
    bybit.fetch_btc_change_1h()                → BTC 1h price change (Binance.US)
    bybit.fetch_rsi_from_klines(symbol)        → RSI-14 (Bybit → Binance.US fallback)
    bybit.compute_orderbook_imbalance(b, a)    → bid/ask ratio (from existing data)
    bybit.fetch_long_short_ratio(symbol)       → OKX L/S account ratio
    db.insert_position_snapshot(29 params)      → ML snapshot with Intelligence features
```

### Key Interfaces

#### `scoring_v2.score()`
def compute_signal_score(
    side: str,                              # "long" or "short"
    risk_reward: Optional[float] = None,    # Midas R:R (e.g., 2.6)
    probability: Optional[float] = None,    # Midas prob 0-100
    win_rate: Optional[float] = None,       # Midas WR 0-100
    trend: Optional[str] = None,            # "strong_bear", etc.
    trend_strength: Optional[float] = None,
    volume_level: Optional[str] = None,
    market_vol: float = 0.5,                # Annualized volatility
    spread_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    multi_tf_trends: Optional[dict] = None, # {15m: "bullish", 1h: ...}
    funding_rate: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
    regime_adjustments: Optional[dict] = None,
) -> dict:
    # Returns:
    # {
    #     "score": 0.72,
    #     "recommendation": "approve",  # "approve"/"reduce"/"reject"
    #     "is_countertrend": False,
    #     "components": {"wr": 0.8, "prob": 0.7, ...},
    #     "kelly_f": 0.15,
    #     "data_quality_pct": 100,
    # }
```

#### `core.monte_carlo.run_monte_carlo_risk()`
```python
def run_monte_carlo_risk(
    portfolio: Portfolio,
    candidate: Optional[CandidateTrade],
    market: MarketData,
    limits: RiskLimits,
    n_scenarios: int = 100_000,
    horizon_hours: float = 24.0,
    confidence: float = 0.99,
) -> RiskResult:
    # Uses Jump-Diffusion GBM on GPU:
    #   rel_changes = vol * sqrt(dt) * Z + poisson_jumps * jump_sizes
    # Returns: RiskResult(approved, var, cvar, liquidation_prob, drawdown_estimate)
```

#### `regime_detector.detect_regime()`
```python
def detect_regime(
    klines: list,           # Hourly klines [[ts,o,h,l,c,vol,to], ...]
    volume_ratio: float,    # Current vol / 24h avg
    current_vol: float,     # Annualized volatility
) -> dict:
    # Returns: {"regime": "trending", "confidence": 0.85, "details": {...}}
```

---

## 🗃️ Database Patterns

### Read Pattern (Analytics)
```python
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val

# Fetch all rows (returns list of dicts)
rows = await pg_fetch_all("SELECT * FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT 10", (symbol,))

# Fetch single row (returns dict or None)
row = await pg_fetch_one("SELECT * FROM signals WHERE signal_hash = ?", (hash,))

# Fetch single value (scalar)
count = await pg_fetch_val("SELECT COUNT(*) FROM signals")
```

### Write Pattern (Signal Insert)
```python
from db_adapter import pg_execute, pg_executemany

# Single insert
await pg_execute("INSERT INTO signals (symbol, side) VALUES (?, ?)", (symbol, side))

# Batch insert
await pg_executemany("INSERT INTO trade_outcomes (hash, event) VALUES (?, ?)", batch_params)
```

### SQL Dialect Conversion (Automatic)
```
SQLite → PostgreSQL (handled by db_adapter._convert_sql):
  ?                     → %s
  datetime('now')       → NOW()
  datetime('now','-1h') → NOW() - INTERVAL '1 hour'
  strftime('%H', col)   → EXTRACT(HOUR FROM col)::INTEGER
  julianday(a)-julianday(b) → EXTRACT(EPOCH FROM (a::timestamp - b::timestamp))/86400
```

### Cache Pattern (3-Level)
```
Level 1: In-memory Python dict (2ms, per-request)
Level 2: analytics_cache table (37ms, materialized views)
Level 3: Full SQL query (200ms+, cold PostgreSQL)
```

---

## ⚡ GPU Development

### Adding a New GPU Function

1. Place in `core/gpu_analytics.py` or `core/monte_carlo.py`
2. Use the standard GPU/CPU dual pattern:

```python
def my_gpu_function(data):
    xp = cp if _HAS_CUPY else np    # GPU or CPU fallback
    
    # Compute on GPU
    result = xp.some_operation(data)
    
    # Transfer back to CPU for Python logic
    if hasattr(xp, 'asnumpy'):
        result = xp.asnumpy(result)
    
    return result
```

3. Always use FP64 for financial calculations: `.astype(xp.float64)`
4. Log device and timing: `logger.info(f"⚡ {name}: {elapsed*1000:.1f}ms on {device}")`

---

## 📡 External API Contracts

### Bybit Proxy (VPS)
```
GET  /market-data/{symbol}     → {price, klines: [[ts,o,h,l,c,vol,to], ...]}
GET  /orderbook/{symbol}       → {bids: [...], asks: [...]}
GET  /funding-rate/{symbol}    → {funding_rate, oi_change_pct}
```

### Telegram Bot API (Direct — v0.9.1)
```
POST https://api.telegram.org/bot<token>/sendMessage → {chat_id, text, parse_mode}
```
**Notification Pattern (position_tracker.py):**
- Routine actions (`move_sl`, `partial_close`) → buffered in `_tg_action_buffer` → hourly digest
- Critical actions (`full_close`, `portfolio_override`) → sent immediately via `_send_telegram_direct()`
- Buffer cap: 500 entries max. Error logging: `logger.warning` (non-silent)

### Trading Bot (VPS)
```
POST /trade-outcome          → {signal_hash, event, details}
POST /dumalka/command        → {action: partial_close | move_sl | place_limit_tp | full_close, symbol, fraction, new_sl, target_price}
GET  /dumalka/positions      → returns list of active symbols to sync states
```

---

## 🧪 Testing Patterns

### Unit Test Structure
```python
import pytest
from scoring import compute_signal_score

def test_approve_high_quality_signal():
    result = compute_signal_score(
        side="long",
        risk_reward=3.0,
        probability=75,
        win_rate=80,
        market_vol=0.5,
    )
    assert result["recommendation"] == "approve"
    assert result["score"] > 0.60

def test_reject_low_quality():
    result = compute_signal_score(side="long", market_vol=2.5)
    assert result["recommendation"] == "reject"
```

### Run Commands
```bash
# All tests
cd src && python -m pytest tests/ -v

# Specific module
python -m pytest tests/test_scoring_v2.py -v

# With output
python -m pytest tests/ -v -s
```

---

## 🧠 ML/RL Pipeline (v0.14.2 Stage)

### Data Labeling
```bash
# Label optimal_action for all closed positions (PostgreSQL)
cd /opt/risk-engine/src && python3 scripts/label_optimal_actions.py
# Output: hold/partial_close/close labels in position_snapshots.optimal_action
```

### ML Dataset Export
```bash
# Export labeled snapshots to CSV (10 core + 3 engineered + 4 conditional features)
python3 scripts/export_ml_dataset.py
# Output: data/ml_dataset_v1.csv (~67K rows)
```

### XGBoost Training
```bash
# Train exit model with class weights and time-based split
python3 scripts/train_exit_model.py
# Output: data/xgboost_exit_v1.json + data/feature_importances.csv
```

### RL Environment
```python
from core.trading_env import TradingEnv

env = TradingEnv(data_source="csv")         # or "db" for live PG data
obs, info = env.reset()                      # Start episode
obs, reward, done, trunc, info = env.step(0) # 0=hold, 1=partial25, 2=partial50, 3=full_close

# Random agent baseline
results = env.run_random_agent(n_episodes=20)
```

### Full Key Features
```
Labeler:        67,555 snapshots labeled (hold/close/partial_close)
XGBoost:        13 features, 3 classes, time-based split
                Top features: zone (gain=662.8), zone_x_tp_progress (348.2)
RL Env:         232 episodes, 10-feature state, 4 actions
                Reward: capture ratio + time penalty
Dependencies:   xgboost 3.2.0, scikit-learn 1.8.0, numpy 2.4.2
```

---

## 🚀 Deployment

### Start Service
```bash
cd /opt/risk-engine/src
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### Systemd Service
```bash
sudo systemctl restart risk-engine
sudo systemctl status risk-engine
journalctl -u risk-engine -f
```

### DB Backup
```bash
# PostgreSQL backup
pg_dump -U riskengine riskengine_db > backup_$(date +%Y%m%d).sql
python backup_db.py  # Sends to Telegram
```

---

## 📝 Adding a New Feature Checklist

- [ ] Add config params to `config.py`
- [ ] Create/modify module in `src/`
- [ ] Add Pydantic models to `models.py` (if new data structures)
- [ ] Add DB table/columns via PostgreSQL migration script
- [ ] Add API endpoint to `main.py` (if external access needed)
- [ ] Add background task to `main.py::startup()` (if periodic)
- [ ] Write tests in `tests/test_<feature>.py`
- [ ] Update `ARCHITECTURE.md` with new module/endpoint
- [ ] Update `info.html` roadmap section
- [ ] Update `dashboard.html` version badge
- [ ] Update version in `/health` endpoint
- [ ] Run `pytest` to verify no regressions
