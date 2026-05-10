<div align="center">

# 🛡️ REDU — Risk Engine & Думалка

**Intelligent Algorithmic Position Management for Crypto Trading**

[![Version](https://img.shields.io/badge/version-0.19.8-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13+-yellow.svg)]()
[![GPU](https://img.shields.io/badge/GPU-NVIDIA%20Titan%20V-76B900.svg)]()
[![ML](https://img.shields.io/badge/ML-ExtraTrees%2BOptuna-orange.svg)]()

*From signal scoring to autonomous position management — a data-driven approach to maximizing capture ratio and eliminating PnL leakage.*

</div>

---

## 🎯 What is REDU?

**REDU** (Risk Engine + DUmalka) is an autonomous position management system for cryptocurrency perpetual futures. While most trading systems focus on *entries*, REDU focuses on **exits** — the 70% of alpha that most traders leave on the table.

### The Problem
Trades reach +3-5% profit but reverse before the trader exits. **PnL Leakage** — the gap between peak profit and realized profit — destroys returns even for strategies with 60%+ win rates.

### The Solution
A 4-level decision pipeline that monitors every open position in real-time, using GPU-accelerated Monte Carlo simulations (100K scenarios/minute) to determine optimal exit timing.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    REDU System Architecture                    │
│                                                                │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│  │   Signals    │───▶│ Risk Engine  │───▶│  Trading Bot    │  │
│  │  (Midas/TV)  │    │  (Scoring)   │    │  (Execution)    │  │
│  └─────────────┘    └──────┬───────┘    └────────▲────────┘  │
│                            │                      │            │
│                     ┌──────▼───────┐              │            │
│                     │   Думалка    │──────────────┘            │
│                     │  (Position   │   Commands:               │
│                     │   Manager)   │   partial_close,          │
│                     │              │   move_sl, full_close     │
│                     └──────┬───────┘                           │
│                            │                                   │
│              ┌─────────────▼──────────────┐                   │
│              │   GPU Monte Carlo Engine    │                   │
│              │  (Titan V, 100K scenarios)  │                   │
│              └────────────────────────────┘                   │
└──────────────────────────────────────────────────────────────┘
```

### 4-Level Decision Pipeline

| Level | Name | Function |
|-------|------|----------|
| **A** | Force Majeure | Squeeze detection, exchange outage protection |
| **B** | Market Regime | Trend/range classification, dynamic SL/TP adjustment |
| **C** | State Filters | Time-Decay exits, profit leak detection, zone policy |
| **D** | AI/ML Analytics | MC probability scoring, optimal hold time prediction |

---

## ✨ Key Features

### 📊 Intelligent Scoring Engine
- Multi-factor signal scoring with GPU acceleration
- Regime-aware recommendations (approve/reduce/reject)
- Volatility-adjusted position sizing

### 🧠 Думалка (Position Manager)
- **Zone Policy System** — 5-zone adaptive exit strategy calibrated on 103K+ live snapshots
- **TP1 Normalizer** — caps effective TP distances for robust zone mathematics regardless of extreme targets (+40-90%)
- **Monte Carlo Forward Projection** — 100K GPU-accelerated price path simulations
- **ATR-Adaptive TP1 Soft BE** (v0.19.4) — skip breakeven move when TP1 distance < 2x ATR(1h), keeps original SL for volatile assets
- **3-Tier Volatility Classification** (v0.19.5) — DEFINITE HIGH (>=4.0), TRANSITIONAL (2.0-4.0), NORMAL (<2.0) with enriched audit diagnostics
- **Configurable Partial Close Logic** — fractional exits (25-90%) toggleable via `PARTIAL_CLOSE_ENABLED` for optimal capture ratios
- **SL Breakeven Protection** — automatic stop-loss adjustment after TP progress

### ⏳ Time-Decay Exits (v0.9.3)
Data analysis of 373 trades revealed a critical **"Danger Zone"** at 1-4 hours:
- Win Rate drops to **20.2%** (vs 65.7% for <1h trades)
- Cumulative loss of **-$113.6** in this window

The engine now detects stale momentum and forces SL to breakeven, with smart exemptions for trades showing genuine progress.

### 🛡️ Per-Symbol Circuit Breaker (v0.9.3)
- Isolates errors per trading pair (GRASS errors don't disable WIF)
- Smart classification of retriable vs fatal errors
- Auto-cooldown recovery (5 minutes)

### 📈 Analytics Dashboard
- Real-time web dashboard with 15+ interactive Chart.js widgets
- Bilingual support (EN/RU)
- Capture Ratio tracking, Exit Quality / post-close analytics, Win Rate trends
- Time-Decay visualization with "Danger Zone" highlighting
- **Midas Benchmark panel** (v0.16.0) — live RE vs Midas signal quality comparison
- **Institutional Risk Metrics** (v0.14.4) — Sharpe, Sortino, MaxDD, Profit Factor, Calmar

### 👻 Shadow PnL & Signal Quality (v0.16.0+)
- Counterfactual SL/TP simulation for ALL signals (approved + rejected)
- Shadow recheck for stale "open" signals with extended kline window
- Score correction backfill for win_rate parsing bug retrofix
- `/api/midas-benchmark` endpoint with corrected metrics

### 📉 Exit Decision Quality Tracker (v0.18.0)
- **Analytics-only** module (`exit_quality.py`) — evaluates Dumalka exit quality for training; no effect on live trading
- **Post-close trajectory**: background backfill writes to `what_if_outcomes` (SL/TP “what if” + PnL at 1h / 4h / 12h / 24h after close)
- **sl_breakeven effectiveness** and **missed exits** (`sl_hit` leakage by zone, post–26 Mar Active Mode via `?since=`)
- **Dashboard**: “Exit Quality — Dumalka Decision Analytics” + `exit_quality` task in `/health` background monitor
- **API**: `GET /api/exit-quality`, `/api/exit-quality/sl-breakeven`, `/api/exit-quality/missed-exits`, `POST /api/exit-quality/recalculate`; `POST /api/what-if-analysis` kept for compatibility
- **Klines**: shared `kline_fetcher.py` (OKX → Binance.US → Bybit → proxy) used by shadow and exit-quality paths

### 🏆 Proven Position & Portfolio Awareness (v0.18.1–v0.18.7)
- **E_pnl Reform** (v0.18.1) — true E[PnL] across all MC paths, skewness detection, momentum-based hold/exit
- **Patience Protocol** (v0.18.2) — Apollo Bail shadow-only, configurable grace period (1h)
- **Bot Roster Desync** (v0.18.3) — auto-close ghost DB rows, admin force-close API
- **Proven Position** (v0.18.4) — sticky grace bypass for positions that proved themselves (max_pnl >= 3%, tp >= 50%); TP1 idempotency guard; Portfolio Stress TG alerts
- **Exit Quality Pipeline Fix** (v0.18.5) — OKX SWAP-first, sentinel rows, all close_reasons analyzed, DESC ordering, 5x throughput
- **Structured Audit Log** (v0.18.6) — pos_id + details JSONB columns, audit for all 13 close reasons, phantom transitions, state changes, market_state enrichment. Coverage: 30% to 95%.
- **ML Training Data Recovery** (v0.18.7) — `finalize_snapshot_future_pnl` recovers last-hour snapshots on position close. +36% labeled data.

### 🚀 Rocket Catcher & Multi-Horizon ML (v0.19.x)
- **Multi-Horizon ML Labeling** (v0.19.2) — `future_pnl_12h`, `future_pnl_max_24h` in position_snapshots; peak tracking `max_pnl_24h` in what_if_outcomes
- **Scout Signal Generator** (v0.19.1-v0.19.2) — 8 autonomous shadow signal types (funding_extreme_reversal, volume_breakout, rsi_divergence, spike_consolidation_breakout, etc.), 12 ML features per signal, derivatives time-series
- **Kline Collector** (v0.19.1) — 2-tier historical candle collection (OKX -> Bybit Proxy), adaptive source cache, 34 symbols x 3 timeframes
- **Deep Kline Backfill** (v0.19.5) — paginated one-time fetch (500 1h + 200 4h per symbol)
- **ML Shadow Mode** (v0.19.6) — ExtraTrees+Optuna model (LOO AUC 0.574-0.621) predicts profit probability per position, logs to `ml_predictions` table, zero trade impact
- **Bot Close-Event Sync** (v0.19.6.1) — `dumalka_close`, `manual_close`, `flip_close` properly update `open_positions` via both Telegram and HTTP paths

### 🔍 Watchlist Scanner
- Autonomous dump/pump detection across monitored symbols
- Contra-trend opportunity alerts
- Post-mortem analysis with simulated PnL

---

## 🚀 Quick Start

### Prerequisites
- Python 3.13+
- PostgreSQL 18+
- NVIDIA GPU (CUDA) — for Monte Carlo simulations
- Trading bot with REST API for order execution

### Installation

```bash
# Clone the repository
git clone https://github.com/etho-ya/REDU.git
cd REDU

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install fastapi uvicorn psycopg2-binary numpy cupy-cuda12x scipy httpx python-telegram-bot

# Configure
cp src/config.py.example src/config.py
# Edit src/config.py with your credentials

# Initialize database
sudo -u postgres createdb riskengine_db
sudo -u postgres psql -d riskengine_db -f src/db_schema.sql

# Run
cd src && uvicorn main:app --host 0.0.0.0 --port 8000
```

### Configuration

All settings are controlled via environment variables with sensible defaults. See [`src/config.py.example`](src/config.py.example) for the full reference.

Key variables:
| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | Telegram bot for notifications |
| `TELEGRAM_CHAT_ID` | Telegram channel for alerts |
| `BYBIT_PROXY_URL` | Exchange API proxy endpoint |
| `DUMALKA_ACTIVE_MODE` | Enable/disable active position management |
| `N_SCENARIOS` | Number of Monte Carlo simulations (default: 100,000) |
| `ML_SHADOW_ENABLED` | Enable ML Shadow Mode predictions (default: true) |
| `ML_SHADOW_CONFIDENCE_THRESHOLD` | ML prediction confidence filter (default: 0.65) |
| `TP1_BE_ATR_SKIP_MULT` | ATR multiplier for TP1 Soft BE skip (default: 2.0) |

---

## 📁 Project Structure

```
REDU/
├── src/
│   ├── main.py                  # FastAPI application (80+ endpoints)
│   ├── config.py                # Configuration (env vars)
│   ├── scoring_v2.py            # Multi-factor signal scoring (v2)
│   ├── position_tracker.py      # Думалка — position management (~2.4K LOC)
│   ├── exit_quality.py          # Exit Decision Quality Tracker
│   ├── scout.py                 # Autonomous Shadow Signal Generator (8 types)
│   ├── kline_collector.py       # 2-tier kline collection (OKX → Bybit)
│   ├── kline_fetcher.py         # Historical klines (multi-exchange fallback)
│   ├── regime_detector.py       # Market regime classification
│   ├── watchlist_scanner.py     # Autonomous dump/pump monitor
│   ├── telegram_bridge.py       # Telegram bot integration
│   ├── bybit.py                 # Exchange API client
│   ├── db.py / db_adapter.py    # Database layer + PostgreSQL adapter
│   ├── notifications.py         # Telegram alert system
│   ├── models.py                # Pydantic data models
│   ├── core/
│   │   ├── monte_carlo.py       # GPU Monte Carlo engine (Titan V)
│   │   └── sentinel.py          # Real-time price sentinel (Bybit WS)
│   ├── models/
│   │   └── et_shadow_v1.pkl     # ML Shadow model (ExtraTrees+Optuna)
│   ├── scripts/
│   │   ├── train_shadow_model.py       # ML model training pipeline
│   │   ├── label_optimal_actions.py    # Multi-horizon labeler
│   │   └── backfill_klines_deep.py     # Deep kline backfill
│   ├── tests/                   # Unit + integration tests
│   ├── static/                  # Web dashboard (HTML/JS/CSS)
│   └── ARCHITECTURE.md          # Technical architecture docs
├── docs/
│   ├── index.md                 # Documentation navigation map
│   └── log.md                   # Decision journal (append-only)
├── CHANGELOG.md                 # Version history
├── dumalka_next_steps.md        # Hypotheses & strategic roadmap
├── PROJECT_RISK_ENGINE_FULL.md  # Complete project documentation
├── BOT_INTEGRATION_CHANGELOG.md # Bot-side change documentation
└── LICENSE
```

---

## 📊 Performance (Live Results, April 2026)

| Metric | Value |
|--------|-------|
| **Win Rate (Approved, Apr 2+)** | **63%+** |
| **Closed Positions** | **700+** |
| **ML Snapshots Collected** | **103,000+** (multi-horizon, 34 features) |
| **Kline Storage** | **10,000+** candles (34 symbols, 3 timeframes) |
| **Scout Signals** | Accumulating (8 types, shadow mode) |
| **ML Shadow Predictions** | Live (ExtraTrees+Optuna, LOO AUC 0.574) |

---

## 🗺️ Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| v0.8-0.9 | Infrastructure, PostgreSQL, Time-Decay, Circuit Breaker | ✅ Complete |
| v0.14-0.18 | Audit Log, Portfolio Awareness, Exit Quality, ML Data | ✅ Complete |
| v0.19.x | Rocket Catcher, Scout Signals, ML Shadow Mode | ✅ Live |
| **Phase 2** | **Volatility-aware thresholds (vol >= 4.0 only)** | ⏳ **Checkpoint Apr 21** |
| v1.0 | ML-driven exit decisions (N >= 300 positions) | ⏳ Mid-April |
| v1.1 | Reinforcement Learning Agent (DQN/CQL) | 🔮 R&D |
| v1.2 | Portfolio Correlation Hedging | 🔮 R&D |

---

## 📚 Documentation

- [Documentation Index](docs/index.md) — Navigation map to all project docs
- [Architecture](src/ARCHITECTURE.md) — Technical architecture & decision pipeline (13 tables, ER diagram)
- [Changelog](CHANGELOG.md) — Version history & release notes
- [Decision Log](docs/log.md) — Append-only journal of significant decisions with rationale
- [Hypotheses & Roadmap](dumalka_next_steps.md) — Active hypotheses, ML experiments, strategic roadmap
- [Full Project Spec](PROJECT_RISK_ENGINE_FULL.md) — Complete project documentation
- [Bot Integration](BOT_INTEGRATION_CHANGELOG.md) — Bot-side changes affecting REDU integration

---

## 🛠️ Tech Stack

- **Backend**: Python 3.13, FastAPI, Uvicorn
- **Database**: PostgreSQL 18 (13 tables, JSONB audit log)
- **GPU**: NVIDIA Titan V (CUDA 12.x, CuPy) — Monte Carlo + ML training
- **ML**: scikit-learn (ExtraTrees), Optuna (hyperparameter tuning), joblib
- **Frontend**: Vanilla HTML/CSS/JS, Chart.js (15+ interactive widgets)
- **Notifications**: Telegram Bot API (python-telegram-bot)
- **Exchange**: Bybit Perpetual Futures API (REST + WebSocket)
- **Data**: OKX + Bybit kline collection, derivatives snapshots (funding, OI, LSR)

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**Built with 🧠 by [etho-ya](https://github.com/etho-ya)**

*"The money is not in the entry — it's in the exit."*

</div>
