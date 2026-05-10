"""
Risk Engine — Main Application (FastAPI)
=========================================

The central API server and orchestrator for the Risk Engine trading platform.
Handles incoming Midas/TradingView webhook signals, performs GPU-accelerated
Monte Carlo risk analysis, and coordinates all background subsystems.

Architecture
------------
This module is the entrypoint that starts 8 concurrent systems:
  1. **FastAPI HTTP Server** — REST API for signal processing, analytics,
     dashboard UI, and GPU analytics endpoints.
  2. **Position Tracker (Думалка)** — Background loop (~30s cycle) that
     actively manages open positions via zone-based exit policy + GPU MC.
  3. **Watchlist Scanner** — Background loop (~5min cycle) that scans
     selected symbols for pump/dump anomalies and accumulation patterns.
  4. **Binance Sentinel** — Background loop (~10s cycle) that monitors
     BTC for flash-crashes and triggers emergency portfolio exits.
  5. **Shadow PnL Backfill** — Background loop (~10min cycle) with two passes:
     (a) First pass: backfill shadow_pnl for new signals (SL/TP klines sim).
     (b) Second pass (v0.16.0): recheck stale "open" signals with extended
         kline window (batch of 5, every 24h per signal).
     Records shadow_resolved_at, shadow_resolved_candles for time analytics.
  6. **Bybit WebSocket Price Feed** — Real-time ticker stream for all
     traded symbols (23+). Shadow mode: observes only, no trade actions.
  7. **Kline Historical Collector** (v0.19.0) — Background loop (~60s cycle)
     that stores 15m/1h/4h candles in PostgreSQL for Scout and future ML.
     2-tier fetch: OKX (geo-free) → Bybit Proxy (100% altcoin coverage).
  8. **Scout Signal Generator** (v0.19.2) — Background loop (~15min cycle)
     that generates autonomous signals (8 types: EMA cross, RSI threshold,
     RSI divergence, volume breakout, funding extreme reversal,
     spike consolidation breakout) in SHADOW mode.
     12 ML features per signal. Derivatives time-series collection. No trading.
  9. **ML Shadow Predictor** (v0.19.6) — ExtraTrees+Optuna model loaded at
     startup from src/models/et_shadow_v1.pkl. Predicts profit probability
     after 5 position snapshots; logs to ml_predictions table. No trade impact.

Signal Processing Pipeline
--------------------------
  Webhook → Parse Midas payload → Fetch market data (Bybit Proxy)
  → Compute signal score (scoring_v2.py) → GPU Monte Carlo risk check
  → Portfolio limits check → Approve/Reduce/Reject → Log to PostgreSQL
  (32 ML features incl. funding_rate, regime, spread, multi-TF trends)
  → Forward to Trading Bot (if approved) → Telegram notification

Observability (v0.14.2)
-----------------------
  - **Structured JSON Logging**: python-json-logger with trace_id, symbol,
    duration_ms as structured fields. RotatingFileHandler (100MB × 30).
  - **Trace Context**: LoggerAdapter propagates trace_id (signal_hash) and
    symbol through entire request lifecycle. Query: jq 'select(.trace_id=="X")'
  - **Noise Suppression**: httpx/httpcore/uvicorn.access set to WARNING.
  - **Error Rate Middleware**: Tracks 2xx/4xx/5xx counts, slow requests (>5s),
    last 50 errors. Exposed via /health → operational section.
  - **Request Correlation**: X-Request-ID header (UUID4) on every response.
  - **Per-Phase Timing**: duration_ms logged for market_fetch, scoring, MC.

ML Data Collection (v0.14.2+)
------------------------------
  - **Shadow PnL**: Background loop simulates SL/TP outcomes for ALL signals
    using klines data. Columns: shadow_pnl_1h, shadow_pnl_4h, shadow_outcome,
    shadow_resolved_at, shadow_resolved_candles (v0.16.0).
  - **Shadow Recheck (v0.16.0)**: Second pass resolves stale "open" signals by
    fetching klines beyond the initial 48h window. Batch of 5 per cycle.
  - **Feature Persistence**: 6 additional ML features per signal: funding_rate,
    oi_change_pct, market_regime, spread_pct, slippage_pct, multi_tf_trends_json.
  - **Scoring Quality**: /api/scoring-quality provides confusion matrix,
    calibration curve, and threshold optimization for scoring calibration.
  - **Score Correction (v0.16.1)**: Retrofix for win_rate parsing bug. Columns:
    score_quality_penalty, corrected_score, corrected_recommendation.

Key Endpoints
-------------
  POST /webhook             — Main signal intake (Midas/TradingView)
  POST /trade-outcome       — Record closed trade PnL
  GET  /api/analytics       — Aggregated performance statistics
  GET  /api/gpu-analytics   — GPU SL optimization, correlation matrix, batch MC
  GET  /api/scoring-quality — Confusion matrix, calibration, threshold optimizer
  GET  /api/midas-benchmark — Midas signal quality vs RE filtering (v0.16.0)
  GET  /api/risk-metrics    — Sharpe, Sortino, MaxDD, Profit Factor, Calmar
  GET  /api/ml-shadow       — ML shadow predictions for active positions (v0.19.6)
  GET  /health              — Service health + version + operational metrics

Version History
---------------
  v0.19.9 (2026-04-12): Audit Log — Complete Coverage — closes gap where bot-initiated
                         close events (sl_hit, dumalka_close, flip_close, etc.) arriving via
                         POST /trade-outcome, and reversal/re-entry closes via POST /tv-webhook,
                         never wrote position_closed to dumalka_audit_log. Both paths now write
                         structured JSONB entry with close_reason, pnl_pct, max_pnl_pct,
                         drawdown_pct, tp_progress_pct, source (bot_api / webhook_reversal).
                         Reversal path also now writes realized_pnl_pct to open_positions.
                         Backfilled 20 historical missing close audit entries. Coverage: 100%.
  v0.19.8 (2026-04-11): API-first Bot Integration — migrates all Bot↔RE machine-to-machine
                         communication from Telegram text parsing to pure HTTP API.
                         Fixes: dedup bypass for source=bot_direct; rejection_reason field
                         exposed in EnrichedRiskResult; event:open handler in /trade-outcome
                         (entry price sync from exchange fill). New: send_signal_report() and
                         send_trade_event_report() in notifications.py for TG observability
                         without telegram_bridge dependency. New: docs/BOT_API_CONTRACT.md.
  v0.19.7 (2026-04-11): Health Watchdog — async background task monitors all 9 system
                         components and bot connectivity; Telegram alerts on stale/down with
                         30-min cooldown and recovery notifications. Config: HEALTH_WATCHDOG_ENABLED,
                         BOT_UNREACHABLE_ALERT_SEC (420s), WATCHDOG_ALERT_COOLDOWN_SEC (1800s).
                         New module: health_watchdog.py. Startup grace 180s prevents false alerts.
  v0.19.6 (2026-04-08): ML Shadow Mode — ExtraTrees+Optuna model (AUC 0.621, LOO) loaded
                         at startup, predicts profit probability after 5 snapshots. Logs to
                         ml_predictions table, no trade impact. Config: ML_SHADOW_ENABLED,
                         ML_SHADOW_CONFIDENCE_THRESHOLD. New endpoint: /api/ml-shadow.
  v0.19.0 (2026-04-04): Kline Storage + Scout Infrastructure — stores 15m/1h/4h candles in
                         PostgreSQL (klines_history), generates autonomous shadow signals (scout_signals)
                         via EMA/RSI/Regime analysis. 2-tier fetch: OKX (geo-free) + Bybit Proxy.
                         Adaptive source cache for Bybit-exclusive altcoins. Shadow PnL tracking.
                         New endpoints: /api/klines/{symbol}, /api/klines-stats, /api/scout-signals.
  v0.18.9 (2026-04-04): sl_breakeven clamp fix — ATR offset could push SHORT SL past market
                         price (Bybit rejected). Clamp ensures SL between entry and current_price.
                         Verified live on RIVER #590: SL moved from 12.999→12.173 (was stuck).
  v0.18.8 (2026-04-04): Hard SL Cap ACTIVATED — MAX_LOSS_PCT=3.5%, PHASE1_ACTIVE_MODE=true.
                         Backtest on 100 approve-only trades: PF 0.946→1.263 (with slippage).
                         Cuts catastrophic tail losses (10 trades >-3.5%) without touching zone policy.
  v0.18.7 (2026-04-04): ML Data Recovery — finalize_snapshot_future_pnl at close, automated labeler.
  v0.18.6 (2026-04-04): Structured Audit Log — pos_id + details JSONB + market_state enrichment
                         on dumalka_audit_log. Audit writes for all close reasons, phantom transitions,
                         state changes, shadow decisions. Coverage 30% -> 95%.
  v0.18.5 (2026-04-03): Exit Quality Pipeline Fix + Bot Integration + BE sign fix.
  v0.18.4 (2026-04-02): "Proven Position" — sticky grace period bypass when max_pnl >= 3%
                         AND tp_progress >= 50% (NOM #548 post-mortem: +14%→-3.7% prevented).
                         TP1 idempotency guard (skip redundant move_sl if SL already at BE).
                         Portfolio Stress Warning — TG alert when same-direction underwater.
  v0.18.3 (2026-04-02): Bot roster desync — auto-close DB row when symbol missing from
                         /dumalka/positions for BOT_ABSENT_CLOSE_CYCLES; POST
                         /api/admin/force-close-position (FORCE_CLOSE_SECRET).
  v0.18.2 (2026-04-02): Patience Protocol — Apollo Bail disabled (shadow-only), grace period
                         YOUNG_POSITION_HOURS from config (1.0h, was 0.5h hardcoded).
                         APOLLO_BAIL_ENABLED config toggle. Service restart to deploy v0.18.1.
  v0.18.1 (2026-04-02): E_pnl Reform "Let Winners Run" — full_e_pnl_pct (true math expectation
                         across ALL MC paths), pnl_skewness (fat-tail detection), TP cap removed
                         from MC, jump-diffusion params configurable, E_PNL_EXIT_THRESHOLD=-1%.
  v0.18.0 (2026-04-01): Exit Decision Quality Tracker — evaluates ALL Dumalka exit decisions
                         for training. Phases: post-close SL/TP sim + multi-horizon PnL (1),
                         sl_breakeven effectiveness (2), missed exits / sl_hit analysis (3).
                         New module: exit_quality.py, kline_fetcher.py extracted from main.
  v0.17.0 (2026-03-31): Same-coin re-entry protocol — new same-side signals pass to scoring
                         instead of blind reject; Dumalka full_close for both re-entry and
                         reversal (was DB-only). Config: REENTRY_ENABLED, REENTRY_COOLDOWN_HOURS.
  v0.16.1 (2026-03-31): Score correction backfill (undo win_rate parsing bug penalty),
                         kline drift sanity checks, corrected_score/recommendation columns.
  v0.16.0 (2026-03-31): Midas Benchmark endpoint + dashboard, shadow recheck 2nd pass,
                         shadow_resolved_at/candles, guard INSERT price fallback fix.
  v0.15.9 (2026-03-30): DB hardening (constraints, indexes, ON CONFLICT), Bybit Proxy
                         security (Tailscale bind), dashboard error handling fix.
  v0.15.2 (2026-03-30): TP1 Normalizer, Configurable Partial Closes, Forensic Analytics.
  v0.15.1 (2026-03-29): Bybit WebSocket Price Feed (shadow), 30s cycle, 8min SL cooldown.
  v0.15.0 (2026-03-29): Apollo Audit — Smart Size Guard, Flow Control, Full Exit Fallbacks.
  v0.14.5 (2026-03-29): Graceful Reversal Protocol, Anti-Whipsaw Guard.
  v0.14.4 (2026-03-29): Institutional Risk Metrics (Sharpe, Sortino, MaxDD, PF, Calmar).
  v0.14.2 (2026-03-26): Shadow PnL Tracker, Scoring Quality UI, 6 ML feature cols,
                         Observability Suite, Repeat Signal Boost, noise suppression.
  v0.12.0 (2026-03-25): Adaptive Dumalka — Portfolio Heat, Dynamic Moonbag, Expanded MC.
  v0.10.1 (2026-03-24): Orderbook Shielding, Binance Sentinel.
  v0.9.3  (2026-03-23): Time-Decay Exits, Per-Symbol Circuit Breaker.
"""
import time
import logging
import asyncio
import json
import secrets
from datetime import datetime, timezone
import httpx
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, pg_executemany, get_db_pool, init_pg_pool, close_pg_pool
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Body
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import (
    RiskRequest, RiskResult, EnrichedRiskResult,
    WebhookPayload, SignalRecord, TradeOutcomePayload,
    Portfolio, CandidateTrade, MarketData, RiskLimits,
)
from core.monte_carlo import run_monte_carlo_risk
from config import config
from db import init_db, insert_signal, get_recent_signals, get_analysis_stats, register_open_position, insert_trade_outcome, get_effectiveness_stats
from bybit import fetch_market_data, fetch_orderbook_depth, fetch_multi_timeframe, fetch_funding_and_oi
from scoring_v2 import compute_signal_score  # v0.9: Strategy Pattern (TIER 1)
from regime_detector import update_regime, get_scoring_adjustments, get_cached_regime
from position_tracker import track_open_positions, admin_force_close_stale_position
from core.sentinel import start_sentinel, get_sentinel_status
from core.bybit_ws import start_price_feed, get_price_feed_status, get_ws_prices
from portfolio_allocator import check_portfolio_limits
import exit_quality  # v0.18.0: Exit Decision Quality Tracker

# --- Telegram Helper (extracted to notifications.py, re-exported for backward compat) ---
from notifications import send_telegram_alert, send_telegram_message, send_signal_report, send_trade_event_report  # noqa: F401

import logging
from logging.handlers import RotatingFileHandler
from pythonjsonlogger import jsonlogger
import os

# ─── App Setup ────────────────────────────────────────────────

os.makedirs('logs', exist_ok=True)

# Set up JSON logger with rotation
log_handler = RotatingFileHandler('logs/app.log', maxBytes=100*1024*1024, backupCount=30)
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
log_handler.setFormatter(formatter)

# Set up console output as well
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s'))

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(log_handler)
root_logger.addHandler(console_handler)

# v0.14.2: Suppress httpx/httpcore noise (was ~70% of log volume)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger("risk-engine")


# v0.14.2: Structured trace context for JSON logs
class TraceContextFilter(logging.Filter):
    """Injects trace_id and symbol into every log record for structured JSON querying.
    Usage: jq 'select(.trace_id == "abc123")' logs/app.log"""
    def filter(self, record):
        """
        Inject structured logging trace context (trace_id, symbol, duration_ms) 
        into log records for observability aggregation.
        """
        if not hasattr(record, 'trace_id'):
            record.trace_id = None
        if not hasattr(record, 'symbol'):
            record.symbol = None
        if not hasattr(record, 'duration_ms'):
            record.duration_ms = None
        return True

log_handler.addFilter(TraceContextFilter())

# Update formatter to include structured fields
formatter = jsonlogger.JsonFormatter(
    '%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s %(symbol)s %(duration_ms)s'
)
log_handler.setFormatter(formatter)


app = FastAPI(
    title="Risk Engine API",
    description="GPU-accelerated Monte Carlo Risk Analysis + Signal Validation",
    version="0.19.9",
)

# ─── v0.14.2: Operational Metrics ─────────────────────────────────────────────
import uuid
from collections import defaultdict

_op_metrics = {
    "startup_time": time.time(),
    "request_count": 0,
    "status_counts": defaultdict(int),  # {"2xx": N, "4xx": N, "5xx": N}
    "slow_requests": 0,  # >5s
    "errors": [],  # last 50 errors
}


@app.middleware("http")
async def observability_middleware(request, call_next):
    """v0.14.2: Request tracking — ID correlation, error rate, latency."""
    request_id = str(uuid.uuid4())[:8]
    t_start = time.perf_counter()

    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - t_start) * 1000, 1)

        # Track metrics
        _op_metrics["request_count"] += 1
        bucket = f"{response.status_code // 100}xx"
        _op_metrics["status_counts"][bucket] += 1

        if duration_ms > 5000:
            _op_metrics["slow_requests"] += 1
            logger.warning(
                f"Slow request: {request.method} {request.url.path} → {response.status_code}",
                extra={"duration_ms": duration_ms, "trace_id": request_id}
            )

        # Add request_id header for client-side correlation
        response.headers["X-Request-ID"] = request_id
        return response

    except Exception as e:
        _op_metrics["status_counts"]["5xx"] += 1
        _op_metrics["errors"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "path": str(request.url.path),
            "error": str(e)[:200],
        })
        # Keep only last 50 errors
        if len(_op_metrics["errors"]) > 50:
            _op_metrics["errors"] = _op_metrics["errors"][-50:]
        logger.error(
            f"Request failed: {request.method} {request.url.path}: {e}",
            extra={"trace_id": request_id}
        )
        raise


# v0.9.4 FIX-4: Background task heartbeat registry
# Tracks last-success timestamp for each background loop so /health can detect stale tasks
_task_heartbeats: dict[str, dict] = {}

@app.on_event("startup")
async def startup_event():
    """
    FastAPI startup hook.
    Initializes PostgreSQL connection pool, ensures schema migrations,
    runs one-shot backfills, and spins up 10 background loops.

    v0.18.0 2026-04-01: exit_quality_backfill_loop (Exit Decision Quality Tracker).
    v0.17.0 2026-03-31: same-coin re-entry protocol (REENTRY_ENABLED, REENTRY_COOLDOWN_HOURS).
    v0.16.1 2026-03-31: added score correction backfill (win_rate bug retrofix),
        shadow_resolved_at/candles columns, corrected_score/recommendation columns.
    v0.16.0 2026-03-31: added shadow recheck 2nd pass, midas benchmark endpoint.
    """
    await init_pg_pool()
    await init_db()
    # Warm up CuPy JIT (first MC call can take 10-30s for CUDA compilation)
    logger.info("Warming up GPU (CuPy JIT compilation)...")
    try:
        from models import Portfolio, CandidateTrade, MarketData, RiskLimits
        await asyncio.to_thread(
            run_monte_carlo_risk,
            portfolio=Portfolio(equity=10000, positions=[]),
            candidate=CandidateTrade(symbol="BTCUSDT", side="long", size=0.001),
            market=MarketData(prices={"BTCUSDT": 70000}, volatility={"BTCUSDT": 0.5}),
            limits=RiskLimits(),
            n_scenarios=1000,  # small — just triggers JIT compilation
        )
        logger.info("GPU warm-up complete ✓")
    except Exception as e:
        logger.error(f"GPU warm-up failed: {e}")
    asyncio.create_task(sample_gpu_load())
    asyncio.create_task(track_open_positions())
    asyncio.create_task(precompute_analytics_loop())
    asyncio.create_task(refresh_bybit_pnl_loop())
    logger.info("🚀 Core background tasks launched: position_tracker, analytics_precompute, bybit_pnl_refresh, gpu_sampler")

    # v0.14.2: Shadow PnL — track rejected signal outcomes for scoring calibration
    # v0.16.0 2026-03-31: added shadow_resolved_at, shadow_resolved_candles
    # v0.16.1 2026-03-31: added score_quality_penalty, corrected_score, corrected_recommendation
    try:
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_pnl_1h REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_pnl_4h REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_outcome TEXT")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_checked_at TIMESTAMPTZ")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_resolved_at TIMESTAMPTZ")          # v0.16.0 2026-03-31
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS shadow_resolved_candles INTEGER")          # v0.16.0 2026-03-31
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_quality_penalty REAL")                # v0.16.1 2026-03-31
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS corrected_score REAL")                      # v0.16.1 2026-03-31
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS corrected_recommendation TEXT")             # v0.16.1 2026-03-31
        # v0.14.2: ML feature columns (previously fetched but not persisted)
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS funding_rate REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS oi_change_pct REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS market_regime TEXT")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS spread_pct REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS slippage_pct REAL")
        await pg_execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS multi_tf_trends_json TEXT")
        # v0.13.2: ML Intelligence features (position_snapshots)
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS btc_change_1h REAL")
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS rsi_14 REAL")
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS orderbook_imbalance REAL")
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS long_short_ratio REAL")
        # v0.18.1: E_pnl Reform — full expected PnL and skewness for ML training
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS full_e_pnl REAL")
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS pnl_skewness REAL")
        # v0.19.2: Multi-horizon ML columns for rocket detection
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS future_pnl_12h DOUBLE PRECISION")
        await pg_execute("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS future_pnl_max_24h DOUBLE PRECISION")
        # v0.19.2: Peak tracking in what_if_outcomes
        await pg_execute("ALTER TABLE what_if_outcomes ADD COLUMN IF NOT EXISTS max_pnl_24h REAL")
        await pg_execute("ALTER TABLE what_if_outcomes ADD COLUMN IF NOT EXISTS max_pnl_24h_hour INTEGER")
        logger.info("Shadow PnL + ML feature columns ensured ✓")
    except Exception as e:
        logger.warning(f"Shadow PnL migration note: {e}")

    # ── v0.19.0: Kline Storage + Scout Signal tables ──────────────────────
    try:
        await pg_execute("""
            CREATE TABLE IF NOT EXISTS klines_history (
                symbol      TEXT NOT NULL,
                timeframe   TEXT NOT NULL,
                open_time   BIGINT NOT NULL,
                open        DOUBLE PRECISION,
                high        DOUBLE PRECISION,
                low         DOUBLE PRECISION,
                close       DOUBLE PRECISION,
                volume      DOUBLE PRECISION,
                turnover    DOUBLE PRECISION,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (symbol, timeframe, open_time)
            )
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_klines_sym_tf_time
            ON klines_history (symbol, timeframe, open_time DESC)
        """)
        await pg_execute("""
            CREATE TABLE IF NOT EXISTS scout_signals (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                entry_price     DOUBLE PRECISION,
                stop_loss       DOUBLE PRECISION,
                tp1             DOUBLE PRECISION,
                tp3             DOUBLE PRECISION,
                signal_type     TEXT,
                regime          TEXT,
                confidence      DOUBLE PRECISION,
                funding_rate    DOUBLE PRECISION,
                oi_change_pct   DOUBLE PRECISION,
                spread_pct      DOUBLE PRECISION,
                rsi_14          DOUBLE PRECISION,
                volume_ratio    DOUBLE PRECISION,
                btc_change_1h   DOUBLE PRECISION,
                multi_tf_trends TEXT,
                accumulation_score DOUBLE PRECISION,
                shadow_pnl_1h   DOUBLE PRECISION,
                shadow_pnl_4h   DOUBLE PRECISION,
                shadow_pnl_24h  DOUBLE PRECISION,
                shadow_outcome  TEXT,
                resolved_at     TIMESTAMPTZ
            )
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_scout_created
            ON scout_signals (created_at DESC)
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_scout_symbol
            ON scout_signals (symbol, created_at DESC)
        """)
        # v0.19.1: Additional Scout ML features
        for col, ctype in [
            ("long_short_ratio", "DOUBLE PRECISION"),
            ("atr_pct", "DOUBLE PRECISION"),
            ("ema_distance_pct", "DOUBLE PRECISION"),
            ("price_change_4h", "DOUBLE PRECISION"),
        ]:
            await pg_execute(f"ALTER TABLE scout_signals ADD COLUMN IF NOT EXISTS {col} {ctype}")

        # v0.19.1: Derivatives time-series for funding curve / OI trend analysis
        await pg_execute("""
            CREATE TABLE IF NOT EXISTS derivatives_snapshots (
                id              SERIAL PRIMARY KEY,
                ts              TIMESTAMPTZ DEFAULT NOW(),
                symbol          TEXT NOT NULL,
                funding_rate    DOUBLE PRECISION,
                oi_value        DOUBLE PRECISION,
                long_short_ratio DOUBLE PRECISION,
                price           DOUBLE PRECISION,
                oi_change_pct   DOUBLE PRECISION
            )
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_deriv_symbol_ts
            ON derivatives_snapshots (symbol, ts DESC)
        """)
        logger.info("Kline + Scout + Derivatives tables ensured ✓")
    except Exception as e:
        logger.warning(f"Kline/Scout table creation note: {e}")

    # ── v0.16.1 2026-03-31: One-shot corrected_score backfill ──────────────
    # Retrofix for win_rate parsing bug: bot regex failed on "67.%" format,
    # causing midas_win_rate=NULL → scoring_v2 applied data quality penalty
    # (×0.85 per missing field). Corrected score reverses the penalty to show
    # what the recommendation WOULD have been with proper data.
    try:
        unfixed = await pg_fetch_one(
            "SELECT count(*) as n FROM signals WHERE corrected_score IS NULL AND re_signal_score > 0"
        )
        if unfixed and unfixed["n"] > 0:
            logger.info(f"🔧 Score correction backfill: {unfixed['n']} signals need corrected_score")
            await pg_execute("""
                UPDATE signals SET
                    score_quality_penalty = CASE
                        WHEN midas_win_rate IS NULL AND midas_probability IS NULL THEN 0.70
                        WHEN midas_win_rate IS NULL OR midas_probability IS NULL THEN 0.85
                        ELSE 1.0
                    END,
                    corrected_score = CASE
                        WHEN midas_win_rate IS NULL AND midas_probability IS NULL
                            THEN LEAST(re_signal_score / 0.70, 1.0)
                        WHEN midas_win_rate IS NULL OR midas_probability IS NULL
                            THEN LEAST(re_signal_score / 0.85, 1.0)
                        ELSE re_signal_score
                    END,
                    corrected_recommendation = CASE
                        WHEN midas_win_rate IS NULL AND midas_probability IS NULL THEN
                            CASE WHEN LEAST(re_signal_score / 0.70, 1.0) >= 0.60 THEN 'approve'
                                 WHEN LEAST(re_signal_score / 0.70, 1.0) >= 0.45 THEN 'reduce'
                                 ELSE 'reject' END
                        WHEN midas_win_rate IS NULL OR midas_probability IS NULL THEN
                            CASE WHEN LEAST(re_signal_score / 0.85, 1.0) >= 0.60 THEN 'approve'
                                 WHEN LEAST(re_signal_score / 0.85, 1.0) >= 0.45 THEN 'reduce'
                                 ELSE 'reject' END
                        ELSE re_recommendation
                    END
                WHERE corrected_score IS NULL AND re_signal_score > 0
            """)
            changed = await pg_fetch_one("""
                SELECT count(*) as n FROM signals
                WHERE corrected_recommendation != re_recommendation
            """)
            logger.info(f"🔧 Score correction backfill done. {changed['n'] if changed else 0} signals would have had different recommendation.")
    except Exception as e:
        logger.warning(f"Score correction backfill note: {e}")

    asyncio.create_task(shadow_pnl_backfill_loop())

    # v0.8.1: Watchlist Scanner — autonomous dump/pump detector
    if config.WATCHLIST_SCANNER_ENABLED:
        from watchlist_scanner import scan_watchlist
        asyncio.create_task(scan_watchlist())
        logger.info(f"Watchlist Scanner enabled: {len(config.WATCHLIST_SYMBOLS)} symbols")

    # v0.10.1: Accumulation Scanner — "Coiled Spring" detector
    from core.accumulation_scanner import accumulation_loop
    asyncio.create_task(accumulation_loop())
    logger.info("Accumulation Scanner background task launched")

    # v0.10.1: Binance Sentinel — Flash Crash Monitor
    asyncio.create_task(start_sentinel(
        emergency_callback=_sentinel_emergency_close,
        alert_callback=_sentinel_alert,
    ))
    logger.info("🛡️ Binance Sentinel background task launched")

    # v0.15.1: Bybit WebSocket Price Feed (SHADOW MODE)
    def _get_active_symbols():
        """Return all traded symbols for WS subscription (full portfolio)."""
        result = set()
        try:
            import psycopg2
            conn = psycopg2.connect(config.DATABASE_URL)
            conn.autocommit = True
            cur = conn.cursor()
            # All symbols we've ever traded + currently open
            cur.execute("SELECT DISTINCT symbol FROM open_positions")
            result = {row[0] for row in cur.fetchall()}
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug(f"[BYBIT-WS] Could not fetch symbols: {e}")
            result = {"BTCUSDT", "ETHUSDT"}
        return result if result else {"BTCUSDT", "ETHUSDT"}

    asyncio.create_task(start_price_feed(get_active_symbols=_get_active_symbols))
    logger.info("📡 Bybit WebSocket Price Feed launched (SHADOW MODE)")

    # v0.18.0: Exit Decision Quality Tracker — backfill loop
    asyncio.create_task(exit_quality.exit_quality_backfill_loop())
    logger.info("📊 Exit Quality backfill loop registered")

    # v0.18.6: ML Labeler — periodic optimal_action backfill for training data
    asyncio.create_task(ml_labeler_loop())
    logger.info("🏷️ ML Labeler loop registered (interval=6h)")

    # v0.19.0: Kline Historical Collector — stores 15m/1h/4h candles
    if config.KLINE_COLLECTOR_ENABLED:
        from kline_collector import kline_collector_loop
        asyncio.create_task(kline_collector_loop())
        logger.info(f"📊 Kline Collector enabled: 3 TF x {len(config.WATCHLIST_SYMBOLS)} symbols")
    else:
        logger.info("📊 Kline Collector DISABLED (KLINE_COLLECTOR_ENABLED=false)")

    # v0.19.0: Scout Shadow Signal Generator — autonomous EMA/RSI/Regime signals
    if config.SCOUT_ENABLED:
        from scout import scout_loop
        asyncio.create_task(scout_loop())
        logger.info("🔭 Scout Signal Generator enabled (SHADOW MODE — no trading)")
    else:
        logger.info("🔭 Scout Signal Generator DISABLED (SCOUT_ENABLED=false)")

    # ── v0.19.6 (2026-04-08): ML Shadow Mode — load ExtraTrees model ─────────
    # Logs predictions to ml_predictions table without affecting trading decisions.
    # Model: ExtraTrees+Optuna trained on position_snapshots (see EXP-7).
    try:
        await pg_execute("""
            CREATE TABLE IF NOT EXISTS ml_predictions (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                pos_id          INTEGER NOT NULL,
                symbol          TEXT NOT NULL,
                side            TEXT,
                snapshot_count  INTEGER,
                prob_profit     DOUBLE PRECISION,
                prediction      TEXT,
                model_version   TEXT,
                features_json   TEXT
            )
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_pred_pos
            ON ml_predictions (pos_id, created_at DESC)
        """)
        await pg_execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_pred_created
            ON ml_predictions (created_at DESC)
        """)
        logger.info("ml_predictions table ensured ✓")
    except Exception as e:
        logger.warning(f"ml_predictions table note: {e}")

    if config.ML_SHADOW_ENABLED:
        try:
            import joblib as _jl
            _model_path = Path(__file__).parent / "models" / "et_shadow_v1.pkl"
            if _model_path.exists():
                _bundle = _jl.load(_model_path)
                import position_tracker as _pt
                _pt._ml_shadow_model = _bundle["model"]
                _pt._ml_shadow_imputer = _bundle["imputer"]
                _pt._ml_shadow_features = _bundle["features"]
                _pt._ml_shadow_numeric = _bundle["numeric_features"]
                _pt._ml_shadow_version = _bundle.get("version", "unknown")
                already = await pg_fetch_all(
                    "SELECT DISTINCT pos_id FROM ml_predictions"
                )
                if already:
                    _pt._ml_shadow_logged = {r["pos_id"] for r in already}
                logger.info(
                    f"ML shadow model loaded: {_bundle.get('version')}, "
                    f"trained on {_bundle.get('n_positions')} positions, "
                    f"LOO AUC={_bundle.get('loo_auc')}, "
                    f"already predicted: {len(_pt._ml_shadow_logged)} positions"
                )
            else:
                logger.info(f"ML shadow model not found at {_model_path} — run train_shadow_model.py first")
        except Exception as e:
            logger.warning(f"ML shadow model not loaded (non-critical): {e}")
    else:
        logger.info("ML Shadow Mode DISABLED (ML_SHADOW_ENABLED=false)")

    # v0.19.7: Health Watchdog — TG alerts on component failure / bot disconnect
    if config.HEALTH_WATCHDOG_ENABLED:
        from health_watchdog import health_watchdog_loop
        asyncio.create_task(health_watchdog_loop())
        logger.info(
            "🩺 Health Watchdog enabled (cycle=60s, bot_alert=%ds, cooldown=%ds)",
            config.BOT_UNREACHABLE_ALERT_SEC,
            config.WATCHDOG_ALERT_COOLDOWN_SEC,
        )
    else:
        logger.info("🩺 Health Watchdog DISABLED (HEALTH_WATCHDOG_ENABLED=false)")


async def _sentinel_emergency_close(change_pct: float, price: float, old_price: float):
    """Called by Binance Sentinel when BTC drops ≥1% in 5s."""
    from notifications import send_telegram_alert
    await send_telegram_alert(
        f"🚨 SENTINEL EMERGENCY: BTC -{abs(change_pct):.1f}% flash crash "
        f"(${old_price:.0f} → ${price:.0f}). Monitoring all positions!"
    )
    # NOTE: Full auto-close disabled for safety. Currently alert-only.
    # When ready for auto-close, uncomment:
    # from position_tracker import emergency_close_all
    # await emergency_close_all(reason=f"SENTINEL: BTC flash crash {change_pct:.1f}%")


async def _sentinel_alert(change_pct: float, price: float, old_price: float):
    """Called by Binance Sentinel when BTC drops ≥0.5% in 5s (non-critical)."""
    logger.warning(
        f"⚠️ [SENTINEL ALERT] BTC drop {change_pct:.2f}%: "
        f"${old_price:.0f} → ${price:.0f}"
    )

# ── Background analytics precomputation (heavy JOINs on 34K+ snapshots) ──
async def precompute_analytics_loop():
    """Precompute heavy materialized views every hour and on startup."""
    await asyncio.sleep(10)  # wait for DB init
    logger.info("📊 Analytics precompute loop started (interval=1h)")
    from core.opportunity_cost import compute_post_trade_excursions
    while True:
        t0 = time.time()
        logger.info("⏳ Analytics precompute starting...")
        try:
            await asyncio.wait_for(precompute_heavy_analytics(), timeout=120)
            await asyncio.wait_for(compute_post_trade_excursions(), timeout=600)
            logger.info(f"✅ Analytics materialized views refreshed in {time.time()-t0:.1f}s")
            _task_heartbeats["analytics_precompute"] = {"last_success": time.time(), "duration": round(time.time()-t0, 1)}
        except asyncio.TimeoutError:
            logger.error(f"❌ Analytics precompute TIMED OUT after {time.time()-t0:.1f}s")
            _task_heartbeats["analytics_precompute"] = {"last_error": time.time(), "error": "timeout"}
        except Exception as e:
            logger.error(f"❌ Analytics precompute error after {time.time()-t0:.1f}s: {e}")
            _task_heartbeats["analytics_precompute"] = {"last_error": time.time(), "error": str(e)[:100]}
        await asyncio.sleep(3600)  # every hour


ML_LABELER_INTERVAL = 6 * 3600  # 6 hours

async def ml_labeler_loop():
    """Periodic backfill of optimal_action labels for ML training.

    Runs every 6h. Labels position_snapshots with optimal_action (hold/close/partial_close)
    based on future_pnl data for all closed positions.

    v0.18.6 (2026-04-04): Added as automated background task.
    """
    await asyncio.sleep(120)  # wait for DB + position tracker init
    from db_adapter import _sync_fetch_all, _sync_executemany
    logger.info("🏷️ ML Labeler loop started (interval=6h)")

    while True:
        t0 = time.time()
        try:
            labeled, skipped = await asyncio.to_thread(_run_ml_labeler, _sync_fetch_all, _sync_executemany)
            elapsed = time.time() - t0
            logger.info(f"🏷️ ML Labeler: {labeled} labeled, {skipped} skipped in {elapsed:.1f}s")
            _task_heartbeats["ml_labeler"] = {"last_success": time.time(), "labeled": labeled}
        except Exception as e:
            logger.error(f"❌ ML Labeler error: {e}")
            _task_heartbeats["ml_labeler"] = {"last_error": time.time(), "error": str(e)[:100]}
        await asyncio.sleep(ML_LABELER_INTERVAL)


def _run_ml_labeler(fetch_all, executemany):
    """Sync labeling logic (runs in thread via asyncio.to_thread)."""
    closed = fetch_all(
        "SELECT id FROM open_positions WHERE status = 'closed' ORDER BY id"
    )
    total_labeled = 0
    total_skipped = 0

    for pos in closed:
        pos_id = pos["id"]
        snaps = fetch_all(
            """SELECT id, pnl_pct, max_pnl_pct, tp_progress_pct,
                      future_pnl_1h, future_pnl_4h, zone
               FROM position_snapshots
               WHERE pos_id = %s AND optimal_action IS NULL
                 AND (future_pnl_1h IS NOT NULL OR future_pnl_4h IS NOT NULL)
               ORDER BY id ASC""",
            (pos_id,)
        )
        if not snaps:
            continue

        batch = []
        for s in snaps:
            future_pnl = s["future_pnl_1h"] if s["future_pnl_1h"] is not None else s["future_pnl_4h"]
            tp_prog = s["tp_progress_pct"] or 0
            zone = s["zone"] or 0

            if future_pnl < -1.0:
                label = "close"
            elif future_pnl < -0.3 and tp_prog > 50:
                label = "partial_close"
            elif future_pnl < -0.3 and zone >= 2:
                label = "partial_close"
            elif future_pnl > 0.5:
                label = "hold"
            else:
                label = "hold"

            batch.append((label, s["id"]))

        if batch:
            executemany(
                "UPDATE position_snapshots SET optimal_action = %s WHERE id = %s",
                batch,
            )
            total_labeled += len(batch)

    return total_labeled, total_skipped


async def precompute_heavy_analytics():
    """Compute MC accuracy + Volatility JOINs and store in analytics_cache.

    v0.19.5-fix (2026-04-08): three data-quality fixes:
    1) Entry snapshot: MIN(id) → first snapshot_at >= opened_at.
       Old method picked stale snapshots for ~18% of positions.
    2) MC Accuracy: restrict to snapshot_at >= 2026-04-02.  Before that
       date MC wrote sentinels (p_sl=1.0/0.3/0.05, p_tp=0.1) instead
       of real simulations.  ~132 of 334 positions had fake MC.
    3) Volatility→Outcome: MC filter removed (volatility is independent
       of MC correctness), broader dataset (280 vs 159 rows).
    """
    import json as _json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    # 1. MC Accuracy (JOIN position_snapshots × open_positions)
    try:
        t1 = time.time()
        logger.info("  📊 Precomputing MC Accuracy...")
        mc_rows = await pg_fetch_all("""
            SELECT ps.mc_p_tp, op.realized_pnl_pct
            FROM (
                SELECT DISTINCT ON (ps2.pos_id) ps2.pos_id, ps2.mc_p_tp
                FROM position_snapshots ps2
                JOIN open_positions p2 ON p2.id = ps2.pos_id
                WHERE ps2.snapshot_at >= p2.opened_at
                  AND ps2.snapshot_at >= '2026-04-02'
                  AND (ps2.mc_p_tp + ps2.mc_p_sl) <= 1.01
                ORDER BY ps2.pos_id, ps2.snapshot_at ASC, ps2.id ASC
            ) ps
            JOIN open_positions op ON ps.pos_id = op.id
            WHERE op.status = 'closed' AND ps.mc_p_tp IS NOT NULL
        """)
        mc_buckets = {}
        for r in mc_rows:
            p_tp = r["mc_p_tp"] or 0
            bucket = f"{int(p_tp * 10) * 10}-{int(p_tp * 10) * 10 + 10}%"
            if bucket not in mc_buckets:
                mc_buckets[bucket] = {"bucket": bucket, "total": 0, "wins": 0, "pnl_sum": 0}
            mc_buckets[bucket]["total"] += 1
            pnl = r["realized_pnl_pct"] or 0
            mc_buckets[bucket]["pnl_sum"] += pnl
            if pnl > 0: mc_buckets[bucket]["wins"] += 1
        for b in mc_buckets.values():
            b["avg_pnl"] = round(b["pnl_sum"] / max(1, b["total"]), 2)
            b["win_rate"] = round(b["wins"] / max(1, b["total"]) * 100, 1)
            del b["pnl_sum"]
        mc_result = sorted(mc_buckets.values(), key=lambda x: x["bucket"])
        await pg_execute("INSERT INTO analytics_cache (metric_key, data_json, computed_at) VALUES (?, ?, ?) ON CONFLICT (metric_key) DO UPDATE SET data_json = EXCLUDED.data_json, computed_at = EXCLUDED.computed_at",
                         ("mc_accuracy", _json.dumps(mc_result), now))
        logger.info(f"  ✓ MC Accuracy: {len(mc_rows)} rows → {len(mc_result)} buckets ({time.time()-t1:.2f}s)")
    except Exception as e:
        logger.warning(f"  ✗ MC Accuracy failed: {e}")

    # 2. Volatility at Entry → Outcome
    try:
        t2 = time.time()
        logger.info("  📊 Precomputing Volatility→Outcome...")
        vol_rows = await pg_fetch_all("""
            SELECT ps.volatility, op.realized_pnl_pct as pnl
            FROM (
                SELECT DISTINCT ON (ps2.pos_id) ps2.pos_id, ps2.volatility
                FROM position_snapshots ps2
                JOIN open_positions p2 ON p2.id = ps2.pos_id
                WHERE ps2.snapshot_at >= p2.opened_at
                ORDER BY ps2.pos_id, ps2.snapshot_at ASC, ps2.id ASC
            ) ps
            JOIN open_positions op ON ps.pos_id = op.id
            WHERE op.status = 'closed' AND ps.volatility IS NOT NULL
        """)
        vol_buckets = {}
        for r in vol_rows:
            v = r["volatility"] or 0
            bk = "< 0.5" if v < 0.5 else "0.5-1.0" if v < 1.0 else "1.0-1.5" if v < 1.5 else "1.5-2.0" if v < 2.0 else "> 2.0"
            if bk not in vol_buckets:
                vol_buckets[bk] = {"bucket": bk, "total": 0, "wins": 0, "pnl_sum": 0}
            vol_buckets[bk]["total"] += 1
            pnl = r["pnl"] or 0
            vol_buckets[bk]["pnl_sum"] += pnl
            if pnl > 0: vol_buckets[bk]["wins"] += 1
        order = ["< 0.5", "0.5-1.0", "1.0-1.5", "1.5-2.0", "> 2.0"]
        for b in vol_buckets.values():
            b["avg_pnl"] = round(b["pnl_sum"] / max(1, b["total"]), 2)
            b["win_rate"] = round(b["wins"] / max(1, b["total"]) * 100, 1)
            del b["pnl_sum"]
        vol_result = sorted(vol_buckets.values(), key=lambda x: order.index(x["bucket"]) if x["bucket"] in order else 99)
        await pg_execute("INSERT INTO analytics_cache (metric_key, data_json, computed_at) VALUES (?, ?, ?) ON CONFLICT (metric_key) DO UPDATE SET data_json = EXCLUDED.data_json, computed_at = EXCLUDED.computed_at",
                         ("volatility_outcome", _json.dumps(vol_result), now))
        logger.info(f"  ✓ Volatility: {len(vol_rows)} rows → {len(vol_result)} buckets ({time.time()-t2:.2f}s)")
    except Exception as e:
        logger.warning(f"  ✗ Volatility failed: {e}")

    # 3. GPU SL Optimization (150 caps in parallel)
    try:
        t3 = time.time()
        logger.info("  📊 Precomputing GPU SL Optimization...")
        sl_rows = await pg_fetch_all("""
            SELECT realized_pnl_pct FROM open_positions
            WHERE status='closed' AND realized_pnl_pct IS NOT NULL
        """)
        all_pnl = [r["realized_pnl_pct"] for r in sl_rows]
        wins = [p for p in all_pnl if p > 0]
        losses = [p for p in all_pnl if p < 0]
        if losses:
            from core.gpu_analytics import gpu_sl_optimization
            gpu_sl = await asyncio.to_thread(gpu_sl_optimization, all_pnl, wins, losses)
            await pg_execute("INSERT INTO analytics_cache (metric_key, data_json, computed_at) VALUES (?, ?, ?) ON CONFLICT (metric_key) DO UPDATE SET data_json = EXCLUDED.data_json, computed_at = EXCLUDED.computed_at",
                             ("gpu_sl_optimization", _json.dumps(gpu_sl), now))
            logger.info(f"  ✓ GPU SL: {gpu_sl.get('caps_tested')} caps on {gpu_sl.get('device')} ({time.time()-t3:.2f}s)")
    except Exception as e:
        logger.warning(f"  ✗ GPU SL failed: {e}")

# ─── GPU Analytics API ────────────────────────────────────────────────────────

@app.get("/api/gpu-analytics")
async def gpu_analytics_endpoint():
    """GPU-accelerated analytics: batch Monte Carlo + SL optimization + correlation."""
    import json as _json
    result = {}

    try:
        # 1. Batch Monte Carlo for all open positions
        from core.gpu_analytics import gpu_batch_monte_carlo
        positions = await pg_fetch_all("""
            SELECT id, symbol, side, size, entry_price, current_price,
                   current_sl, current_tp1, current_tp3
            FROM open_positions WHERE status = 'open'
        """)

        if positions:
            # Add volatility estimate from recent snapshots
            for p in positions:
                snap = await pg_fetch_one("""
                    SELECT volatility FROM position_snapshots
                    WHERE pos_id = ? ORDER BY id DESC LIMIT 1
                """, (p['id'],))
                p['volatility'] = snap['volatility'] if snap else 0.5

            result['batch_mc'] = await asyncio.to_thread(
                gpu_batch_monte_carlo, positions, 200_000
            )

        # 2. GPU SL optimization from cache
        row = await pg_fetch_one("SELECT data_json FROM analytics_cache WHERE metric_key = 'gpu_sl_optimization'")
        if row:
            result['gpu_sl_optimization'] = _json.loads(row['data_json'])

    except Exception as e:
        logger.warning(f"GPU analytics error: {e}")
        result['error'] = str(e)

    return result

# ── v0.15.1: Bybit WebSocket Price Feed API ──────────────────────────────────
@app.get("/api/ws-prices")
async def ws_prices_endpoint():
    """Real-time prices from Bybit WebSocket feed (shadow mode)."""
    return {
        "status": get_price_feed_status(),
        "prices": get_ws_prices(),
    }

# ─── Dashboard UI ─────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the Risk Engine dashboard UI."""
    html_path = STATIC_DIR / "dashboard.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"), 
        status_code=200,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page():
    """Serve the Risk Engine Analytics page."""
    html_path = STATIC_DIR / "analytics.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        status_code=200,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

# ── Regime API (v0.7.0) ──
@app.get("/api/regime")
async def regime_api():
    """Current market regime classification."""
    return get_cached_regime()

# ── v3.1: Bybit Real PnL Cache ──
# Aggregated real USDT PnL from Bybit, keyed by symbol
_bybit_pnl_cache = {"data": {}, "ts": 0}
BYBIT_PNL_CACHE_TTL = 300  # 5 min

@app.get("/api/refresh-bybit-pnl")
async def refresh_bybit_pnl_api():
    """Fetch ALL closed PnL from Bybit, aggregate by symbol, cache the result."""
    from bybit import fetch_closed_pnl
    
    records = await fetch_closed_pnl(limit=100)
    if not records:
        return {"error": "Could not fetch Bybit closed PnL", "count": 0}
    
    # Aggregate by symbol
    by_symbol = {}
    total = 0
    for r in records:
        sym = r.get("symbol", "")
        pnl = r.get("closedPnl", 0)
        total += pnl
        if sym not in by_symbol:
            by_symbol[sym] = {"pnl_usdt": 0, "trades": 0}
        by_symbol[sym]["pnl_usdt"] += pnl
        by_symbol[sym]["trades"] += 1
    
    # Round values
    for sym in by_symbol:
        by_symbol[sym]["pnl_usdt"] = round(by_symbol[sym]["pnl_usdt"], 4)
    
    global _bybit_pnl_cache
    _bybit_pnl_cache = {"data": by_symbol, "ts": time.time()}
    
    # Also clear any wrong per-position backfill data
    await pg_execute("UPDATE open_positions SET realized_pnl_usdt = NULL WHERE realized_pnl_usdt IS NOT NULL")
    
    # Clear analytics cache
    global _analytics_cache
    _analytics_cache = {"data": None, "ts": 0}
    
    return {
        "total_pnl": round(total, 4),
        "by_symbol": by_symbol,
    }

async def refresh_bybit_pnl_loop():
    """Background loop to refresh Bybit PnL cache periodically."""
    await asyncio.sleep(5)  # wait for DB init
    logger.info(f"💱 Bybit PnL refresh loop started (interval={BYBIT_PNL_CACHE_TTL}s)")
    while True:
        try:
            logger.info("⏳ Refreshing Bybit PnL cache...")
            await refresh_bybit_pnl_api()
            _task_heartbeats["bybit_pnl_refresh"] = {"last_success": time.time()}
        except Exception as e:
            logger.error(f"❌ Bybit PnL refresh error: {e}")
            _task_heartbeats["bybit_pnl_refresh"] = {"last_error": time.time(), "error": str(e)[:100]}
        await asyncio.sleep(BYBIT_PNL_CACHE_TTL)


# ── v0.14.2: Shadow PnL — Counterfactual Tracking for Scoring Quality ────────
#
# Purpose: For EVERY signal (approve + reject + reduce), simulate what WOULD
# have happened using the signal's own SL/TP parameters. This enables:
#   - Confusion Matrix: True/False Approve/Reject (using actual SL/TP outcomes)
#   - Calibration Curve: does score=0.6 → 60% profitable?
#   - Threshold Optimization: find optimal approve/reject cutoff
#   - ML Training Labels: shadow_outcome as ground truth for supervised learning
#
# Key insight: We use the signal's stop_loss, tp1, tp3 to determine if the
# trade would have been a winner or loser — the SAME parameters the bot uses.

SHADOW_PNL_INTERVAL_SEC = 300  # 5 minutes between backfill runs
SHADOW_PNL_BATCH_SIZE = 15     # signals per run (rate-limit friendly)

from kline_fetcher import fetch_klines_with_fallbacks  # v0.18.0: extracted to resolve circular import

async def shadow_pnl_backfill_loop():
    """
    Background loop: backfill shadow_pnl_1h/4h and shadow_outcome for past signals.

    Two-phase approach:
      Phase 1 (cheap, every cycle): Snapshot price delta at 1h and 4h marks.
      Phase 2 (SL/TP sim): For signals >4h old with SL/TP, fetch klines and
                            simulate which level would have been hit first.

    Changelog:
      v0.14.2          — initial implementation.
      v0.16.0 2026-03-31 — records shadow_resolved_at/shadow_resolved_candles on
                           SL/TP hit for time-to-resolution analytics.
      v0.16.0 2026-03-31 — added second-pass recheck for stale 'open' signals
                           (fetches klines from shadow_checked_at onward, resolves
                           signals missed by the initial 48h window, batch of 5/cycle).
    """
    await asyncio.sleep(30)  # let other tasks start first
    logger.info("👻 Shadow PnL backfill loop started")

    while True:
        try:
            # ── Unified Deterministic Shadow PnL Backtesting ──
            # Fetch signals lacking complete shadow data, ensuring at least 4.2 hours have passed
            signals_needed = await pg_fetch_all("""
                SELECT id, symbol, side, price_at_signal, stop_loss, tp1, tp3, created_at,
                       shadow_pnl_1h, shadow_pnl_4h, shadow_outcome
                FROM signals
                WHERE (shadow_pnl_4h IS NULL OR shadow_outcome IS NULL)
                  AND price_at_signal > 0
                  AND stop_loss IS NOT NULL AND stop_loss > 0
                  AND (tp1 IS NOT NULL AND tp1 > 0 OR tp3 IS NOT NULL AND tp3 > 0)
                  AND created_at < NOW() - INTERVAL '4.2 hours'
                  AND COALESCE(source, '') != 'backtest_v2'
                ORDER BY id DESC LIMIT ?
            """, (SHADOW_PNL_BATCH_SIZE,))

            outcomes_filled = 0
            for s in signals_needed:
                try:
                    entry = float(s["price_at_signal"])
                    sl = float(s["stop_loss"])
                    tp1 = float(s["tp1"]) if s["tp1"] else 0
                    tp3 = float(s["tp3"]) if s["tp3"] else 0
                    side = s["side"]
                    tp = tp3 if tp3 > 0 else tp1

                    if sl <= 0 or tp <= 0 or entry <= 0:
                        await pg_execute("UPDATE signals SET shadow_outcome = 'no_data' WHERE id = ?", (s["id"],))
                        continue

                    created_at_val = s["created_at"]
                    if isinstance(created_at_val, str):
                        from datetime import datetime
                        created_dt = datetime.fromisoformat(created_at_val.replace('Z', '+00:00'))
                    else:
                        created_dt = created_at_val
                        
                    # Fetch klines starting exactly at created_at minus 15 min buffer
                    start_ts = int(created_dt.timestamp() * 1000) - (15 * 60 * 1000)
                    
                    klines = await fetch_klines_with_fallbacks(s['symbol'], start_ts, 192, "15", "15m")
                    if not klines or len(klines) < 16:  # need at least 4 hours (16 candles)
                        continue

                    # v0.16.1 2026-03-31: sanity check — reject stale/misaligned kline data
                    first_kline_ts = int(klines[0][0])
                    drift_ms = abs(first_kline_ts - start_ts)
                    if drift_ms > 24 * 3600 * 1000:
                        logger.warning(f"👻 Shadow: kline drift {drift_ms/3600000:.1f}h for {s['symbol']} id={s['id']}, skipping")
                        continue

                    outcome = "open"
                    shadow_pnl_1h = None
                    shadow_pnl_4h = None
                    
                    final_pnl = 0.0
                    sl_hit = False
                    tp_hit = False

                    for idx, k in enumerate(klines):
                        high = float(k[2])
                        low = float(k[3])
                        close = float(k[4])

                        # Calculate current floating PnL based on candle close
                        if side == "long":
                            current_pnl = ((close - entry) / entry) * 100
                        else:
                            current_pnl = ((entry - close) / entry) * 100

                        # Capture 1h mark (4th 15m candle)
                        if idx == 4 and shadow_pnl_1h is None:
                            shadow_pnl_1h = current_pnl
                            
                        # Capture 4h mark (16th 15m candle)
                        if idx == 16 and shadow_pnl_4h is None:
                            shadow_pnl_4h = current_pnl

                        # Check hard stops if trade is still alive
                        if not sl_hit and not tp_hit:
                            if side == "long":
                                if low <= sl:
                                    sl_hit = True
                                    outcome = "sl_hit"
                                    final_pnl = ((sl - entry) / entry) * 100
                                elif high >= tp:
                                    tp_hit = True
                                    outcome = "tp_hit"
                                    final_pnl = ((tp - entry) / entry) * 100
                            else: # short
                                if high >= sl:
                                    sl_hit = True
                                    outcome = "sl_hit"
                                    final_pnl = ((entry - sl) / entry) * 100
                                elif low <= tp:
                                    tp_hit = True
                                    outcome = "tp_hit"
                                    final_pnl = ((entry - tp) / entry) * 100
                                    
                            # If stopped out before the time markers, the time marker PnL takes the locked final PnL
                            if sl_hit or tp_hit:
                                resolved_candle_idx = idx
                                if shadow_pnl_1h is None and idx <= 4:
                                    shadow_pnl_1h = final_pnl
                                if shadow_pnl_4h is None and idx <= 16:
                                    shadow_pnl_4h = final_pnl

                    # Fallbacks if it never hit SL/TP but 1h/4h weren't set (e.g. data stopped)
                    if shadow_pnl_1h is None: shadow_pnl_1h = current_pnl
                    if shadow_pnl_4h is None: shadow_pnl_4h = current_pnl

                    resolved_at_val = None
                    resolved_candles_val = None
                    if sl_hit or tp_hit:
                        resolved_candles_val = resolved_candle_idx
                        try:
                            resolved_candle_ts = int(klines[resolved_candle_idx][0])
                            from datetime import datetime, timezone
                            resolved_at_val = datetime.fromtimestamp(resolved_candle_ts / 1000, tz=timezone.utc)
                            if resolved_at_val < created_dt.replace(tzinfo=timezone.utc) if created_dt.tzinfo is None else created_dt:
                                logger.warning(f"👻 Shadow: resolved_at < created_at for {s['symbol']} id={s['id']}, discarding timestamp")
                                resolved_at_val = None
                                resolved_candles_val = None
                        except Exception:
                            pass

                    await pg_execute("""
                        UPDATE signals
                        SET shadow_outcome = ?, shadow_pnl_1h = ?, shadow_pnl_4h = ?, shadow_checked_at = NOW(),
                            shadow_resolved_at = COALESCE(?, shadow_resolved_at),
                            shadow_resolved_candles = COALESCE(?, shadow_resolved_candles)
                        WHERE id = ?
                    """, (outcome, round(shadow_pnl_1h, 4), round(shadow_pnl_4h, 4),
                          resolved_at_val, resolved_candles_val, s["id"]))
                    outcomes_filled += 1

                    logger.debug(f"👻 Shadow sim: {s['symbol']} {side} score→{outcome} 1h={shadow_pnl_1h:.2f}% 4h={shadow_pnl_4h:.2f}%")

                except Exception as e:
                    logger.debug(f"Shadow sim error for signal {s['id']}: {e}")

            if outcomes_filled > 0:
                logger.info(f"👻 Shadow PnL backfill: Processed {outcomes_filled} chronological deterministic simulations.")

            # ── Second pass: re-check stale "open" signals (SL/TP not hit in first 48h window) ──
            stale_open = await pg_fetch_all("""
                SELECT id, symbol, side, price_at_signal, stop_loss, tp1, tp3, created_at, shadow_checked_at
                FROM signals
                WHERE shadow_outcome = 'open'
                  AND shadow_checked_at IS NOT NULL
                  AND shadow_checked_at < NOW() - INTERVAL '24 hours'
                  AND price_at_signal > 0
                  AND stop_loss IS NOT NULL AND stop_loss > 0
                  AND (tp1 IS NOT NULL AND tp1 > 0 OR tp3 IS NOT NULL AND tp3 > 0)
                  AND COALESCE(source, '') != 'backtest_v2'
                ORDER BY shadow_checked_at ASC LIMIT 5
            """)

            recheck_filled = 0
            for s in stale_open:
                try:
                    entry = float(s["price_at_signal"])
                    sl = float(s["stop_loss"])
                    tp1_v = float(s["tp1"]) if s["tp1"] else 0
                    tp3_v = float(s["tp3"]) if s["tp3"] else 0
                    side = s["side"]
                    tp = tp3_v if tp3_v > 0 else tp1_v

                    if sl <= 0 or tp <= 0 or entry <= 0:
                        continue

                    checked_at = s["shadow_checked_at"]
                    if isinstance(checked_at, str):
                        from datetime import datetime
                        checked_at = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
                    start_ts = int(checked_at.timestamp() * 1000)

                    klines = await fetch_klines_with_fallbacks(s['symbol'], start_ts, 192, "15", "15m")
                    if not klines or len(klines) < 4:
                        await pg_execute("UPDATE signals SET shadow_checked_at = NOW() WHERE id = ?", (s["id"],))
                        continue

                    # v0.16.1 2026-03-31: sanity check — reject stale/misaligned kline data
                    first_kline_ts = int(klines[0][0])
                    drift_ms = abs(first_kline_ts - start_ts)
                    if drift_ms > 24 * 3600 * 1000:
                        logger.warning(f"👻 Shadow recheck: kline drift {drift_ms/3600000:.1f}h for {s['symbol']} id={s['id']}, skipping")
                        await pg_execute("UPDATE signals SET shadow_checked_at = NOW() WHERE id = ?", (s["id"],))
                        continue

                    outcome = "open"
                    resolved_candle_idx = None
                    for idx, k in enumerate(klines):
                        high = float(k[2])
                        low = float(k[3])
                        if side == "long":
                            if low <= sl:
                                outcome = "sl_hit"
                                resolved_candle_idx = idx
                                break
                            elif high >= tp:
                                outcome = "tp_hit"
                                resolved_candle_idx = idx
                                break
                        else:
                            if high >= sl:
                                outcome = "sl_hit"
                                resolved_candle_idx = idx
                                break
                            elif low <= tp:
                                outcome = "tp_hit"
                                resolved_candle_idx = idx
                                break

                    resolved_at_val = None
                    if outcome != "open" and resolved_candle_idx is not None:
                        try:
                            resolved_candle_ts = int(klines[resolved_candle_idx][0])
                            from datetime import datetime, timezone
                            resolved_at_val = datetime.fromtimestamp(resolved_candle_ts / 1000, tz=timezone.utc)
                        except Exception:
                            pass

                    if outcome != "open":
                        if side == "long":
                            final_pnl = ((sl - entry) / entry) * 100 if outcome == "sl_hit" else ((tp - entry) / entry) * 100
                        else:
                            final_pnl = ((entry - sl) / entry) * 100 if outcome == "sl_hit" else ((entry - tp) / entry) * 100

                        created_at_val = s["created_at"]
                        if isinstance(created_at_val, str):
                            from datetime import datetime
                            created_at_val = datetime.fromisoformat(created_at_val.replace('Z', '+00:00'))

                        if resolved_at_val and created_at_val:
                            from datetime import timezone as tz_
                            c = created_at_val.replace(tzinfo=tz_.utc) if created_at_val.tzinfo is None else created_at_val
                            if resolved_at_val < c:
                                logger.warning(f"👻 Shadow recheck: resolved_at < created_at for {s['symbol']} id={s['id']}, discarding")
                                resolved_at_val = None

                        total_candles = None
                        if resolved_at_val and created_at_val:
                            try:
                                total_candles = int((resolved_at_val - created_at_val).total_seconds() / 900)
                            except Exception:
                                pass

                        await pg_execute("""
                            UPDATE signals
                            SET shadow_outcome = ?, shadow_checked_at = NOW(),
                                shadow_resolved_at = ?, shadow_resolved_candles = ?
                            WHERE id = ?
                        """, (outcome, resolved_at_val, total_candles, s["id"]))
                        recheck_filled += 1
                        logger.info(f"👻 Shadow recheck: {s['symbol']} {side} id={s['id']} → {outcome} (extended window)")
                    else:
                        await pg_execute("UPDATE signals SET shadow_checked_at = NOW() WHERE id = ?", (s["id"],))

                except Exception as e:
                    logger.debug(f"Shadow recheck error for signal {s['id']}: {e}")

            if recheck_filled > 0:
                logger.info(f"👻 Shadow recheck: Resolved {recheck_filled} previously-open signals.")

            _task_heartbeats["shadow_pnl_backfill"] = {"last_success": time.time()}

        except Exception as e:
            logger.error(f"Shadow PnL backfill error: {e}")
            _task_heartbeats["shadow_pnl_backfill"] = {"last_error": time.time(), "error": str(e)[:100]}

        await asyncio.sleep(SHADOW_PNL_INTERVAL_SEC)


@app.get("/api/midas-benchmark")
async def midas_benchmark_api():
    """
    Midas signal quality benchmark: compare all-signals PnL vs RE-filtered PnL.

    v0.16.0 2026-03-31: Initial implementation — shadow_pnl_4h aggregation by
        re_recommendation, TP win rates, resolve timing.
    v0.16.1 2026-03-31: Added corrected_* metrics to account for win_rate parsing
        bug (bot regex failed on "67.%" format, causing data quality penalty in
        scoring_v2). corrected_recommendation shows what RE would have decided
        with proper data.
    """
    from db_adapter import pg_fetch_one
    row = await pg_fetch_one("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE shadow_outcome IN ('sl_hit','tp_hit')) as resolved,
            COUNT(*) FILTER (WHERE shadow_outcome = 'open') as still_open,
            ROUND(AVG(shadow_pnl_4h)::numeric, 3) as midas_avg_pnl,
            ROUND(AVG(shadow_pnl_4h) FILTER (WHERE re_recommendation = 'approve')::numeric, 3) as re_approve_avg,
            ROUND(AVG(shadow_pnl_4h) FILTER (WHERE re_recommendation = 'reduce')::numeric, 3) as re_reduce_avg,
            ROUND(AVG(shadow_pnl_4h) FILTER (WHERE re_recommendation = 'reject')::numeric, 3) as re_reject_avg,
            COUNT(*) FILTER (WHERE shadow_outcome = 'tp_hit') as tp_hits,
            COUNT(*) FILTER (WHERE shadow_outcome = 'sl_hit') as sl_hits,
            ROUND(100.0 * COUNT(*) FILTER (WHERE shadow_outcome = 'tp_hit')
                / NULLIF(COUNT(*) FILTER (WHERE shadow_outcome IN ('tp_hit','sl_hit')), 0), 1) as tp_win_rate,
            ROUND(100.0 * COUNT(*) FILTER (WHERE shadow_outcome = 'tp_hit' AND re_recommendation = 'approve')
                / NULLIF(COUNT(*) FILTER (WHERE shadow_outcome IN ('tp_hit','sl_hit') AND re_recommendation = 'approve'), 0), 1) as re_approve_tp_wr,
            ROUND(100.0 * COUNT(*) FILTER (WHERE shadow_outcome = 'tp_hit' AND re_recommendation = 'reject')
                / NULLIF(COUNT(*) FILTER (WHERE shadow_outcome IN ('tp_hit','sl_hit') AND re_recommendation = 'reject'), 0), 1) as re_reject_tp_wr,
            ROUND(AVG(shadow_resolved_candles * 15.0 / 60) FILTER (WHERE shadow_resolved_candles IS NOT NULL)::numeric, 1) as avg_hours_to_resolve,
            -- v0.16.1: corrected metrics (undo win_rate parsing bug penalty)
            ROUND(AVG(shadow_pnl_4h) FILTER (WHERE corrected_recommendation = 'approve')::numeric, 3) as corrected_approve_avg,
            ROUND(AVG(shadow_pnl_4h) FILTER (WHERE corrected_recommendation = 'reject')::numeric, 3) as corrected_reject_avg,
            COUNT(*) FILTER (WHERE corrected_recommendation != re_recommendation) as reclassified_count,
            COUNT(*) FILTER (WHERE score_quality_penalty IS NOT NULL AND score_quality_penalty < 1.0) as penalty_affected_count
        FROM signals
        WHERE shadow_pnl_4h IS NOT NULL AND COALESCE(source, '') != 'backtest_v2'
    """)
    if not row:
        return {"error": "no data"}
    delta = None
    corrected_delta = None
    if row.get("midas_avg_pnl") is not None and row.get("re_approve_avg") is not None:
        delta = round(float(row["re_approve_avg"]) - float(row["midas_avg_pnl"]), 3)
    if row.get("midas_avg_pnl") is not None and row.get("corrected_approve_avg") is not None:
        corrected_delta = round(float(row["corrected_approve_avg"]) - float(row["midas_avg_pnl"]), 3)
    return {
        **row,
        "re_delta_vs_midas": delta,
        "corrected_delta_vs_midas": corrected_delta,
        "interpretation": "positive delta = RE filter adds value" if delta and delta > 0
            else "negative delta = RE filter currently reduces returns vs blind Midas",
        "corrected_interpretation": (
            "corrected shows what RE WOULD decide if win_rate was parsed correctly"
        )
    }


@app.get("/api/scoring-quality")
async def scoring_quality_api():
    """
    Comprehensive scoring quality analysis — Confusion Matrix, Calibration,
    Threshold Optimization, Saved/Missed PnL.

    v0.14.2: Uses shadow_pnl for rejected signals + trade_outcomes for approved.
    v0.16.1 2026-03-31: Note — historical data affected by win_rate parsing bug
        (46% of signals). Use /api/midas-benchmark for corrected analytics.

    JSON response structure:
      confusion_matrix: {true_reject, false_reject, true_approve, false_approve}
      calibration_curve: [{score_bucket, win_rate, count, avg_pnl}]
      threshold_analysis: [{threshold, precision, recall, f1, saved_pnl, missed_pnl}]
      summary: {total_signals, coverage, scoring_accuracy, optimal_threshold}
    """
    try:
        # ── 1. Confusion Matrix (4h shadow PnL for rejects, trade outcomes for approves) ──
        confusion = await pg_fetch_one("""
            SELECT
                -- TRUE REJECT: rejected + would have lost money (shadow_pnl_4h <= 0)
                SUM(CASE WHEN re_recommendation = 'reject' AND shadow_pnl_4h <= 0 THEN 1 ELSE 0 END) as true_reject,
                -- FALSE REJECT: rejected + would have made money (shadow_pnl_4h > 0)
                SUM(CASE WHEN re_recommendation = 'reject' AND shadow_pnl_4h > 0 THEN 1 ELSE 0 END) as false_reject,
                -- TRUE APPROVE (Shadow): approved/reduced + would have made money
                SUM(CASE WHEN re_recommendation IN ('approve', 'strong', 'reduce') AND shadow_pnl_4h > 0 THEN 1 ELSE 0 END) as true_approve_shadow,
                -- FALSE APPROVE (Shadow): approved/reduced + would have lost money
                SUM(CASE WHEN re_recommendation IN ('approve', 'strong', 'reduce') AND shadow_pnl_4h <= 0 THEN 1 ELSE 0 END) as false_approve_shadow,
                -- Saved PnL: how much loss we avoided (sum of negative shadow_pnl for rejects)
                SUM(CASE WHEN re_recommendation = 'reject' AND shadow_pnl_4h <= 0 THEN ABS(shadow_pnl_4h) ELSE 0 END) as saved_pnl_pct,
                -- Missed PnL: how much profit we missed (sum of positive shadow_pnl for rejects)
                SUM(CASE WHEN re_recommendation = 'reject' AND shadow_pnl_4h > 0 THEN shadow_pnl_4h ELSE 0 END) as missed_pnl_pct,

                -- Reject avg PnL (what would have happened)
                AVG(CASE WHEN re_recommendation = 'reject' THEN shadow_pnl_4h END) as reject_avg_shadow_pnl,
                -- Reduce avg PnL
                AVG(CASE WHEN re_recommendation = 'reduce' THEN shadow_pnl_4h END) as reduce_avg_shadow_pnl,
                -- Total with shadow data
                COUNT(CASE WHEN shadow_pnl_4h IS NOT NULL THEN 1 END) as shadow_coverage,
                COUNT(*) as total_signals
            FROM signals
            WHERE COALESCE(source, '') != 'backtest_v2'
              AND re_recommendation IS NOT NULL
        """)

        # Approved signals effectiveness (from trade_outcomes)
        approved_stats = await pg_fetch_one("""
            SELECT
                COUNT(DISTINCT t.signal_hash) as total_approved_trades,
                SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as true_approve,
                SUM(CASE WHEN t.pnl_pct <= 0 THEN 1 ELSE 0 END) as false_approve,
                AVG(t.pnl_pct) as approve_avg_pnl
            FROM trade_outcomes t
            JOIN signals s ON s.signal_hash = t.signal_hash
            WHERE t.event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
              AND s.re_recommendation IN ('approve', 'strong')
              AND COALESCE(s.source, '') != 'backtest_v2'
        """)

        # ── 2. Calibration Curve: score buckets vs actual win rate ──
        calibration = await pg_fetch_all("""
            SELECT
                CASE
                    WHEN re_signal_score < 0.30 THEN '0.00-0.30'
                    WHEN re_signal_score < 0.40 THEN '0.30-0.40'
                    WHEN re_signal_score < 0.50 THEN '0.40-0.50'
                    WHEN re_signal_score < 0.60 THEN '0.50-0.60'
                    WHEN re_signal_score < 0.70 THEN '0.60-0.70'
                    ELSE '0.70+'
                END as score_bucket,
                COUNT(*) as count,
                AVG(shadow_pnl_4h) as avg_shadow_pnl,
                SUM(CASE WHEN shadow_pnl_4h > 0 THEN 1 ELSE 0 END) as profitable,
                ROUND(SUM(CASE WHEN shadow_pnl_4h > 0 THEN 1 ELSE 0 END)::numeric
                       / NULLIF(COUNT(*), 0) * 100, 1) as win_rate_pct
            FROM signals
            WHERE shadow_pnl_4h IS NOT NULL
              AND re_signal_score IS NOT NULL
              AND COALESCE(source, '') != 'backtest_v2'
            GROUP BY score_bucket
            ORDER BY score_bucket
        """)

        # ── 3. Threshold Analysis: simulate different cutoffs ──
        threshold_data = await pg_fetch_all("""
            SELECT re_signal_score as score, shadow_pnl_4h as shadow_pnl
            FROM signals
            WHERE shadow_pnl_4h IS NOT NULL
              AND re_signal_score IS NOT NULL
              AND COALESCE(source, '') != 'backtest_v2'
            ORDER BY re_signal_score
        """)

        threshold_analysis = []
        for threshold in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
            above = [r for r in threshold_data if r["score"] >= threshold]
            below = [r for r in threshold_data if r["score"] < threshold]
            if not above and not below:
                continue

            # Precision: of those we'd approve, how many actually profitable?
            tp = sum(1 for r in above if r["shadow_pnl"] > 0)
            fp = sum(1 for r in above if r["shadow_pnl"] <= 0)
            # Recall: of all profitable signals, how many did we approve?
            all_profitable = sum(1 for r in threshold_data if r["shadow_pnl"] > 0)
            fn = sum(1 for r in below if r["shadow_pnl"] > 0)  # profitable we'd reject

            precision = tp / (tp + fp) * 100 if (tp + fp) > 0 else 0
            recall = tp / all_profitable * 100 if all_profitable > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

            # PnL impact
            saved = sum(abs(r["shadow_pnl"]) for r in below if r["shadow_pnl"] <= 0)
            missed = sum(r["shadow_pnl"] for r in below if r["shadow_pnl"] > 0)

            threshold_analysis.append({
                "threshold": threshold,
                "approved_count": len(above),
                "rejected_count": len(below),
                "precision_pct": round(precision, 1),
                "recall_pct": round(recall, 1),
                "f1_score": round(f1, 1),
                "saved_pnl_pct": round(saved, 2),
                "missed_pnl_pct": round(missed, 2),
            })

        # ── 4. Find optimal threshold (max F1) ──
        optimal = max(threshold_analysis, key=lambda x: x["f1_score"]) if threshold_analysis else None

        # ── 5. 1h vs 4h comparison (how fast does the outcome materialize?) ──
        timing = await pg_fetch_one("""
            SELECT
                AVG(shadow_pnl_1h) as avg_pnl_1h,
                AVG(shadow_pnl_4h) as avg_pnl_4h,
                CORR(shadow_pnl_1h, shadow_pnl_4h) as correlation_1h_4h,
                COUNT(CASE WHEN shadow_pnl_1h IS NOT NULL AND shadow_pnl_4h IS NOT NULL THEN 1 END) as both_filled
            FROM signals
            WHERE COALESCE(source, '') != 'backtest_v2'
        """)

        # ── 6. Per-component effectiveness ──
        component_analysis = await pg_fetch_all("""
            SELECT
                s.re_recommendation,
                COUNT(*) as cnt,
                AVG(CASE WHEN shadow_pnl_4h IS NOT NULL THEN shadow_pnl_4h END) as avg_shadow_pnl_4h,
                SUM(CASE WHEN shadow_pnl_4h > 0 THEN 1 ELSE 0 END) as profitable,
                SUM(CASE WHEN shadow_pnl_4h IS NOT NULL THEN 1 ELSE 0 END) as with_shadow
            FROM signals s
            WHERE COALESCE(s.source, '') != 'backtest_v2'
              AND s.re_recommendation IS NOT NULL
            GROUP BY s.re_recommendation
        """)

        # Build summary
        total = confusion.get("total_signals", 0) if confusion else 0
        shadow_cov = confusion.get("shadow_coverage", 0) if confusion else 0
        coverage_pct = round(shadow_cov / total * 100, 1) if total > 0 else 0

        tr = confusion.get("true_reject", 0) or 0 if confusion else 0
        fr = confusion.get("false_reject", 0) or 0 if confusion else 0
        ta_shadow = confusion.get("true_approve_shadow", 0) or 0 if confusion else 0
        fa_shadow = confusion.get("false_approve_shadow", 0) or 0 if confusion else 0
        
        # Original bot actual approval stats
        ta_actual = approved_stats.get("true_approve", 0) or 0 if approved_stats else 0
        fa_actual = approved_stats.get("false_approve", 0) or 0 if approved_stats else 0

        # ACCURACY MUST BE CALCULATED ON PURE SHADOW DATA to be statistically sound
        accuracy = round((tr + ta_shadow) / (tr + fr + ta_shadow + fa_shadow) * 100, 1) if (tr + fr + ta_shadow + fa_shadow) > 0 else None

        return {
            "confusion_matrix": {
                "true_reject": tr,
                "false_reject": fr,
                "true_approve": ta_shadow,
                "false_approve": fa_shadow,
                "accuracy_pct": accuracy,
                "saved_pnl_pct": round(confusion.get("saved_pnl_pct", 0) or 0, 2) if confusion else 0,
                "missed_pnl_pct": round(confusion.get("missed_pnl_pct", 0) or 0, 2) if confusion else 0,
            },
            "by_recommendation": component_analysis,
            "calibration_curve": calibration,
            "threshold_analysis": threshold_analysis,
            "optimal_threshold": optimal,
            "timing": {
                "avg_pnl_1h": round(timing.get("avg_pnl_1h", 0) or 0, 4) if timing else None,
                "avg_pnl_4h": round(timing.get("avg_pnl_4h", 0) or 0, 4) if timing else None,
                "correlation_1h_4h": round(timing.get("correlation_1h_4h", 0) or 0, 3) if timing else None,
                "signals_with_both": timing.get("both_filled", 0) if timing else 0,
            },
            "summary": {
                "total_signals": total,
                "shadow_coverage_pct": coverage_pct,
                "scoring_accuracy_pct": accuracy,
                "optimal_threshold": optimal["threshold"] if optimal else None,
                "optimal_f1": optimal["f1_score"] if optimal else None,
            },
            "approved_stats": {
                "total_trades": approved_stats.get("total_approved_trades", 0) if approved_stats else 0,
                "true_approve": ta_actual,
                "false_approve": fa_actual,
                "avg_pnl_pct": round(approved_stats.get("approve_avg_pnl", 0) or 0, 3) if approved_stats else None,
            },
            "reject_stats": {
                "avg_shadow_pnl_4h": round(confusion.get("reject_avg_shadow_pnl", 0) or 0, 3) if confusion else None,
                "reduce_avg_shadow_pnl_4h": round(confusion.get("reduce_avg_shadow_pnl", 0) or 0, 3) if confusion else None,
            },
        }
    except Exception as e:
        logger.error(f"Scoring quality analysis error: {e}", exc_info=True)
        return {"error": str(e)}


def _get_bybit_pnl(symbol: str) -> float:
    """Get cached Bybit PnL for a symbol (0 if not cached)."""
    return _bybit_pnl_cache.get("data", {}).get(symbol, {}).get("pnl_usdt", 0)

# ── v3.1: Account balance from Bybit ──
@app.get("/api/account")
async def account_api():
    """Wallet balance from Bybit."""
    from bybit import fetch_wallet_balance
    balance = await fetch_wallet_balance()
    if balance:
        return balance
    return {"error": "Could not fetch wallet balance"}

# ── v0.14.4: Sharpe Ratio / Max Drawdown / Risk Metrics ──────────────────────

@app.get("/api/risk-metrics")
async def risk_metrics_api():
    """
    v0.14.4: Institutional-grade portfolio risk metrics.
    Computes Sharpe Ratio, Sortino Ratio, Max Drawdown, Profit Factor,
    and Calmar Ratio from daily PnL data in open_positions.
    
    Sharpe = (mean_daily / std_daily) × √365   (annualized)
    Sortino = (mean_daily / downside_std) × √365
    Max Drawdown = largest peak-to-trough decline in cumulative PnL
    Profit Factor = gross_profit / gross_loss
    Calmar = annualized_return / max_drawdown
    """
    import math
    
    rows = await pg_fetch_all("""
        SELECT date_trunc('day', closed_at)::date as day,
               sum(realized_pnl_pct) as daily_pnl,
               count(*) as trades
        FROM open_positions
        WHERE status != 'open' AND closed_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)
    
    if not rows or len(rows) < 2:
        return {"error": "Not enough data (need 2+ trading days)", "days": len(rows) if rows else 0}
    
    daily_pnls = [float(r["daily_pnl"]) for r in rows]
    n_days = len(daily_pnls)
    
    # -- Sharpe Ratio (annualized) --
    mean_daily = sum(daily_pnls) / n_days
    variance = sum((x - mean_daily) ** 2 for x in daily_pnls) / (n_days - 1)
    std_daily = math.sqrt(variance) if variance > 0 else 0.0001
    sharpe = (mean_daily / std_daily) * math.sqrt(365)
    
    # -- Sortino Ratio (only downside deviation) --
    downside = [x for x in daily_pnls if x < 0]
    if downside:
        downside_var = sum(x ** 2 for x in downside) / len(downside)
        downside_std = math.sqrt(downside_var)
        sortino = (mean_daily / downside_std) * math.sqrt(365) if downside_std > 0 else 0
    else:
        sortino = float('inf')  # No losing days
    
    # -- Max Drawdown (cumulative PnL) --
    cumulative = []
    running = 0.0
    for pnl in daily_pnls:
        running += pnl
        cumulative.append(running)
    
    peak = cumulative[0]
    max_dd = 0.0
    for val in cumulative:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    
    # -- Profit Factor --
    gross_profit = sum(x for x in daily_pnls if x > 0)
    gross_loss = abs(sum(x for x in daily_pnls if x < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float('inf')
    
    # -- Calmar Ratio --
    total_return = sum(daily_pnls)
    annualized_return = (total_return / n_days) * 365
    calmar = round(annualized_return / max_dd, 2) if max_dd > 0 else float('inf')
    
    # -- Win/Loss days --
    win_days = sum(1 for x in daily_pnls if x > 0)
    loss_days = sum(1 for x in daily_pnls if x < 0)
    
    # -- Best / Worst day --
    best_day = max(rows, key=lambda r: float(r["daily_pnl"]))
    worst_day = min(rows, key=lambda r: float(r["daily_pnl"]))
    
    return {
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2) if sortino != float('inf') else "∞",
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor": profit_factor,
        "calmar_ratio": calmar,
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(annualized_return, 1),
        "mean_daily_pnl_pct": round(mean_daily, 3),
        "std_daily_pnl_pct": round(std_daily, 3),
        "trading_days": n_days,
        "win_days": win_days,
        "loss_days": loss_days,
        "best_day": {"date": str(best_day["day"]), "pnl": round(float(best_day["daily_pnl"]), 2), "trades": best_day["trades"]},
        "worst_day": {"date": str(worst_day["day"]), "pnl": round(float(worst_day["daily_pnl"]), 2), "trades": worst_day["trades"]},
        "daily_pnl": [{"date": str(r["day"]), "pnl": round(float(r["daily_pnl"]), 3), "trades": r["trades"]} for r in rows],
    }

# ── Analytics cache (TTL 60s) ──
_analytics_cache = {"data": None, "ts": 0}
ANALYTICS_CACHE_TTL = 60  # seconds

@app.get("/api/analytics")
async def analytics_api():
    """Comprehensive analytics data for the analytics dashboard."""
    global _analytics_cache
    now = time.time()
    if _analytics_cache["data"] is not None and (now - _analytics_cache["ts"]) < ANALYTICS_CACHE_TTL:
        return _analytics_cache["data"]
    result = await _compute_analytics()
    _analytics_cache = {"data": result, "ts": time.time()}
    return result

async def _compute_analytics():
    """
    Heavy background computation logic for the dashboard.
    Aggregates win-rates, PnL per symbol, market regimes, and capture ratios
    directly from PostgreSQL leveraging async offloading.
    """
    _bt_filter = "AND signal_hash NOT IN (SELECT signal_hash FROM signals WHERE source='backtest_v2')"

    # 1. RE Recommendation effectiveness
    by_recommendation = await pg_fetch_all("""
        SELECT s.re_recommendation as rec,
           COUNT(DISTINCT s.signal_hash) as trades,
           AVG(CASE WHEN t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close') THEN t.pnl_pct END) as avg_pnl,
           SUM(CASE WHEN t.pnl_pct > 0 AND t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close') THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN t.pnl_pct <= 0 AND t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close') THEN 1 ELSE 0 END) as losses,
           ROUND(COALESCE(SUM(CASE WHEN t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close') THEN t.pnl_pct END), 0), 2) as sum_pnl_pct,
           ROUND(COALESCE(SUM(
               CASE WHEN t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close')
            THEN op.size * op.entry_price * t.pnl_pct / 100 END
           ), 0), 2) as pnl_usdt
        FROM signals s
        LEFT JOIN trade_outcomes t ON s.signal_hash = t.signal_hash
        LEFT JOIN open_positions op ON s.signal_hash = op.signal_hash AND op.status = 'closed'
        WHERE s.signal_hash IS NOT NULL AND COALESCE(s.source, '') != 'backtest_v2'
        GROUP BY rec
    """)

    # 2. Score bucket analysis
    by_score_bucket = await pg_fetch_all("""
        SELECT CASE WHEN s.re_signal_score >= 0.70 THEN '0.70+ strong'
                WHEN s.re_signal_score >= 0.60 THEN '0.60-0.70 approve'
                WHEN s.re_signal_score >= 0.45 THEN '0.45-0.60 reduce'
                ELSE '<0.45 reject' END as bucket,
           COUNT(DISTINCT s.signal_hash) as trades, AVG(t.pnl_pct) as avg_pnl,
           SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN t.pnl_pct <= 0 THEN 1 ELSE 0 END) as losses
        FROM signals s JOIN trade_outcomes t ON s.signal_hash = t.signal_hash
        WHERE t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close')
          AND s.re_signal_score IS NOT NULL AND s.signal_hash IS NOT NULL
          AND COALESCE(s.source, '') != 'backtest_v2'
        GROUP BY bucket ORDER BY bucket
    """)

    # 3. Position close reasons
    close_reasons = await pg_fetch_all(f"""
        SELECT close_reason, COUNT(*) as cnt, AVG(realized_pnl_pct) as avg_pnl,
               AVG(max_pnl_pct) as avg_max_pnl
        FROM open_positions WHERE status='closed' {_bt_filter}
        GROUP BY close_reason
    """)

    close_reasons_detailed = await pg_fetch_all(f"""
        SELECT COALESCE(close_reason_detailed, close_reason, 'unknown') as reason,
               COUNT(*) as cnt, AVG(realized_pnl_pct) as avg_pnl
        FROM open_positions WHERE status='closed' {_bt_filter}
        GROUP BY reason ORDER BY cnt DESC
    """)

    # 4. Zone distribution
    zone_distribution = await pg_fetch_all("""
        SELECT zone, COUNT(*) as cnt, AVG(pnl_pct) as avg_pnl,
               AVG(drawdown_pct) as avg_dd, AVG(tp_progress_pct) as avg_tp
        FROM position_snapshots GROUP BY zone ORDER BY zone
    """)

    # 5. Per-symbol performance
    by_symbol = await pg_fetch_all(f"""
        SELECT p.symbol, COUNT(*) as trades,
               AVG(p.realized_pnl_pct) as avg_pnl, AVG(p.max_pnl_pct) as avg_max_pnl,
               SUM(CASE WHEN p.realized_pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(COALESCE(SUM(p.realized_pnl_pct), 0), 2) as sum_pnl_pct
        FROM open_positions p LEFT JOIN signals s ON s.signal_hash = p.signal_hash
        WHERE p.status='closed' AND p.realized_pnl_pct IS NOT NULL
          AND COALESCE(s.source, '') != 'backtest_v2'
        GROUP BY p.symbol HAVING COUNT(*) >= 1 ORDER BY sum_pnl_pct DESC
    """)
    for row in by_symbol:
        row["pnl_usdt"] = _get_bybit_pnl(row["symbol"])

    # 6. Daily PnL
    daily_pnl = await pg_fetch_all(f"""
        SELECT DATE(closed_at) as day, SUM(realized_pnl_pct) as total_pnl,
               COUNT(*) as trades,
               SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) as wins
        FROM open_positions WHERE status='closed' AND closed_at IS NOT NULL {_bt_filter}
        GROUP BY day ORDER BY day
    """)

    # 7. Capture ratio
    capture_rows = await pg_fetch_all(f"""
        SELECT AVG(CASE WHEN max_pnl_pct > 0.5 THEN realized_pnl_pct / max_pnl_pct ELSE 0 END) as avg_capture,
               COUNT(*) as total,
               SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) as profitable,
               SUM(CASE WHEN max_pnl_pct > 1.0 AND realized_pnl_pct < 0 THEN 1 ELSE 0 END) as leaked_count,
               SUM(CASE WHEN max_pnl_pct > 1.0 AND realized_pnl_pct < 0 THEN realized_pnl_pct ELSE 0 END) as leaked_pnl_total,
               SUM(CASE WHEN max_pnl_pct > 1.0 AND realized_pnl_pct < 0 THEN max_pnl_pct ELSE 0 END) as potential_saved
        FROM open_positions WHERE status='closed' AND max_pnl_pct > 0.5 {_bt_filter}
    """)
    capture = capture_rows[0] if capture_rows else {}

    # 8. Calibration history
    try:
        calibrations = await pg_fetch_all("SELECT * FROM zone_calibration ORDER BY id DESC LIMIT 5")
    except Exception as e:
        logger.debug(f"Calibration history unavailable: {e}")
        calibrations = []

    # 9. Summary stats
    total_signals = await pg_fetch_val("SELECT COUNT(*) FROM signals WHERE COALESCE(source, '') != 'backtest_v2'") or 0
    total_closed = await pg_fetch_val(f"SELECT COUNT(*) FROM open_positions WHERE status='closed' {_bt_filter}") or 0
    total_snapshots = await pg_fetch_val("SELECT COUNT(*) FROM position_snapshots") or 0

    # 9.5 Time-Decay (v0.9.3)
    time_decay = await pg_fetch_all(f"""
        SELECT 
            CASE 
                WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 1 THEN '0-1h'
                WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 4 THEN '1-4h'
                WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 12 THEN '4-12h'
                WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 24 THEN '12-24h'
                ELSE '24h+'
            END AS duration,
            COUNT(*) AS trades,
            ROUND(AVG(CASE WHEN realized_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate,
            ROUND(SUM(realized_pnl_pct)::numeric, 1) AS total_pnl
        FROM open_positions 
        WHERE status = 'closed' AND closed_at IS NOT NULL AND opened_at IS NOT NULL {_bt_filter}
        GROUP BY 1 ORDER BY 
            CASE 
                WHEN CASE WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 1 THEN '0-1h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 4 THEN '1-4h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 12 THEN '4-12h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 24 THEN '12-24h' ELSE '24h+' END = '0-1h' THEN 1
                WHEN CASE WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 1 THEN '0-1h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 4 THEN '1-4h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 12 THEN '4-12h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 24 THEN '12-24h' ELSE '24h+' END = '1-4h' THEN 2
                WHEN CASE WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 1 THEN '0-1h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 4 THEN '1-4h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 12 THEN '4-12h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 24 THEN '12-24h' ELSE '24h+' END = '4-12h' THEN 3
                WHEN CASE WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 1 THEN '0-1h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 4 THEN '1-4h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 12 THEN '4-12h' WHEN EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 < 24 THEN '12-24h' ELSE '24h+' END = '12-24h' THEN 4
                ELSE 5
            END;
    """)

    # 10. Win rate trend
    daily_wr_raw = await pg_fetch_all(f"""
        SELECT date(closed_at) as day,
               SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
               COUNT(*) as total
        FROM open_positions WHERE status='closed' AND closed_at IS NOT NULL {_bt_filter}
        GROUP BY day ORDER BY day
    """)
    win_rate_trend = []
    for i, d in enumerate(daily_wr_raw):
        window = daily_wr_raw[max(0, i-6):i+1]
        w = sum(x['wins'] for x in window)
        t = sum(x['total'] for x in window)
        win_rate_trend.append({"day": d['day'], "win_rate": round(w / t * 100, 1) if t > 0 else 0, "trades": t})

    result = {
        "summary": {"total_signals": total_signals, "total_closed_positions": total_closed,
                     "total_snapshots": total_snapshots, "capture_ratio": capture},
        "by_recommendation": by_recommendation, "by_score_bucket": by_score_bucket,
        "close_reasons": close_reasons, "close_reasons_detailed": close_reasons_detailed,
        "zone_distribution": zone_distribution, "by_symbol": by_symbol,
        "daily_pnl": daily_pnl, "calibrations": calibrations, "win_rate_trend": win_rate_trend,
        "time_decay": time_decay,
    }

    # ── Enrichments ──
    try:
        try:
            what_if = await pg_fetch_all("""
                SELECT what_if, COUNT(*) as cnt, AVG(exit_pnl) as avg_exit_pnl, AVG(missed_pnl) as avg_missed_pnl
                FROM what_if_outcomes GROUP BY what_if""")
            result["what_if_summary"] = what_if
            hold_time = await pg_fetch_all("""
                SELECT what_if, AVG(hours_to_outcome) as avg_hours, MIN(hours_to_outcome) as min_hours, MAX(hours_to_outcome) as max_hours
                FROM what_if_outcomes WHERE hours_to_outcome IS NOT NULL GROUP BY what_if""")
            result["hold_time_analysis"] = hold_time
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            import json as _json
            row = await pg_fetch_one("SELECT data_json FROM analytics_cache WHERE metric_key = 'mc_accuracy'")
            if row:
                result["mc_accuracy"] = _json.loads(row["data_json"])
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            ht_rows = await pg_fetch_all(f"""
                SELECT COALESCE(close_reason_detailed, close_reason) as reason,
                       AVG(EXTRACT(EPOCH FROM (closed_at::timestamp - opened_at::timestamp))/3600) as avg_hours,
                       MIN(EXTRACT(EPOCH FROM (closed_at::timestamp - opened_at::timestamp))/3600) as min_hours,
                       MAX(EXTRACT(EPOCH FROM (closed_at::timestamp - opened_at::timestamp))/3600) as max_hours,
                       COUNT(*) as cnt
                FROM open_positions WHERE status='closed' AND closed_at IS NOT NULL AND opened_at IS NOT NULL {_bt_filter}
                GROUP BY reason ORDER BY cnt DESC""")
            result["hold_time_by_reason"] = ht_rows
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            tod_rows = await pg_fetch_all(f"""
                SELECT EXTRACT(HOUR FROM opened_at::timestamp)::INTEGER as hour,
                       COUNT(*) as cnt, AVG(realized_pnl_pct) as avg_pnl,
                       SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) as wins
                FROM open_positions WHERE status='closed' AND opened_at IS NOT NULL {_bt_filter}
                GROUP BY hour ORDER BY hour""")
            result["time_of_day"] = tod_rows
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            row = await pg_fetch_one("SELECT data_json FROM analytics_cache WHERE metric_key = 'volatility_outcome'")
            if row:
                result["volatility_outcome"] = _json.loads(row["data_json"])
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            rr_rows = await pg_fetch_all(f"""
                SELECT side, entry_price, current_price, current_sl, current_tp3, realized_pnl_pct
                FROM open_positions WHERE status='closed' AND current_sl IS NOT NULL AND current_tp3 IS NOT NULL AND entry_price > 0 {_bt_filter}""")
            rr_buckets = {}
            for r in rr_rows:
                entry, sl, tp3 = r["entry_price"], r["current_sl"], r["current_tp3"]
                risk = abs(entry - sl) if r["side"] == "long" else abs(sl - entry)
                reward = abs(tp3 - entry) if r["side"] == "long" else abs(entry - tp3)
                if risk <= 0: continue
                expected_rr = round(reward / risk, 2)
                pnl = r["realized_pnl_pct"] or 0
                realized_rr = round(abs(pnl) / 100 * entry / risk, 2)
                if pnl < 0: realized_rr = -realized_rr
                bk = "< 1:1" if expected_rr < 1 else "1:1-2:1" if expected_rr < 2 else "2:1-3:1" if expected_rr < 3 else "> 3:1"
                if bk not in rr_buckets:
                    rr_buckets[bk] = {"bucket": bk, "total": 0, "wins": 0, "pnl_sum": 0, "rr_sum": 0}
                rr_buckets[bk]["total"] += 1
                rr_buckets[bk]["pnl_sum"] += pnl
                rr_buckets[bk]["rr_sum"] += realized_rr
                if pnl > 0: rr_buckets[bk]["wins"] += 1
            order = ["< 1:1", "1:1-2:1", "2:1-3:1", "> 3:1"]
            for b in rr_buckets.values():
                b["avg_pnl"] = round(b["pnl_sum"] / max(1, b["total"]), 2)
                b["avg_realized_rr"] = round(b["rr_sum"] / max(1, b["total"]), 2)
                b["win_rate"] = round(b["wins"] / max(1, b["total"]) * 100, 1)
                del b["pnl_sum"]; del b["rr_sum"]
            result["rr_analysis"] = sorted(rr_buckets.values(), key=lambda x: order.index(x["bucket"]) if x["bucket"] in order else 99)
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            hr_rows = await pg_fetch_all("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN t.event_type = 'tp3_hit' THEN 1 ELSE 0 END) as tp3_hits,
                       SUM(CASE WHEN t.event_type IN ('tp1_hit','tp3_hit') THEN 1 ELSE 0 END) as tp1_hits,
                       SUM(CASE WHEN t.event_type = 'sl_hit' THEN 1 ELSE 0 END) as sl_hits,
                       SUM(CASE WHEN t.event_type = 'timeout' THEN 1 ELSE 0 END) as timeouts,
                       SUM(CASE WHEN t.event_type = 'full_close' THEN 1 ELSE 0 END) as manual_closes,
                       AVG(t.pnl_pct) as avg_pnl
                FROM signals s JOIN trade_outcomes t ON s.signal_hash = t.signal_hash
                WHERE t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','tp1_hit','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close')
                  AND COALESCE(s.source, '') != 'backtest_v2'""")
            row = hr_rows[0] if hr_rows else {}
            total = row.get("total") or 1
            result["midas_hit_rate"] = {
                "total": total,
                "tp3_rate": round((row.get("tp3_hits") or 0) / total * 100, 1),
                "tp1_rate": round((row.get("tp1_hits") or 0) / total * 100, 1),
                "sl_rate": round((row.get("sl_hits") or 0) / total * 100, 1),
                "timeout_rate": round((row.get("timeouts") or 0) / total * 100, 1),
                "manual_rate": round((row.get("manual_closes") or 0) / total * 100, 1),
                "avg_pnl": round(row.get("avg_pnl") or 0, 2),
            }
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            rows = await pg_fetch_all("""
                SELECT s.re_recommendation as rec, COUNT(*) as cnt, AVG(t.pnl_pct) as avg_pnl,
                       SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as wins
                FROM signals s LEFT JOIN trade_outcomes t ON s.signal_hash = t.signal_hash
                WHERE s.re_recommendation IS NOT NULL GROUP BY s.re_recommendation""")
            for r in rows:
                r["win_rate"] = round((r["wins"] or 0) / max(1, r["cnt"]) * 100, 1)
                r["avg_pnl"] = round(r["avg_pnl"] or 0, 2)
            result["rejected_signals"] = rows
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            slip_rows = await pg_fetch_all("""
                SELECT s.symbol, s.price_at_signal as signal_price, op.side, op.entry_price, op.realized_pnl_pct as pnl
                FROM signals s JOIN open_positions op ON s.signal_hash = op.signal_hash
                WHERE s.price_at_signal IS NOT NULL AND s.price_at_signal > 0
                  AND op.entry_price IS NOT NULL AND op.entry_price > 0""")
            if slip_rows:
                slippages = []
                for r in slip_rows:
                    slip_pct = (r["entry_price"] - r["signal_price"]) / r["signal_price"] * 100
                    if r["side"] == "short": slip_pct = -slip_pct
                    slippages.append(slip_pct)
                result["slippage"] = {
                    "count": len(slippages), "avg_pct": round(sum(slippages) / len(slippages), 4),
                    "max_pct": round(max(slippages), 4), "min_pct": round(min(slippages), 4),
                    "favorable": sum(1 for s in slippages if s < 0),
                    "unfavorable": sum(1 for s in slippages if s > 0),
                }
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            sym_rows = await pg_fetch_all("""
                SELECT op.symbol, COUNT(*) as cnt, AVG(op.realized_pnl_pct) as avg_pnl,
                       SUM(CASE WHEN op.realized_pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                       MIN(op.realized_pnl_pct) as worst, MAX(op.realized_pnl_pct) as best,
                       ROUND(COALESCE(SUM(op.realized_pnl_pct), 0), 2) as sum_pnl_pct
                FROM open_positions op LEFT JOIN signals s ON s.signal_hash = op.signal_hash
                WHERE op.status = 'closed' AND op.realized_pnl_pct IS NOT NULL
                  AND COALESCE(s.source, '') != 'backtest_v2'
                GROUP BY op.symbol HAVING COUNT(*) >= 1 ORDER BY sum_pnl_pct DESC""")
            for r in sym_rows:
                r["win_rate"] = round((r["wins"] or 0) / max(1, r["cnt"]) * 100, 1)
                r["avg_pnl"] = round(r["avg_pnl"] or 0, 2)
                r["worst"] = round(r["worst"] or 0, 2)
                r["best"] = round(r["best"] or 0, 2)
                r["sum_pnl_pct"] = round(r.get("sum_pnl_pct") or 0, 2)
                r["pnl_usdt"] = round(_get_bybit_pnl(r["symbol"]), 2)
            result["symbol_quality"] = sym_rows
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            streak_rows = await pg_fetch_all(f"""
                SELECT realized_pnl_pct FROM open_positions
                WHERE status = 'closed' AND realized_pnl_pct IS NOT NULL {_bt_filter}
                ORDER BY closed_at ASC""")
            pnls = [r["realized_pnl_pct"] for r in streak_rows]
            if pnls:
                streaks = []
                cur_type = "win" if pnls[0] > 0 else "loss"
                cur_len = 1
                for i in range(1, len(pnls)):
                    t = "win" if pnls[i] > 0 else "loss"
                    if t == cur_type:
                        cur_len += 1
                    else:
                        streaks.append({"type": cur_type, "length": cur_len})
                        cur_type = t
                        cur_len = 1
                streaks.append({"type": cur_type, "length": cur_len})
                max_win = max((s["length"] for s in streaks if s["type"] == "win"), default=0)
                max_loss = max((s["length"] for s in streaks if s["type"] == "loss"), default=0)
                result["streaks"] = {
                    "current_type": streaks[-1]["type"], "current_length": streaks[-1]["length"],
                    "max_win_streak": max_win, "max_loss_streak": max_loss,
                    "total_streaks": len(streaks),
                    "avg_streak": round(sum(s["length"] for s in streaks) / len(streaks), 1),
                }
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            all_trades = await pg_fetch_all(f"""
                SELECT realized_pnl_pct, side, entry_price, current_sl, current_tp3,
                       CASE WHEN side='long' AND current_sl > 0 THEN ABS(entry_price - current_sl) / entry_price * 100
                            WHEN side='short' AND current_sl > 0 THEN ABS(current_sl - entry_price) / entry_price * 100
                            ELSE NULL END as sl_distance_pct
                FROM open_positions WHERE status='closed' AND realized_pnl_pct IS NOT NULL AND entry_price > 0 {_bt_filter}""")
            if all_trades:
                wins = [t for t in all_trades if t['realized_pnl_pct'] > 0]
                losses = [t for t in all_trades if t['realized_pnl_pct'] < 0]
                total_win = sum(t['realized_pnl_pct'] for t in wins)
                total_loss = abs(sum(t['realized_pnl_pct'] for t in losses))
                net = total_win - total_loss
                avg_loss = abs(sum(t['realized_pnl_pct'] for t in losses) / max(1, len(losses)))
                avg_win = sum(t['realized_pnl_pct'] for t in wins) / max(1, len(wins))
                optimal_max_loss = avg_loss * total_win / max(0.01, total_loss) if total_loss > 0 else avg_loss
                sl_buckets = {}
                for t in losses:
                    sl_d = t.get('sl_distance_pct')
                    if sl_d is not None:
                        bk = f"{int(sl_d)}%" if sl_d < 10 else "10%+"
                        if bk not in sl_buckets:
                            sl_buckets[bk] = {'bucket': bk, 'count': 0, 'total_loss': 0}
                        sl_buckets[bk]['count'] += 1
                        sl_buckets[bk]['total_loss'] += abs(t['realized_pnl_pct'])
                simulations = []
                for cap in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]:
                    capped_losses = sum(min(abs(t['realized_pnl_pct']), cap) for t in losses)
                    sim_net = total_win - capped_losses
                    simulations.append({'sl_cap_pct': cap, 'net_pnl': round(sim_net, 2),
                                       'saved_pct': round(total_loss - capped_losses, 2), 'profitable': sim_net > 0})
                result['sl_optimization'] = {
                    'total_trades': len(all_trades), 'wins': len(wins), 'losses': len(losses),
                    'total_win_pnl': round(total_win, 2), 'total_loss_pnl': round(-total_loss, 2),
                    'net_pnl': round(net, 2), 'avg_win': round(avg_win, 2), 'avg_loss': round(-avg_loss, 2),
                    'optimal_max_loss': round(optimal_max_loss, 3),
                    'profit_factor': round(total_win / max(0.01, total_loss), 2),
                    'sl_distance_buckets': sorted(sl_buckets.values(), key=lambda x: x['bucket']),
                    'simulations': simulations,
                }
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")

        try:
            timeout_trades = await pg_fetch_all(f"""
                SELECT op.realized_pnl_pct, op.max_pnl_pct, op.entry_price,
                       op.current_sl, op.current_tp1, op.current_tp3, op.symbol,
                       ROUND(EXTRACT(EPOCH FROM (op.closed_at::timestamp - op.opened_at::timestamp))/3600, 1) as hold_hours,
                       CASE WHEN op.max_pnl_pct > 2 THEN '>2%' WHEN op.max_pnl_pct > 1 THEN '1-2%'
                            WHEN op.max_pnl_pct > 0.5 THEN '0.5-1%' WHEN op.max_pnl_pct > 0 THEN '0-0.5%'
                            ELSE '<0%' END as peak_bucket
                FROM open_positions op WHERE op.status='closed' AND op.close_reason='timeout'
                  AND op.realized_pnl_pct IS NOT NULL
                  AND op.signal_hash NOT IN (SELECT signal_hash FROM signals WHERE source='backtest_v2')""")
            if timeout_trades:
                buckets = {}
                for t in timeout_trades:
                    bk = t['peak_bucket']
                    if bk not in buckets:
                        buckets[bk] = {'bucket': bk, 'count': 0, 'avg_pnl': 0, 'avg_peak': 0, 'avg_hours': 0, 'total_missed': 0}
                    buckets[bk]['count'] += 1
                    buckets[bk]['avg_pnl'] += t['realized_pnl_pct']
                    buckets[bk]['avg_peak'] += (t['max_pnl_pct'] or 0)
                    buckets[bk]['avg_hours'] += (t['hold_hours'] or 0)
                    buckets[bk]['total_missed'] += max(0, (t['max_pnl_pct'] or 0) - t['realized_pnl_pct'])
                for bk in buckets.values():
                    n = bk['count']
                    bk['avg_pnl'] = round(bk['avg_pnl'] / n, 2)
                    bk['avg_peak'] = round(bk['avg_peak'] / n, 2)
                    bk['avg_hours'] = round(bk['avg_hours'] / n, 1)
                    bk['total_missed'] = round(bk['total_missed'], 2)
                order = ['>2%', '1-2%', '0.5-1%', '0-0.5%', '<0%']
                sorted_buckets = sorted(buckets.values(), key=lambda x: order.index(x['bucket']) if x['bucket'] in order else 99)
                sym_counts = {}
                for t in timeout_trades:
                    sym_counts[t['symbol']] = sym_counts.get(t['symbol'], 0) + 1
                top_timeout_symbols = sorted(sym_counts.items(), key=lambda x: -x[1])[:5]
                result['timeout_analytics'] = {
                    'total_timeouts': len(timeout_trades),
                    'avg_pnl': round(sum(t['realized_pnl_pct'] for t in timeout_trades) / len(timeout_trades), 2),
                    'avg_max_pnl': round(sum((t['max_pnl_pct'] or 0) for t in timeout_trades) / len(timeout_trades), 2),
                    'total_missed_pnl': round(sum(max(0, (t['max_pnl_pct'] or 0) - t['realized_pnl_pct']) for t in timeout_trades), 2),
                    'avg_hold_hours': round(sum((t['hold_hours'] or 0) for t in timeout_trades) / len(timeout_trades), 1),
                    'peak_buckets': sorted_buckets,
                    'top_symbols': [{'symbol': s, 'count': c} for s, c in top_timeout_symbols],
                }
        except Exception as e:
            logger.debug(f"Analytics enrichment skipped: {e}")
    except Exception as e:
        logger.warning(f"Analytics enrichment error: {e}")

    return result

# v0.8.1: Watchlist Scanner alerts API
@app.get("/api/watchlist-alerts")
async def watchlist_alerts_api(limit: int = 50, symbol: str = None):
    """Recent watchlist scanner alerts with post-mortem data."""
    if symbol:
        rows = await pg_fetch_all("""
            SELECT * FROM watchlist_alerts WHERE symbol = ? ORDER BY id DESC LIMIT ?
        """, (symbol, limit))
    else:
        rows = await pg_fetch_all("""
            SELECT * FROM watchlist_alerts ORDER BY id DESC LIMIT ?
        """, (limit,))

    summary_rows = await pg_fetch_all("""
        SELECT COUNT(*) as total_alerts,
            SUM(CASE WHEN alert_sent::text IN ('1', 'true', 'True') THEN 1 ELSE 0 END) as alerts_sent,
            AVG(CASE WHEN would_have_pnl_pct IS NOT NULL THEN would_have_pnl_pct::float END) as avg_would_pnl,
            SUM(CASE WHEN would_have_pnl_pct::float > 0 THEN 1 ELSE 0 END) as profitable_alerts,
            SUM(CASE WHEN would_have_pnl_pct IS NOT NULL THEN 1 ELSE 0 END) as evaluated_alerts
        FROM watchlist_alerts
    """)
    summary = summary_rows[0] if summary_rows else {}

    return {
        "alerts": rows,
        "summary": summary,
    }


# v0.10.0: Accumulation Scanner API
@app.get("/api/accumulation")
async def accumulation_data_api():
    """Get latest accumulation scan results for analytics widget."""
    from core.accumulation_scanner import get_latest_scan_data
    rows = await get_latest_scan_data()
    return {"symbols": rows, "count": len(rows)}


@app.get("/api/accumulation/scan")
async def accumulation_trigger_api():
    """Manually trigger an accumulation scan + digest (for testing)."""
    from core.accumulation_scanner import scan_all_symbols, build_and_send_digest
    results = await scan_all_symbols()
    await build_and_send_digest(results)
    return {
        "scanned": len(results),
        "top": results[:5] if results else [],
    }


# v0.9.0: Deep Capture Ratio & PnL Leak Analytics
@app.get("/api/capture-ratio")
async def capture_ratio_api():
    """Comprehensive capture ratio analytics — tracks PnL leakage."""

    # 1. Overview
    overview_rows = await pg_fetch_all("""
        SELECT COUNT(*) as total_closed,
               COUNT(CASE WHEN max_pnl_pct::float > 1.0 THEN 1 END) as peaked_1pct,
               COUNT(CASE WHEN max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0 THEN 1 END) as leaked,
               ROUND(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN realized_pnl_pct::float ELSE 0 END)::numeric / 
                     GREATEST(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN max_pnl_pct::float ELSE 0 END)::numeric, 0.01), 4) as avg_capture,
               ROUND(SUM(CASE WHEN max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0
                    THEN realized_pnl_pct::float ELSE 0 END)::numeric, 2) as total_leaked_pnl,
               ROUND(SUM(CASE WHEN max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0
                    THEN max_pnl_pct::float ELSE 0 END)::numeric, 2) as potential_saved,
               ROUND(AVG(max_pnl_pct::float)::numeric, 2) as avg_peak
        FROM open_positions WHERE status='closed' AND realized_pnl_pct IS NOT NULL
    """)
    overview = overview_rows[0] if overview_rows else {}
    overview['target_capture'] = 0.30

    # 2. By close reason
    by_reason = await pg_fetch_all("""
        SELECT close_reason as reason,
               COUNT(*) as cnt,
               ROUND(AVG(max_pnl_pct::float)::numeric, 2) as avg_peak,
               ROUND(AVG(realized_pnl_pct::float)::numeric, 2) as avg_pnl,
               ROUND(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN realized_pnl_pct::float ELSE 0 END)::numeric / 
                     GREATEST(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN max_pnl_pct::float ELSE 0 END)::numeric, 0.01), 3) as capture,
               COUNT(CASE WHEN max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0 THEN 1 END) as leaked
        FROM open_positions WHERE status='closed' AND close_reason IS NOT NULL AND realized_pnl_pct IS NOT NULL
        GROUP BY close_reason ORDER BY COUNT(*) DESC
    """)

    # 3. By zone (last zone before close)
    by_zone = await pg_fetch_all("""
        SELECT ps.zone,
               COUNT(DISTINCT ps.signal_hash) as cnt,
               ROUND(AVG(op.realized_pnl_pct::float)::numeric, 2) as avg_pnl,
               ROUND(AVG(op.max_pnl_pct::float)::numeric, 2) as avg_peak,
               ROUND(SUM(CASE WHEN op.max_pnl_pct::float > 0.5 THEN op.realized_pnl_pct::float ELSE 0 END)::numeric / 
                     GREATEST(SUM(CASE WHEN op.max_pnl_pct::float > 0.5 THEN op.max_pnl_pct::float ELSE 0 END)::numeric, 0.01), 3) as capture
        FROM position_snapshots ps
        JOIN open_positions op ON ps.signal_hash = op.signal_hash
        WHERE op.status='closed' AND op.realized_pnl_pct IS NOT NULL AND ps.zone IS NOT NULL
        GROUP BY ps.zone ORDER BY ps.zone
    """)

    # 4. Weekly trend
    trend = await pg_fetch_all("""
        SELECT TO_CHAR(closed_at::date, 'IYYY-"W"IW') as week,
               COUNT(*) as closed,
               COUNT(CASE WHEN max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0 THEN 1 END) as leaked,
               ROUND(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN realized_pnl_pct::float ELSE 0 END)::numeric / 
                     GREATEST(SUM(CASE WHEN max_pnl_pct::float > 0.5 THEN max_pnl_pct::float ELSE 0 END)::numeric, 0.01), 3) as capture,
               ROUND(AVG(realized_pnl_pct::float)::numeric, 2) as avg_pnl
        FROM open_positions WHERE status='closed' AND closed_at IS NOT NULL AND realized_pnl_pct IS NOT NULL
        GROUP BY week ORDER BY week
    """)

    # 5. Worst leakers (positions that lost the most unrealized profit)
    worst = await pg_fetch_all("""
        SELECT symbol, side,
               ROUND(max_pnl_pct::float::numeric, 2) as max_pnl,
               ROUND(realized_pnl_pct::float::numeric, 2) as realized_pnl,
               ROUND((realized_pnl_pct::float / GREATEST(max_pnl_pct::float, 0.01))::numeric, 3) as capture,
               close_reason,
               TO_CHAR(closed_at::timestamp, 'MM-DD HH24:MI') as closed_at
        FROM open_positions
        WHERE status='closed' AND max_pnl_pct::float > 1.0 AND realized_pnl_pct::float < 0
        ORDER BY (max_pnl_pct::float - realized_pnl_pct::float) DESC
        LIMIT 15
    """)

    # 6. Labeler stats
    labeler = await pg_fetch_all("""
        SELECT optimal_action as action, COUNT(*) as cnt
        FROM position_snapshots WHERE optimal_action IS NOT NULL
        GROUP BY optimal_action ORDER BY COUNT(*) DESC
    """)

    # 7. Capture distribution (histogram)
    capture_hist = await pg_fetch_all("""
        SELECT
            CASE
                WHEN cap < -0.5 THEN '< -50%'
                WHEN cap < 0 THEN '-50..0%'
                WHEN cap < 0.3 THEN '0..30%'
                WHEN cap < 0.5 THEN '30..50%'
                WHEN cap < 0.8 THEN '50..80%'
                ELSE '80%+'
            END as bucket,
            COUNT(*) as cnt,
            ROUND(AVG(realized_pnl_pct::float)::numeric, 2) as avg_pnl
        FROM (
            SELECT realized_pnl_pct, max_pnl_pct,
                   realized_pnl_pct::float / GREATEST(max_pnl_pct::float, 0.01) as cap
            FROM open_positions
            WHERE status='closed' AND max_pnl_pct::float > 0.5 AND realized_pnl_pct IS NOT NULL
        ) sub GROUP BY bucket ORDER BY MIN(cap)
    """)

    return {
        "overview": overview,
        "by_reason": by_reason,
        "by_zone": by_zone,
        "trend": trend,
        "worst_leakers": worst,
        "labeler_stats": labeler,
        "capture_distribution": capture_hist,
    }

@app.get("/api/opportunity-cost")
async def get_opportunity_cost():
    """Returns Ghost Tracker (MFE/MAE) analytics with KPI summary for the dashboard."""
    from db_adapter import pg_fetch_all, pg_fetch_one
    try:
        # KPI summary stats
        summary = await pg_fetch_one("""
            SELECT COUNT(*) as total_trades,
                   ROUND(AVG(mfe_1h)::numeric, 2) as avg_mfe_1h,
                   ROUND(AVG(mae_1h)::numeric, 2) as avg_mae_1h,
                   ROUND(AVG(mfe_4h)::numeric, 2) as avg_mfe_4h,
                   ROUND(AVG(mae_4h)::numeric, 2) as avg_mae_4h,
                   ROUND(MAX(mfe_4h)::numeric, 2) as max_mfe_4h,
                   ROUND(MAX(mae_4h)::numeric, 2) as max_mae_4h,
                   ROUND((AVG(mfe_4h) - AVG(mae_4h))::numeric, 2) as net_opportunity
            FROM trade_opportunity_cost
        """)

        # Aggregated stats by reason
        by_reason = await pg_fetch_all("""
            SELECT close_reason,
                   COUNT(*) as trades,
                   AVG(mfe_1h) as avg_mfe_1h, AVG(mae_1h) as avg_mae_1h,
                   AVG(mfe_4h) as avg_mfe_4h, AVG(mae_4h) as avg_mae_4h
            FROM trade_opportunity_cost
            GROUP BY close_reason
            ORDER BY trades DESC
        """)

        # Worst Leakers (Missed Rockets) - we closed but it pumped
        missed_rockets = await pg_fetch_all("""
            SELECT symbol, side, closed_at, close_price, close_reason, mfe_4h, mae_4h
            FROM trade_opportunity_cost
            WHERE mfe_4h > 5
            ORDER BY mfe_4h DESC
            LIMIT 5
        """)

        # Best Saves (Saved from Abyss) - we closed and it dumped hard
        saved_disasters = await pg_fetch_all("""
            SELECT symbol, side, closed_at, close_price, close_reason, mfe_4h, mae_4h
            FROM trade_opportunity_cost
            WHERE mae_4h > 5
            ORDER BY mae_4h DESC
            LIMIT 5
        """)

        return {
            "summary": dict(summary) if summary else {},
            "by_reason": by_reason,
            "missed_rockets": missed_rockets,
            "saved_disasters": saved_disasters
        }
    except Exception as e:
        logger.error(f"API /api/opportunity-cost error: {e}")
        return {"error": str(e)}

@app.get("/api/score-outcome")
async def score_outcome_api():
    """Score vs Outcome scatter data for dashboard chart."""
    points = await pg_fetch_all("""
        SELECT s.re_signal_score as score, s.re_recommendation as rec,
               t.pnl_pct as pnl, s.symbol
        FROM signals s
        JOIN trade_outcomes t ON s.signal_hash = t.signal_hash
        WHERE s.re_signal_score IS NOT NULL
          AND t.event_type IN ('full_close','sl_hit','tp3_hit','timeout','apollo_full_exit','zone_full_exit','e_pnl_full_exit','dumalka_close','manual_close','flip_close')
          AND s.signal_hash IS NOT NULL
        ORDER BY s.id DESC LIMIT 200
    """)
    return {"points": points}

def classify_close_reason(side, entry, exit_price, sl, tp1, tp3, existing_reason=None):
    """Classify how a position was closed based on exit_price vs SL/TP levels."""
    if not exit_price or not entry or exit_price <= 0:
        return existing_reason or "unknown"

    # Tolerance for price matching (0.3% of entry)
    tol = entry * 0.003

    if sl and abs(exit_price - sl) <= tol:
        return "sl_hit"
    if tp3 and abs(exit_price - tp3) <= tol:
        return "tp3_hit"
    if tp1 and abs(exit_price - tp1) <= tol:
        return "tp1_partial"

    # If existing reason is timeout, keep it
    if existing_reason == "timeout":
        return "timeout"

    # Manual close — determine if in profit or loss
    if side == "long":
        pnl = (exit_price - entry) / entry
    else:
        pnl = (entry - exit_price) / entry

    if pnl > 0.001:
        return "manual_close_profit"
    elif pnl < -0.001:
        return "manual_close_loss"
    else:
        return "manual_close_breakeven"

@app.post("/api/backfill-close-reasons")
async def backfill_close_reasons():
    """Reclassify close_reason_detailed for all closed positions (batch)."""
    results = {}

    positions = await pg_fetch_all("""
        SELECT id, side, entry_price, current_price, current_sl, current_tp1,
               current_tp3, close_reason
        FROM open_positions WHERE status='closed'
    """)

    updates = []
    for pos in positions:
        detailed = classify_close_reason(
            pos["side"], pos["entry_price"], pos["current_price"],
            pos["current_sl"], pos["current_tp1"], pos["current_tp3"],
            pos["close_reason"]
        )
        results[detailed] = results.get(detailed, 0) + 1
        updates.append((detailed, pos["id"]))

    await pg_executemany(
        "UPDATE open_positions SET close_reason_detailed = ? WHERE id = ?",
        updates
    )

    return {"updated": len(updates), "breakdown": results}

# ─── v0.18.0: Exit Quality Tracker API endpoints ─────────────────────────────

@app.get("/api/exit-quality")
async def exit_quality_api(since: str = None):
    """
    Exit quality summary — aggregated what_if_outcomes by close_reason.
    Optional ?since=2026-03-26 filter for post-Active-Mode data only.

    v0.18.0 2026-04-01: replaces old /api/what-if-analysis (read-only).
    """
    return await exit_quality.get_exit_quality_summary(since=since)


@app.post("/api/exit-quality/recalculate")
async def exit_quality_recalculate():
    """
    Manual full recalculation trigger — clears what_if_outcomes, backfill loop
    will re-analyze all eligible positions over subsequent cycles.

    v0.18.0 2026-04-01: replaces old DELETE+INSERT pattern.
    """
    return await exit_quality.recalculate_all_exits()


@app.post("/api/what-if-analysis")
async def what_if_analysis_compat():
    """
    Backward-compatible redirect to exit quality recalculation.
    v0.18.0 2026-04-01: old endpoint preserved for compatibility.
    """
    return await exit_quality.recalculate_all_exits()


@app.get("/api/exit-quality/sl-breakeven")
async def exit_quality_sl_breakeven(since: str = None):
    """
    sl_breakeven effectiveness analytics — Phase 2.
    v0.18.0 2026-04-01: initial implementation.
    """
    return await exit_quality.sl_breakeven_effectiveness(since=since)


@app.get("/api/exit-quality/missed-exits")
async def exit_quality_missed_exits(since: str = None):
    """
    Missed exit analysis — sl_hit positions with leaked profit — Phase 3.
    v0.18.0 2026-04-01: initial implementation.
    """
    return await exit_quality.missed_exit_analysis(since=since)


# ─── v0.19.0: Kline History + Scout Signal API endpoints ─────────────────────

@app.get("/api/klines/{symbol}")
async def get_klines_api(symbol: str, tf: str = "1h", limit: int = 100):
    """Retrieve stored klines for a symbol/timeframe. Read-only debugging endpoint."""
    rows = await pg_fetch_all("""
        SELECT open_time, open, high, low, close, volume, turnover
        FROM klines_history
        WHERE symbol = ? AND timeframe = ?
        ORDER BY open_time DESC
        LIMIT ?
    """, (symbol.upper(), tf, min(limit, 500)))
    total = await pg_fetch_one(
        "SELECT COUNT(*) as n FROM klines_history WHERE symbol = ? AND timeframe = ?",
        (symbol.upper(), tf)
    )
    return {
        "symbol": symbol.upper(),
        "timeframe": tf,
        "total_stored": total["n"] if total else 0,
        "returned": len(rows) if rows else 0,
        "klines": rows or [],
    }


@app.get("/api/klines-stats")
async def get_klines_stats_api():
    """Kline storage statistics — symbols, row counts, coverage."""
    stats = await pg_fetch_all("""
        SELECT symbol, timeframe, COUNT(*) as rows,
               MIN(open_time) as earliest_ms, MAX(open_time) as latest_ms
        FROM klines_history
        GROUP BY symbol, timeframe
        ORDER BY symbol, timeframe
    """)
    return {"stats": stats or [], "total_rows": sum(r["rows"] for r in stats) if stats else 0}


@app.get("/api/scout-signals")
async def get_scout_signals_api(limit: int = 50, symbol: str = None):
    """Recent scout signals with shadow PnL. Read-only."""
    if symbol:
        rows = await pg_fetch_all("""
            SELECT * FROM scout_signals WHERE symbol = ?
            ORDER BY created_at DESC LIMIT ?
        """, (symbol.upper(), min(limit, 200)))
    else:
        rows = await pg_fetch_all("""
            SELECT * FROM scout_signals ORDER BY created_at DESC LIMIT ?
        """, (min(limit, 200),))
    summary = await pg_fetch_one("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE shadow_outcome = 'tp_hit') as tp_hits,
               COUNT(*) FILTER (WHERE shadow_outcome = 'sl_hit') as sl_hits,
               COUNT(*) FILTER (WHERE shadow_outcome = 'timeout') as timeouts,
               COUNT(*) FILTER (WHERE shadow_outcome IS NULL OR shadow_outcome = 'open') as pending
        FROM scout_signals
    """)
    return {"signals": rows or [], "summary": summary or {}}


# ─── v0.19.6: ML Shadow Predictions API ──────────────────────────────────────

@app.get("/api/ml-shadow")
async def ml_shadow_api(limit: int = 50, pos_id: int = None):
    """
    ML shadow predictions — logged by ExtraTrees model during position tracking.
    Read-only. Does not affect trading decisions.

    v0.19.6 (2026-04-08): Initial implementation.
    """
    if pos_id:
        rows = await pg_fetch_all("""
            SELECT mp.*, p.symbol AS pos_symbol, p.realized_pnl_pct,
                   p.status AS pos_status, p.close_reason
            FROM ml_predictions mp
            LEFT JOIN open_positions p ON p.id = mp.pos_id
            WHERE mp.pos_id = ?
            ORDER BY mp.created_at DESC LIMIT ?
        """, (pos_id, min(limit, 200)))
    else:
        rows = await pg_fetch_all("""
            SELECT mp.*, p.symbol AS pos_symbol, p.realized_pnl_pct,
                   p.status AS pos_status, p.close_reason
            FROM ml_predictions mp
            LEFT JOIN open_positions p ON p.id = mp.pos_id
            ORDER BY mp.created_at DESC LIMIT ?
        """, (min(limit, 200),))
    summary = await pg_fetch_one("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE prediction = 'PROFIT') as profit_preds,
               COUNT(*) FILTER (WHERE prediction = 'LOSS') as loss_preds,
               ROUND(AVG(prob_profit)::numeric, 3) as avg_prob,
               model_version
        FROM ml_predictions
        GROUP BY model_version
        ORDER BY COUNT(*) DESC LIMIT 1
    """)
    import position_tracker as _pt
    model_loaded = getattr(_pt, '_ml_shadow_model', None) is not None
    return {
        "predictions": rows or [],
        "summary": summary or {},
        "model_loaded": model_loaded,
        "shadow_enabled": config.ML_SHADOW_ENABLED,
        "confidence_threshold": config.ML_SHADOW_CONFIDENCE_THRESHOLD,
    }


# ─── Webhook: TradingView / Midas ────────────────────────────────────────────

# v0.18.5: In-memory dedup guard — reject duplicate (symbol, side) webhooks within window
_recent_webhooks: dict[tuple, float] = {}
DEDUP_WINDOW_SEC = 5.0

@app.post("/tv-webhook", response_model=EnrichedRiskResult)
async def tv_webhook(payload: WebhookPayload, x_webhook_secret: str = Header(default="")):
    """
    Full pipeline: parse Midas metadata → fetch market data → signal scoring
    → Monte Carlo VaR/CVaR → log everything → return enriched result.

    v0.17.0 2026-03-31: Same-coin re-entry protocol. If a new same-side signal arrives
        for a position older than REENTRY_COOLDOWN_HOURS, it passes to scoring. If
        approved/reduced, old position is closed via Dumalka full_close before approval.
        Also fixes reversal (v0.14.5) to send Dumalka full_close (was DB-only).
    v0.16.0 2026-03-31: guard INSERT uses improved price fallback chain
        (entry_low → entry_high → stop_loss → 0.0) for shadow backfill eligibility.
    v0.14.5: Graceful Reversal Protocol for opposing signals on trades older than 4h.
    """
    if config.WEBHOOK_SECRET and x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Webhook Secret")

    # v0.14.2: Structured trace context for this request
    trace_id = payload.signal_hash or f"{payload.symbol}_{int(time.time())}"
    _log = logging.LoggerAdapter(logger, {
        "trace_id": trace_id,
        "symbol": payload.symbol,
    })
    _log.info(f"Received webhook: {payload.side} size={payload.size} "
              f"rr={payload.risk_reward} prob={payload.probability} wr={payload.win_rate} "
              f"trend={payload.trend}")

    # v0.18.5: Dedup guard — reject same (symbol, side) within DEDUP_WINDOW_SEC
    # v0.18.6: Skip dedup for approval_flow — bridge must always get a real score,
    #          otherwise it sends {reject, score=0} to the bot (critical bug).
    # v0.19.8: Skip dedup for bot_direct — same reasoning as approval_flow.
    _dedup_key = (payload.symbol, payload.side)
    _dedup_now = time.time()
    _skip_dedup = getattr(payload, "source", None) in ("approval_flow", "bot_direct")
    if not _skip_dedup and _dedup_key in _recent_webhooks and (_dedup_now - _recent_webhooks[_dedup_key]) < DEDUP_WINDOW_SEC:
        _log.info(f"Dedup reject: {payload.symbol} {payload.side} within {DEDUP_WINDOW_SEC}s window")
        return EnrichedRiskResult(
            approved=False, var=0, cvar=0, liquidation_prob=0,
            drawdown_estimate=0, signal_hash=trace_id,
            recommendation="reject", rejection_reason="duplicate_webhook_dedup",
        )
    _recent_webhooks[_dedup_key] = _dedup_now

    try:
        if payload.size <= 0:
            raise ValueError("Size must be positive")

        # v0.14.5: Graceful Reversal Protocol & Anti-Whipsaw Guard
        from db_adapter import pg_fetch_one, pg_execute
        from datetime import datetime, timezone
        
        active_pos = await pg_fetch_one(
            "SELECT id, side, opened_at, signal_hash FROM open_positions WHERE symbol=? AND status='open' LIMIT 1",
            (payload.symbol,)
        )
        
        reversal_candidate_id = None
        reentry_candidate_id = None
        reentry_old_hash = None
        
        if active_pos:
            opened_at = active_pos["opened_at"]
            if isinstance(opened_at, str):
                opened_at = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0

            if active_pos["side"] == payload.side:
                # v0.17.0: Same-side re-entry protocol
                if not config.REENTRY_ENABLED:
                    rejection_reason = "already_in_position"
                    logger.warning(f"🚫 [STATE GUARD] REJECTED {payload.symbol}: Already have an active {payload.side} trade (re-entry disabled).")
                elif age_hours < config.REENTRY_COOLDOWN_HOURS:
                    rejection_reason = "reentry_cooldown"
                    logger.warning(f"🚫 [REENTRY COOLDOWN] REJECTED {payload.symbol}: Active {payload.side} trade is only {age_hours:.1f}h old (need {config.REENTRY_COOLDOWN_HOURS}h).")
                else:
                    reentry_candidate_id = active_pos["id"]
                    reentry_old_hash = active_pos.get("signal_hash")
                    rejection_reason = None
                    logger.info(f"🔄 [REENTRY ALLOWED] {payload.symbol} {payload.side} new signal vs existing {payload.side} trade ({age_hours:.1f}h old). Passing to scoring...")
            else:
                # Opposite direction — existing Reversal Protocol (v0.14.5)
                if age_hours < 4.0:
                    rejection_reason = "whipsaw_protection"
                    logger.warning(f"🚫 [WHIPSAW PROTECT] REJECTED reversal for {payload.symbol}: Active trade is only {age_hours:.1f}h old.")
                else:
                    reversal_candidate_id = active_pos["id"]
                    logger.info(f"🔄 [REVERSAL ALLOWED] {payload.symbol} {payload.side} signal against {active_pos['side']} trade ({age_hours:.1f}h old). Passing to scoring...")
                    rejection_reason = None
            
            if rejection_reason:
                # Persist the rejected signal
                # v0.16.0 2026-03-31: fixed price fallback chain (was: entry_low or 0.0,
                #   caused price_at_signal=0 for guard rejects, blocking shadow backfill)
                try:
                    await pg_execute("""
                        INSERT INTO signals
                        (symbol, side, price_at_signal, tp1, stop_loss, risk_reward,
                         re_signal_score, re_recommendation, midas_comment, created_at, signal_hash, source, payload_raw)
                         VALUES (?, ?, ?, ?, ?, ?, 0.0, 'reject', ?, now(), ?, ?, ?)
                         ON CONFLICT (signal_hash) WHERE signal_hash IS NOT NULL DO NOTHING
                    """, (payload.symbol, payload.side, payload.entry_low or payload.entry_high or payload.stop_loss or 0.0,
                         payload.tp1, payload.stop_loss,
                         payload.risk_reward, f"GUARD: {rejection_reason.upper()}", payload.signal_hash or trace_id, payload.source, payload.model_dump_json()))
                except Exception as e:
                    logger.error(f"Failed to append rejected guard signal to DB: {e}")

                return EnrichedRiskResult(
                    approved=False, var=0, cvar=0, liquidation_prob=0,
                    drawdown_estimate=0, latency_ms=0, signal_hash=trace_id,
                    signal_score=0, is_countertrend=False, recommendation="reject",
                    midas_probability=payload.probability, midas_win_rate=payload.win_rate,
                    midas_trend=payload.trend, setup_master_text=payload.setup_master_text,
                    re_annual_vol=0, trend_strength="",
                    risk_reward=payload.risk_reward, volume_level="",
                    rejection_reason=rejection_reason
                )

        # ── Phase 1 Filters — SHADOW MODE (analytics only, no intervention) ──
        phase1_flag = None
        if config.PHASE1_SHADOW_MODE or config.PHASE1_ACTIVE_MODE:
            from datetime import datetime, timezone
            # 1.1 Symbol Blacklist (win rate < 15% from analytics)
            if payload.symbol in config.SYMBOL_BLACKLIST:
                phase1_flag = f"symbol_blacklist:{payload.symbol}"
                logger.info(f"📊 Phase 1 SHADOW: {payload.symbol} would be rejected (blacklist)")

            # 1.2 Unprofitable hours UTC (avg PnL < -1% from time_of_day)
            current_hour_utc = datetime.now(timezone.utc).hour
            if current_hour_utc in config.UNPROFITABLE_HOURS_UTC and not phase1_flag:
                phase1_flag = f"unprofitable_hour:{current_hour_utc}UTC"
                logger.info(f"📊 Phase 1 SHADOW: hour {current_hour_utc} UTC would be rejected")

        # ACTIVE MODE: actually reject (disabled by default, enable when ready)
        if phase1_flag and config.PHASE1_ACTIVE_MODE:
            enriched = EnrichedRiskResult(
                approved=False,
                var=0, cvar=0, liquidation_prob=0, drawdown_estimate=0,
                latency_ms=0,
                signal_hash=payload.signal_hash or None,
                signal_score=0, is_countertrend=False,
                recommendation="reject",
                midas_probability=payload.probability,
                midas_win_rate=payload.win_rate,
                midas_risk_reward=payload.risk_reward,
                computed_volatility=0, score_components={},
                kelly_suggested_size_usd=0, exposure_warning=False,
            )
            asyncio.create_task(insert_signal(
                source=payload.source, symbol=payload.symbol, side=payload.side,
                size=payload.size, price_at_signal=0, volatility_used=0,
                payload_raw=payload.model_dump(mode='json'),
                risk_request={}, risk_result=enriched.model_dump(mode='json'),
                latency_ms=0, stop_loss=payload.stop_loss,
                tp1=payload.tp1, tp3=payload.tp3,
                risk_reward=payload.risk_reward,
                midas_probability=payload.probability, midas_win_rate=payload.win_rate,
                midas_trend=payload.trend, trend_strength=payload.trend_strength,
                volume_level=payload.volume_level,
                is_countertrend=False, midas_comment=f"phase1_reject:{phase1_flag}",
                re_annual_vol=0, re_signal_score=0, re_recommendation="reject",
                signal_hash=payload.signal_hash, score_components={},
                setup_master_text=payload.setup_master_text,
            ))
            logger.warning(f"🚫 Phase 1 ACTIVE reject: {payload.symbol} ({phase1_flag})")
            return enriched
        # SHADOW MODE (default): log and continue normal processing

        t0 = time.perf_counter()
        t_phase = time.perf_counter()

        # 1. Fetch real market data (price + volatility)
        price, vol = await fetch_market_data(payload.symbol)
        market_fetch_ms = round((time.perf_counter() - t_phase) * 1000, 1)
        _log.info(f"Market data fetched: price={price} vol={vol:.4f}", extra={"duration_ms": market_fetch_ms})
        
        # Fallback to provided entry prices if fetch failed (common for symbols not on Binance US)
        if price <= 0:
            if payload.entry_low:
                price = payload.entry_low
                logger.info(f"Using entry_low as fallback price for {payload.symbol}: {price}")
            elif payload.entry_high:
                price = payload.entry_high
                logger.info(f"Using entry_high as fallback price for {payload.symbol}: {price}")
            else:
                raise ValueError(f"Failed to fetch valid price for {payload.symbol} and no entry_low/high provided.")

        # 1.5 v0.6.0: Fetch orderbook depth + multi-timeframe trends (parallel)
        trade_size_usd = payload.size * price if price > 0 else 100.0
        spread_pct, slippage_pct = await fetch_orderbook_depth(payload.symbol, trade_size_usd)
        multi_tf_trends = await fetch_multi_timeframe(payload.symbol)

        # 1.6 v0.7.0: Fetch funding rate + OI
        funding_rate, oi_change_pct = await fetch_funding_and_oi(payload.symbol)

        # 1.7 v0.7.0: Update regime detector (uses cached BTC data, updates every 15 min)
        regime_adj = None
        if config.REGIME_DETECTOR_ENABLED:
            try:
                regime_adj = get_scoring_adjustments()
            except Exception as e:
                logger.warning(f"Regime detector error: {e}")

        # Calculate SL distance as % for slippage comparison
        stop_loss_pct = None
        if payload.stop_loss and price > 0:
            stop_loss_pct = abs(price - payload.stop_loss) / price * 100

        # 2. Signal quality scoring (our assessment vs Midas)
        t_phase = time.perf_counter()
        score_result = compute_signal_score(
            side=payload.side,
            risk_reward=payload.risk_reward,
            probability=payload.probability,
            win_rate=payload.win_rate,
            trend=payload.trend,
            trend_strength=payload.trend_strength,
            volume_level=payload.volume_level,
            market_vol=vol,
            spread_pct=spread_pct,
            slippage_pct=slippage_pct,
            stop_loss_pct=stop_loss_pct,
            multi_tf_trends=multi_tf_trends,
            funding_rate=funding_rate,
            oi_change_pct=oi_change_pct,
            regime_adjustments=regime_adj,
        )

        # ── v0.14.2: Repeat Signal Persistence Boost ──
        # Data insight (408 trades): repeat signals <6h → WR 52% vs fresh 41%
        # Signal persistence = strong trend confirmation. Add +0.03 score boost.
        repeat_boost = 0.0
        hours_since_last = None
        try:
            last_same = await pg_fetch_one(
                "SELECT created_at FROM signals "
                "WHERE symbol = ? AND side = ? AND id != (SELECT MAX(id) FROM signals) "
                "ORDER BY created_at DESC LIMIT 1",
                (payload.symbol, payload.side)
            )
            if last_same and last_same.get("created_at"):
                from datetime import datetime, timezone
                last_ts = last_same["created_at"]
                if hasattr(last_ts, 'timestamp'):
                    delta_h = (datetime.now(timezone.utc) - last_ts.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                else:
                    delta_h = 999
                hours_since_last = round(delta_h, 1)
                if delta_h < 6.0:
                    repeat_boost = 0.03
                    score_result["score"] = round(score_result["score"] + repeat_boost, 4)
                    score_result["components"]["repeat_boost"] = repeat_boost
                    logger.info(
                        f"🔄 [REPEAT SIGNAL] {payload.symbol} {payload.side}: "
                        f"last same signal {delta_h:.1f}h ago → +0.03 boost "
                        f"(new score={score_result['score']:.4f})"
                    )
                    # Re-check recommendation after boost
                    if score_result["score"] >= 0.60 and score_result["recommendation"] == "reduce":
                        score_result["recommendation"] = "approve"
                        logger.info(
                            f"🔄 [REPEAT UPGRADE] {payload.symbol}: "
                            f"score {score_result['score']:.4f} → upgraded reduce→approve"
                        )
        except Exception as e:
            logger.debug(f"Repeat signal lookup failed: {e}")

        # ── v0.18.5: Support/Resistance keyword boost ──
        # Midas signals mentioning key S/R levels have historically higher win rate.
        keyword_boost = 0.0
        _comment_text = (getattr(payload, "midas_comment", None) or "").lower()
        if _comment_text:
            _SR_KEYWORDS = [
                "поддержк", "сопротивлен", "ключев", "уровен",
                "support", "resistance", "key level",
                "strong buy", "strong sell",
                "зона спроса", "зона предложения",
                "пробой", "отскок от уровня", "тест уровня",
            ]
            _sr_matches = [kw for kw in _SR_KEYWORDS if kw in _comment_text]
            if _sr_matches:
                keyword_boost = 0.03 * min(len(_sr_matches), 3)
                logger.info(
                    f"🎯 [SR_BOOST] {payload.symbol}: +{keyword_boost:.2f} "
                    f"for keywords: {_sr_matches}"
                )
            if "strong" in _comment_text[:50]:
                keyword_boost = max(keyword_boost, 0.05)
                logger.info(
                    f"💪 [STRONG_SIGNAL] {payload.symbol}: boost={keyword_boost:.2f}"
                )
            if keyword_boost > 0:
                score_result["score"] = round(
                    min(score_result["score"] + keyword_boost, 1.0), 4
                )
                score_result["components"]["keyword_boost"] = keyword_boost
                if score_result["score"] >= 0.60 and score_result["recommendation"] == "reduce":
                    score_result["recommendation"] = "approve"
                    logger.info(
                        f"🎯 [SR_UPGRADE] {payload.symbol}: "
                        f"score {score_result['score']:.4f} → upgraded reduce→approve"
                    )

        # 3. Build RiskRequest
        from models import Position
        equity = payload.current_equity if payload.current_equity is not None else config.DEFAULT_EQUITY
        positions_list = [Position(**p) for p in payload.open_positions] if payload.open_positions else []
        portfolio = Portfolio(equity=equity, positions=positions_list)
        candidate = CandidateTrade(symbol=payload.symbol, side=payload.side, size=payload.size)
        market = MarketData(
            prices={payload.symbol: price},
            volatility={payload.symbol: vol},
        )
        limits = RiskLimits(max_var=0.05, max_cvar=0.07, max_liquidation_prob=0.01)

        request = RiskRequest(
            portfolio=portfolio,
            candidate_trade=candidate,
            market=market,
            risk_limits=limits,
            n_scenarios=config.SCENARIOS,
        )
        
        # 3.5 Max Exposure Check (per-symbol)
        current_exposure_usd = sum(p.size * p.entry_price for p in portfolio.positions if p.symbol == payload.symbol)
        new_exposure_usd = current_exposure_usd + (payload.size * price)
        
        exposure_warning = False
        limit_usd = portfolio.equity * config.MAX_EXPOSURE_PER_SYMBOL
        if new_exposure_usd > limit_usd:
            score_result["recommendation"] = "reject"
            exposure_warning = True
            logger.warning(f"Exposure limit exceeded for {payload.symbol}: {new_exposure_usd:.2f} > {limit_usd:.2f} (Limit={config.MAX_EXPOSURE_PER_SYMBOL*100}%)")

        # 3.6 v0.6.0: Portfolio Allocator (sector concentration check)
        alloc_result = check_portfolio_limits(
            candidate_symbol=payload.symbol,
            candidate_side=payload.side,
            candidate_size=payload.size,
            candidate_price=price,
            equity=equity,
            existing_positions=payload.open_positions,
        )
        if not alloc_result["allowed"]:
            if score_result["recommendation"] != "reject":
                score_result["recommendation"] = "reject"
                logger.warning(
                    f"Portfolio allocator blocked: {alloc_result['reason']} "
                    f"(sector={alloc_result['sector']} exposure={alloc_result['sector_exposure_pct']:.1f}%)"
                )
        elif alloc_result["suggested_size_mult"] < 1.0:
            if score_result["recommendation"] == "approve":
                score_result["recommendation"] = "reduce"
                logger.info(
                    f"Portfolio allocator suggests reduce: mult={alloc_result['suggested_size_mult']:.2f} "
                    f"(sector={alloc_result['sector']} exposure={alloc_result['sector_exposure_pct']:.1f}%)"
                )

        scoring_ms = round((time.perf_counter() - t_phase) * 1000, 1)
        _log.info(f"Scoring done: score={score_result['score']:.4f} rec={score_result['recommendation']}",
                  extra={"duration_ms": scoring_ms})

        # 4. Monte Carlo VaR/CVaR on Titan V (run in thread to not block event loop)
        t_phase = time.perf_counter()
        gpu_before = get_gpu_util_now()
        mc_result = await asyncio.to_thread(
            run_monte_carlo_risk,
            portfolio=request.portfolio,
            candidate=request.candidate_trade,
            market=request.market,
            limits=request.risk_limits,
            n_scenarios=request.n_scenarios,
        )
        gpu_after = get_gpu_util_now()
        record_gpu_peak(max(gpu_before, gpu_after, 95))  # MC uses 95%+ GPU
        mc_ms = round((time.perf_counter() - t_phase) * 1000, 1)
        latency_ms = (time.perf_counter() - t0) * 1000
        _log.info(f"MC done: VaR={mc_result.var:.4f} CVaR={mc_result.cvar:.4f} approved={mc_result.approved}",
                  extra={"duration_ms": mc_ms})

        # 5. Combine scoring + MC into enriched result
        # Scoring recommendation + MC risk → final decision
        # v0.19.8.1: "reduce" now returns approved=True so the bot opens with
        # reduced conviction_size_usd (0.7x). Shadow data shows reduce signals
        # average +1.8% PnL (N=155), better than approve (+0.3%).
        recommendation = score_result["recommendation"]
        combined_approved = (recommendation in ("approve", "reduce")) and mc_result.approved

        # VaR-based auto-reduce with category normalization
        # Large-cap (BTC, ETH) have structurally lower VaR → use category baselines
        var_pct = mc_result.var
        if price > 0:
            if price > 1000:    # large-cap: BTC, ETH
                var_baseline = 0.005
            elif price > 10:    # mid-cap: SOL, AVAX, etc.
                var_baseline = 0.015
            else:               # small-cap / meme
                var_baseline = 0.05
            normalized_var = var_pct / var_baseline if var_baseline > 0 else 1.0
        else:
            normalized_var = var_pct / 0.015  # fallback

        if normalized_var > 3.0 and recommendation == "approve":
            recommendation = "reduce"
            logger.info(f"VaR override: {var_pct*100:.2f}% (normalized={normalized_var:.1f}x baseline), downgrading to reduce (approved, 0.7x size)")

        # 4.5 v0.10.1: Conviction Sizing — Score-based dynamic lot sizing
        # Modulates position size based on signal quality instead of flat sizing.
        # High-conviction signals get larger allocation; weak signals get reduced.
        base_size_usd = payload.size * price if price > 0 else 100.0
        signal_score_val = score_result["score"]
        if signal_score_val >= 0.75:
            conviction_mult = 1.5
        elif signal_score_val >= 0.60:
            conviction_mult = 1.0
        elif signal_score_val >= 0.45:
            conviction_mult = 0.7  # v0.18.5: raised from 0.5 to keep margin above Bybit $5.5 min
        else:
            conviction_mult = 0.0
        conviction_size_usd = round(base_size_usd * conviction_mult, 2)
        logger.info(
            f"[CONVICTION] score={signal_score_val:.3f} mult={conviction_mult}x "
            f"base=${base_size_usd:.0f} → conviction=${conviction_size_usd:.0f}"
        )

        # v0.18.5: Auto SL→BE parameters for bot-side breakeven protection.
        # trigger = TP1 price (when price reaches TP1, bot moves SL to BE)
        # be_price = entry + 0.2% offset (ensures small profit if stopped out at BE)
        _auto_be_price = None
        _auto_be_trigger = None
        if recommendation != "reject" and payload.tp1 and price > 0:
            _auto_be_trigger = round(payload.tp1, 6)
            _atr_offset = price * 0.002
            if payload.side == "long":
                _auto_be_price = round(price + _atr_offset, 6)
            else:
                _auto_be_price = round(price - _atr_offset, 6)

        enriched = EnrichedRiskResult(
            approved=combined_approved,
            var=mc_result.var,
            cvar=mc_result.cvar,
            liquidation_prob=mc_result.liquidation_prob,
            drawdown_estimate=mc_result.drawdown_estimate,
            latency_ms=latency_ms,
            signal_hash=payload.signal_hash or None,
            signal_score=score_result["score"],
            is_countertrend=score_result["is_countertrend"],
            recommendation=recommendation,
            midas_probability=payload.probability,
            midas_win_rate=payload.win_rate,
            midas_risk_reward=payload.risk_reward,
            computed_volatility=vol,
            score_components=score_result["components"],
            kelly_suggested_size_usd=portfolio.equity * score_result.get("kelly_f", 0.0),
            conviction_size_usd=conviction_size_usd,
            auto_be_price=_auto_be_price,
            auto_be_trigger=_auto_be_trigger,
            exposure_warning=exposure_warning,
        )

        # v0.17.0: enrich midas_comment with re-entry / reversal context
        _midas_comment = payload.midas_comment or ""
        if reentry_candidate_id:
            _midas_comment = f"RE_ENTRY: old #{reentry_candidate_id} (hash={reentry_old_hash}); {_midas_comment}".strip("; ")
        elif reversal_candidate_id:
            _midas_comment = f"REVERSAL: old #{reversal_candidate_id}; {_midas_comment}".strip("; ")

        # 5.5 v0.18.4: Portfolio Stress Warning — alert when concentration risk is high.
        # NOM #548 post-mortem: entered LONG extreme-vol while KERNEL LONG was at -11%.
        # Informational only (no blocking); operator/Kati gets TG alert for awareness.
        try:
            _stress_rows = await pg_fetch_all(
                "SELECT symbol, side, current_pnl_pct FROM open_positions WHERE status = 'open'"
            )
            if _stress_rows:
                _candidate_side = payload.side.lower()
                _underwater_same_dir = [
                    r for r in _stress_rows
                    if (r.get("side") or "").lower() == _candidate_side
                    and float(r.get("current_pnl_pct") or 0) < -5.0
                ]
                if _underwater_same_dir:
                    _uw_str = ", ".join(
                        f"{r['symbol']} ({float(r['current_pnl_pct']):+.1f}%)"
                        for r in _underwater_same_dir
                    )
                    _stress_msg = (
                        f"⚠️ *PORTFOLIO STRESS*\n"
                        f"Opening {payload.symbol} {_candidate_side.upper()} "
                        f"while {len(_underwater_same_dir)} same-direction position(s) "
                        f"underwater: {_uw_str}"
                    )
                    logger.warning(
                        f"[PORTFOLIO STRESS] {payload.symbol} {_candidate_side}: "
                        f"existing underwater same-dir: {_uw_str}"
                    )
                    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
                        async def _send_stress_tg():
                            await send_telegram_message(_stress_msg, parse_mode="Markdown")

                        asyncio.create_task(_send_stress_tg())
        except Exception as _se:
            logger.debug(f"Portfolio stress check skipped: {_se}")

        # 6. Log to DB asynchronously
        asyncio.create_task(insert_signal(
            source=payload.source,
            symbol=payload.symbol,
            side=payload.side,
            size=payload.size,
            price_at_signal=price,
            volatility_used=vol,
            payload_raw=payload.model_dump(mode='json'),
            risk_request=request.model_dump(mode='json'),
            risk_result=enriched.model_dump(mode='json'),
            latency_ms=latency_ms,
            stop_loss=payload.stop_loss,
            tp1=payload.tp1,
            tp3=payload.tp3,
            risk_reward=payload.risk_reward,
            midas_probability=payload.probability,
            midas_win_rate=payload.win_rate,
            midas_trend=payload.trend,
            trend_strength=payload.trend_strength,
            volume_level=payload.volume_level,
            is_countertrend=score_result["is_countertrend"],
            midas_comment=_midas_comment,
            re_annual_vol=vol,
            re_signal_score=score_result["score"],
            re_recommendation=recommendation,
            signal_hash=payload.signal_hash,
            score_components=score_result["components"],
            setup_master_text=payload.setup_master_text,
            # v0.14.2: ML feature columns (previously fetched but not persisted)
            funding_rate=funding_rate,
            oi_change_pct=oi_change_pct,
            market_regime=regime_adj.get("regime") if regime_adj else None,
            spread_pct=spread_pct,
            slippage_pct=slippage_pct,
            multi_tf_trends_json=multi_tf_trends,
        ))

        # v0.14.5 / v0.17.0: Execute Reversal or Re-entry close via Dumalka
        _close_candidate_id = reversal_candidate_id or reentry_candidate_id
        _close_reason_tag = "trend_reversal" if reversal_candidate_id else "re_entry"
        _close_min_rec = ("approve",) if reversal_candidate_id else ("approve", "reduce")

        if _close_candidate_id and recommendation in _close_min_rec:
            from position_tracker import send_command_to_bot
            try:
                close_result = await send_command_to_bot(
                    "full_close", payload.symbol,
                    trace_id=payload.signal_hash or trace_id,
                    reason=f"{_close_reason_tag}_new_signal",
                    _pos_id=_close_candidate_id,
                )
                if close_result.get("ok"):
                    _old_pos = await pg_fetch_one(
                        "SELECT realized_pnl_pct, current_pnl_pct, max_pnl_pct, "
                        "drawdown_from_peak_pct, tp_progress_pct, current_price "
                        "FROM open_positions WHERE id = ?",
                        (_close_candidate_id,)
                    )
                    _pnl = (_old_pos["current_pnl_pct"] if _old_pos else None)
                    await pg_execute(
                        "UPDATE open_positions SET status='closed', close_reason=?, "
                        "closed_at=now(), realized_pnl_pct=?, current_pnl_pct=? WHERE id=?",
                        (_close_reason_tag, _pnl, _pnl, _close_candidate_id)
                    )
                    logger.info(
                        f"🔄 [{_close_reason_tag.upper()}] Closed old position #{_close_candidate_id} "
                        f"for {payload.symbol}, new signal scored {score_result['score']:.3f}"
                    )
                    try:
                        await pg_execute(
                            """INSERT INTO dumalka_audit_log
                               (timestamp, trace_id, symbol, action, pos_id, details)
                               VALUES (?, ?, ?, 'position_closed', ?, ?)""",
                            (
                                datetime.now(timezone.utc).isoformat(),
                                payload.signal_hash or trace_id,
                                payload.symbol,
                                _close_candidate_id,
                                json.dumps({
                                    "close_reason": _close_reason_tag,
                                    "detail": f"{_close_reason_tag}_new_signal",
                                    "source": "webhook_reversal",
                                    "pnl_pct": round(_pnl, 4) if _pnl is not None else None,
                                    "price": _old_pos["current_price"] if _old_pos else None,
                                    "max_pnl_pct": round(_old_pos["max_pnl_pct"], 4) if _old_pos and _old_pos["max_pnl_pct"] else None,
                                    "drawdown_pct": round(_old_pos["drawdown_from_peak_pct"], 4) if _old_pos and _old_pos["drawdown_from_peak_pct"] else None,
                                    "tp_progress_pct": round(_old_pos["tp_progress_pct"], 4) if _old_pos and _old_pos["tp_progress_pct"] else None,
                                    "new_signal_score": round(score_result["score"], 4),
                                }),
                            ),
                        )
                    except Exception as audit_err:
                        logger.warning(f"Reversal/re-entry audit log write failed: {audit_err}")
                else:
                    logger.warning(
                        f"🛑 [{_close_reason_tag.upper()} ABORTED] full_close failed for "
                        f"#{_close_candidate_id}: {close_result}"
                    )
                    recommendation = "reject"
                    enriched.recommendation = "reject"
                    enriched.approved = False
            except Exception as e:
                logger.error(f"Failed to execute {_close_reason_tag} close for #{_close_candidate_id}: {e}")
                recommendation = "reject"
                enriched.recommendation = "reject"
                enriched.approved = False

        # 6.5 Phase 3.7 Shadow Mode: Register open position
        # Dedup guard: only register if signal_hash not already in open_positions
        # v0.17.0: skip registration if recommendation was downgraded to reject (e.g. re-entry close failed)
        _final_rec = recommendation
        async def _safe_register():
            """
            Registers the position asynchronously inside the event loop,
            attempting immediately and wrapping in robust try-except.
            """
            if _final_rec == "reject":
                return
            if payload.signal_hash:
                existing = await pg_fetch_one(
                    "SELECT id FROM open_positions WHERE signal_hash = ? AND status = 'open' LIMIT 1",
                    (payload.signal_hash,)
                )
                if existing:
                    return  # Already registered
            await register_open_position(
                signal_id=0,
                symbol=payload.symbol,
                side=payload.side,
                size=payload.size,
                entry_price=price,
                sl=payload.stop_loss,
                tp1=payload.tp1,
                tp2=payload.tp2,
                tp3=payload.tp3,
                score=score_result["score"],
                rec=_final_rec,
                signal_hash=payload.signal_hash,
            )
        asyncio.create_task(_safe_register())

        logger.info(
            f"Webhook processed | {payload.symbol} {payload.side} {payload.size} | "
            f"price={price} vol={vol:.4f} score={score_result['score']:.3f} "
            f"rec={recommendation} MC_var={mc_result.var:.4f} latency={latency_ms:.1f}ms"
        )

        # v0.19.8: TG observability for direct bot API calls
        if payload.source in ("bot_direct", "approval_flow"):
            asyncio.create_task(send_signal_report(enriched, payload.symbol, payload.side))

        return enriched

    except Exception as e:
        logger.error(f"TV webhook handling failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

# ─── Batch Import ─────────────────────────────────────────────────────────────

@app.post("/import-signals")
async def import_signals(signals: List[WebhookPayload]):
    """
    Batch import historical signals. Each signal goes through the full
    pipeline: market data fetch → scoring → Monte Carlo → DB.
    """
    results = []
    errors = []
    for i, sig in enumerate(signals):
        try:
            # Reuse the webhook pipeline
            enriched = await tv_webhook(sig, x_webhook_secret=config.WEBHOOK_SECRET)
            results.append({
                "index": i,
                "symbol": sig.symbol,
                "side": sig.side,
                "score": enriched.signal_score,
                "recommendation": enriched.recommendation,
                "var": enriched.var,
                "cvar": enriched.cvar,
                "approved": enriched.approved,
            })
        except Exception as e:
            errors.append({"index": i, "symbol": sig.symbol, "error": str(e)})
            logger.warning(f"Import failed for signal {i} ({sig.symbol}): {e}")

    return {
        "imported": len(results),
        "errors": len(errors),
        "results": results,
        "error_details": errors,
    }

# ─── Analysis ────────────────────────────────────────────────────────────────

@app.get("/analysis")
async def analysis():
    """
    Compare Risk Engine assessments vs Midas metadata.
    Returns aggregate stats from the signals database.
    """
    stats = await get_analysis_stats()
    return stats

# ─── Trade Outcomes ───────────────────────────────────────────────────────────

@app.post("/trade-outcome")
async def record_trade_outcome(
    payload: TradeOutcomePayload,
    x_webhook_secret: str = Header(default=""),
):
    """
    Record a trade outcome event (SL hit, TP hit, close, etc.).
    Can be called by the trading bot directly, or parsed from TG messages.
    Hash is optional — events without hash are still recorded for raw stats.

    v0.19.6.1 (2026-04-10): Added close-event handler — updates open_positions
    for sl_hit, tp3_hit, full_close, timeout, apollo/zone/e_pnl_full_exit,
    dumalka_close, manual_close, flip_close. Previously only position_increased
    was synced, leaving close events to be caught by position_tracker desync.
    """
    if config.WEBHOOK_SECRET and x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Webhook Secret")

    # If hash provided, try to enrich with original signal data
    re_recommendation = None
    re_signal_score = None
    re_var = None
    if payload.hash:
        try:
            row = await pg_fetch_one(
                "SELECT re_recommendation, re_signal_score, var FROM signals WHERE signal_hash = ? ORDER BY id DESC LIMIT 1",
                (payload.hash,)
            )
            if row:
                re_recommendation = row["re_recommendation"]
                re_signal_score = row["re_signal_score"]
                re_var = row["var"]
        except Exception as e:
            logger.warning(f"Could not enrich outcome with signal data: {e}")

    await insert_trade_outcome(
        signal_hash=payload.hash,
        event_type=payload.event,
        symbol=payload.symbol,
        side=payload.side,
        price_at_event=payload.price,
        pnl_pct=payload.pnl_pct,
        size_remaining=payload.size_remaining,
        metadata={"comment": payload.comment} if payload.comment else None,
        re_recommendation=re_recommendation,
        re_signal_score=re_signal_score,
        re_var=re_var,
    )

    # v0.19.8: Sync open_positions entry price on "open" event (bot confirms actual fill).
    if payload.event == "open" and payload.hash:
        try:
            existing = await pg_fetch_one(
                "SELECT id FROM open_positions WHERE signal_hash = ? AND status = 'open' LIMIT 1",
                (payload.hash,)
            )
            if existing:
                updates = []
                params = []
                if payload.price is not None:
                    updates.append("entry_price = ?")
                    updates.append("current_price = ?")
                    params.extend([payload.price, payload.price])
                if payload.size_remaining is not None:
                    updates.append("size = ?")
                    updates.append("original_size = ?")
                    params.extend([payload.size_remaining, payload.size_remaining])
                if updates:
                    params.append(payload.hash)
                    await pg_execute(
                        f"UPDATE open_positions SET {', '.join(updates)} WHERE signal_hash = ? AND status = 'open'",
                        tuple(params),
                    )
                    logger.info(f"trade-outcome open: updated entry for {payload.symbol} hash={payload.hash}")
            else:
                await pg_execute("""
                    INSERT INTO open_positions (
                        signal_hash, opened_at, symbol, side,
                        size, original_size, entry_price,
                        current_price, status,
                        initial_signal_score, initial_recommendation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """, (
                    payload.hash,
                    datetime.now(timezone.utc).isoformat(),
                    payload.symbol, payload.side or "unknown",
                    payload.size_remaining or 0, payload.size_remaining or 0,
                    payload.price or 0, payload.price or 0,
                    re_signal_score, re_recommendation,
                ))
                logger.info(f"trade-outcome open: registered new position {payload.symbol} hash={payload.hash}")
        except Exception as e:
            logger.warning(f"trade-outcome open: failed to sync position: {e}")

    # v0.19.6.1 (2026-04-10): Sync open_positions on close events from bot.
    # Previously only position_increased was handled; close events left positions
    # as status='open' until position_tracker desync detection (5-249 min delay).
    _CLOSE_EVENTS = {
        'sl_hit', 'tp3_hit', 'full_close', 'timeout',
        'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit',
        'dumalka_close', 'manual_close', 'flip_close',
    }
    if payload.event in _CLOSE_EVENTS and payload.hash:
        try:
            pos_row = await pg_fetch_one(
                "SELECT id, max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct "
                "FROM open_positions WHERE signal_hash = ? AND status = 'open' LIMIT 1",
                (payload.hash,)
            )
            await pg_execute("""
                UPDATE open_positions SET status = 'closed',
                    closed_at = ?, close_reason = ?, realized_pnl_pct = ?,
                    current_price = ?
                WHERE signal_hash = ? AND status = 'open'
            """, (
                datetime.now(timezone.utc).isoformat(),
                payload.event, payload.pnl_pct, payload.price, payload.hash,
            ))
            logger.info(f"trade-outcome close: {payload.symbol} {payload.event} hash={payload.hash}")
            if pos_row:
                try:
                    await pg_execute(
                        """INSERT INTO dumalka_audit_log
                           (timestamp, trace_id, symbol, action, pos_id, details)
                           VALUES (?, ?, ?, 'position_closed', ?, ?)""",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            payload.hash,
                            payload.symbol,
                            pos_row["id"],
                            json.dumps({
                                "close_reason": payload.event,
                                "source": "bot_api",
                                "pnl_pct": round(payload.pnl_pct, 4) if payload.pnl_pct is not None else None,
                                "price": payload.price,
                                "max_pnl_pct": round(pos_row["max_pnl_pct"], 4) if pos_row["max_pnl_pct"] else None,
                                "drawdown_pct": round(pos_row["drawdown_from_peak_pct"], 4) if pos_row["drawdown_from_peak_pct"] else None,
                                "tp_progress_pct": round(pos_row["tp_progress_pct"], 4) if pos_row["tp_progress_pct"] else None,
                            }),
                        ),
                    )
                except Exception as audit_err:
                    logger.warning(f"trade-outcome: audit log write failed: {audit_err}")
        except Exception as e:
            logger.warning(f"trade-outcome: failed to close position: {e}")

    # v0.18.5: Sync open_positions when bot merges a position (same symbol, 2nd signal)
    if payload.event == "position_increased" and payload.hash:
        try:
            await pg_execute(
                "UPDATE open_positions SET entry_price = ?, "
                "size = size + COALESCE(?, 0), current_price = ? "
                "WHERE signal_hash = ? AND status = 'open'",
                (payload.price, payload.size_remaining, payload.price, payload.hash),
            )
            logger.info(f"position_increased: updated open_positions for {payload.symbol} (hash={payload.hash})")
        except Exception as e:
            logger.warning(f"position_increased: failed to update open_positions: {e}")

    # v0.19.8: TG observability for trade close events
    asyncio.create_task(send_trade_event_report(
        event=payload.event, symbol=payload.symbol, side=payload.side,
        pnl_pct=payload.pnl_pct, price=payload.price,
        signal_hash=payload.hash, linked=re_recommendation is not None,
    ))

    return {
        "status": "recorded",
        "hash": payload.hash,
        "event": payload.event,
        "linked_to_signal": re_recommendation is not None,
    }

@app.get("/effectiveness")
async def effectiveness():
    """
    Analyze RE recommendation effectiveness vs actual trade P&L.
    Requires accumulated trade_outcomes linked via signal_hash.
    """
    stats = await get_effectiveness_stats()
    return stats

# ─── Signals History ──────────────────────────────────────────────────────────

@app.get("/signals", response_model=List[SignalRecord])
async def list_signals(limit: int = 50):
    """View recent signals evaluated by the Risk Engine."""
    rows = await get_recent_signals(limit)
    return rows

# Duplicate /dashboard route removed (already defined above at line 82)

# ─── Open Positions ──────────────────────────────────────────────────────────

@app.get("/open-positions")
async def get_open_positions():
    """Return all tracked positions for the dashboard."""
    rows = await pg_fetch_all(
        "SELECT * FROM open_positions WHERE status IN ('open', 'phantom') "
        "ORDER BY CASE WHEN status='open' THEN 0 WHEN status='phantom' THEN 1 ELSE 2 END, id DESC LIMIT 50"
    )
    return [dict(r) for r in rows]


@app.post("/api/admin/force-close-position")
async def api_admin_force_close_position(
    body: dict = Body(...),
    x_force_close_secret: str | None = Header(None, alias="X-Force-Close-Secret"),
):
    """
    Close a ghost open/phantom row when DB diverged from exchange (v0.18.3).

    Requires env FORCE_CLOSE_SECRET; send same value in header X-Force-Close-Secret.
    Body: {\"pos_id\": 123, \"detail\": \"optional reason\"}
    """
    if not config.FORCE_CLOSE_SECRET:
        raise HTTPException(status_code=503, detail="FORCE_CLOSE_SECRET not configured")
    if not x_force_close_secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Force-Close-Secret")
    try:
        secret_ok = secrets.compare_digest(
            x_force_close_secret.encode("utf-8"),
            config.FORCE_CLOSE_SECRET.encode("utf-8"),
        )
    except (ValueError, TypeError, UnicodeEncodeError):
        secret_ok = False
    if not secret_ok:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Force-Close-Secret")

    pos_id = body.get("pos_id")
    if pos_id is None:
        raise HTTPException(status_code=400, detail="pos_id required")
    try:
        pid = int(pos_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="pos_id must be integer")

    detail = str(body.get("detail") or "manual admin force close via API")
    result = await admin_force_close_stale_position(pid, detail=detail)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "failed"))
    return result


@app.get("/info")
async def get_info_page():
    """
    Returns the frontend configuration and system component health checks
    used by the main dashboard UI.
    """
    return FileResponse("static/info.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

# ─── Direct Evaluate ──────────────────────────────────────────────────────────

@app.post("/evaluate", response_model=RiskResult)
async def evaluate_risk(request: RiskRequest):
    """Direct evaluate endpoint for a candidate trade."""
    try:
        t0 = time.perf_counter()
        result = await asyncio.to_thread(
            run_monte_carlo_risk,
            portfolio=request.portfolio,
            candidate=request.candidate_trade,
            market=request.market,
            limits=request.risk_limits,
            n_scenarios=request.n_scenarios,
            confidence=request.confidence,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "evaluate | equity=%.0f scenarios=%d latency=%.1fms approved=%s var=%.4f cvar=%.4f",
            request.portfolio.equity,
            request.n_scenarios,
            latency_ms,
            result.approved,
            result.var,
            result.cvar,
        )
        return result
    except Exception as e:
        logger.error("Risk evaluation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/portfolio-risk", response_model=RiskResult)
async def portfolio_risk(request: RiskRequest):
    """Analyse risk of the current portfolio (ignores candidate_trade)."""
    request.candidate_trade = None
    return await evaluate_risk(request)

# ─── GPU Monitoring ───────────────────────────────────────────────────────────

import pynvml
from collections import deque

# 1-second samples, 3600 = 1 full hour of history
gpu_load_history = deque(maxlen=3600)
# Track GPU peaks during MC requests (captured by the webhook handler)
gpu_request_peaks = deque(maxlen=100)

try:
    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception as e:
    print(f"NVML Init failed: {e}")
    handle = None

def get_gpu_util_now() -> int:
    """Get current GPU utilization (0-100). Thread-safe."""
    if handle:
        try:
            return pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
        except:
            pass
    return 0

def record_gpu_peak(peak: int):
    """Record a GPU peak from a Monte Carlo request."""
    gpu_request_peaks.append(peak)

async def sample_gpu_load():
    """Background task to sample GPU utilization every 1 second."""
    logger.info("🖥️ GPU load sampler started (interval=1s)")
    while True:
        try:
            util = get_gpu_util_now()
            gpu_load_history.append(util)
        except Exception:
            pass  # GPU sampling failure is non-critical, continue silently
        await asyncio.sleep(1)   # 1s for accurate burst capture

# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """GPU + DB health-check with 1-hour avg/peak."""
    import cupy as cp
    try:
        props = cp.cuda.runtime.getDeviceProperties(0)
        free, total = cp.cuda.Device(0).mem_info

        current_load = get_gpu_util_now()

        # Combined history: sampled + request peaks
        all_loads = list(gpu_load_history) + list(gpu_request_peaks)
        avg_load = sum(all_loads) / len(all_loads) if all_loads else 0
        max_load = max(all_loads) if all_loads else 0

        # Non-zero loads for "active" average
        active_loads = [l for l in all_loads if l > 0]
        avg_active = sum(active_loads) / len(active_loads) if active_loads else 0

        # v0.9.4 FIX-4: Background task health
        now = time.time()
        task_health = {}

        # Collect heartbeats from all sources
        from position_tracker import _heartbeat as pt_hb
        try:
            from watchlist_scanner import _heartbeat as ws_hb
        except Exception:
            ws_hb = {}

        try:
            from health_watchdog import _heartbeat as wd_hb
        except Exception:
            wd_hb = {}

        all_heartbeats = {
            "position_tracker": pt_hb,
            "analytics_precompute": _task_heartbeats.get("analytics_precompute", {}),
            "bybit_pnl_refresh": _task_heartbeats.get("bybit_pnl_refresh", {}),
            "watchlist_scanner": ws_hb,
            "exit_quality": exit_quality._heartbeat,  # v0.18.0
            "ml_labeler": _task_heartbeats.get("ml_labeler", {}),
            "kline_collector": _task_heartbeats.get("kline_collector", {}),
            "scout": _task_heartbeats.get("scout", {}),
            "health_watchdog": wd_hb,  # v0.19.7
        }
        stale_thresholds = {
            "position_tracker": 300,      # 5 min (runs every 60s)
            "analytics_precompute": 5400,  # 90 min (runs every 60 min)
            "bybit_pnl_refresh": 600,      # 10 min (runs every 5 min)
            "watchlist_scanner": 600,       # 10 min (runs every 5 min)
            "exit_quality": 600,            # 10 min (runs every 5 min) — v0.18.5
            "ml_labeler": 6 * 3600 + 600,  # 6h 10min (runs every 6h)
            "kline_collector": 300,       # 5 min (runs every 60s)
            "scout": 1800,                # 30 min (runs every 15 min)
            "health_watchdog": 300,       # 5 min (runs every 60s) — v0.19.7
        }
        for task_name, threshold in stale_thresholds.items():
            hb = all_heartbeats.get(task_name, {})
            if not hb:
                task_health[task_name] = "not_started"
            elif "last_error" in hb and "last_success" not in hb:
                task_health[task_name] = f"error: {hb.get('error', 'unknown')}"
            else:
                last = hb.get("last_success", 0)
                age = now - last
                if age > threshold:
                    task_health[task_name] = f"STALE ({age:.0f}s ago)"
                else:
                    task_health[task_name] = "healthy"

        overall_healthy = all(status == "healthy" for status in task_health.values())

        # v0.14.2: Error rate & operational metrics
        uptime_sec = round(time.time() - _op_metrics["startup_time"])
        uptime_h = round(uptime_sec / 3600, 1)
        total_reqs = _op_metrics["request_count"]
        error_5xx = _op_metrics["status_counts"].get("5xx", 0)
        error_rate = round(error_5xx / total_reqs * 100, 2) if total_reqs > 0 else 0.0

        # Log file size
        try:
            log_size_mb = round(os.path.getsize('logs/app.log') / 1024**2, 1)
        except Exception:
            log_size_mb = None

        # v0.19.7: Bot connectivity status for /health
        try:
            from position_tracker import _bot_last_success_ts, _bot_sync_failed
            _bot_age = round(now - _bot_last_success_ts, 1) if _bot_last_success_ts > 0 else None
            bot_status = {
                "reachable": not _bot_sync_failed,
                "last_success_ago_sec": _bot_age,
                "url": config.DUMALKA_BOT_URL,
            }
        except Exception:
            bot_status = {"reachable": False, "error": "import_failed"}

        return {
            "status": "healthy" if overall_healthy else "unhealthy",
            "version": app.version,
            "uptime_hours": uptime_h,
            "gpu": props["name"].decode(),
            "memory_free_mb": round(free / 1024**2),
            "memory_total_mb": round(total / 1024**2),
            "load_current": current_load,
            "load_avg": round(avg_load, 1),
            "load_avg_active": round(avg_active, 1),
            "load_max": max_load,
            "samples_count": len(gpu_load_history),
            "request_peaks": len(gpu_request_peaks),
            "background_tasks": task_health,
            "sentinel": get_sentinel_status(),
            "bybit_ws": get_price_feed_status(),
            "bot_connection": bot_status,
            "operational": {
                "total_requests": total_reqs,
                "error_rate_pct": error_rate,
                "status_counts": dict(_op_metrics["status_counts"]),
                "slow_requests_5s": _op_metrics["slow_requests"],
                "recent_errors": len(_op_metrics["errors"]),
                "log_size_mb": log_size_mb,
            },
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e), "version": app.version}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
