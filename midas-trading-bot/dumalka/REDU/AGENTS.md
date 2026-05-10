# AGENTS.md — LLM Context for Risk Engine

## Quick Reference

- **Version**: 0.19.9 (2026-04-12)
- **Open hypotheses & next steps**: `dumalka_next_steps.md` (H1 triple compression, H2 artifact recovery, H3 RIVER LONG, H4 skewness, H5 scout volume, H6 LONG vs SHORT)
- **ML Shadow Mode**: ExtraTrees+Optuna model (LOO AUC 0.574–0.621), logs predictions to `ml_predictions` table, no trade impact
- **Phase 2 checkpoint**: April 21, 2026 — evaluate H1 (N >= 10 zone exits with vol >= 4.0)
- **Main entry**: `src/main.py` (FastAPI)
- **Position tracker (Dumalka)**: `src/position_tracker.py` (~2.4K LOC)
- **Database**: PostgreSQL 18, adapter in `src/db_adapter.py`
- **GPU**: NVIDIA Titan V, Monte Carlo in `src/core/monte_carlo.py`
- **Decision log**: `docs/log.md` — append-only journal of significant decisions with rationale
- **Doc index**: `docs/index.md` — navigation map to all project documentation

## Investigating Dumalka Decisions

**Always check `dumalka_audit_log` FIRST when asked about position actions, closes, or phantom events.**

Since v0.18.6, every significant Dumalka decision is written to `dumalka_audit_log` with structured JSONB context.
Since v0.19.9, coverage is **100%** — all three close paths write `position_closed`:
- `source: "position_tracker"` — Dumalka-initiated (zone_full_exit, hard_sl_cap, sl_hit, etc.)
- `source: "bot_api"` — bot-initiated via `POST /trade-outcome` (sl_hit, dumalka_close, flip_close, etc.)
- `source: "webhook_reversal"` — reversal/re-entry close triggered by new signal in `POST /tv-webhook`

```sql
-- Full audit trail for a position
SELECT timestamp, action, details, mc_diagnostics, market_state
FROM dumalka_audit_log WHERE pos_id = <ID> ORDER BY timestamp;

-- All closes in last 24h
SELECT pos_id, symbol, action, details->>'close_reason' as reason,
  (details->>'pnl_pct')::float as pnl
FROM dumalka_audit_log
WHERE action = 'position_closed' AND timestamp > NOW() - INTERVAL '24h'
ORDER BY timestamp DESC;

-- Phantom transitions
SELECT * FROM dumalka_audit_log WHERE action = 'phantom_transition' ORDER BY timestamp DESC;
```

### Key Tables for Trade Analysis

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `dumalka_audit_log` | All Dumalka decisions (v0.19.9: **100% coverage**) | pos_id, action, details (JSONB), market_state |
| `open_positions` | Position state (open/closed/phantom) | id, symbol, side, close_reason, realized_pnl_pct |
| `position_snapshots` | Per-cycle ML features (~278/position) | pos_id, zone, action_taken, mc_p_tp, mc_p_sl, future_pnl_max_24h |
| `what_if_outcomes` | Post-close trajectory + peak tracking | pos_id, pnl_1h_after, pnl_4h_after, pnl_24h_after, max_pnl_24h |
| `trade_outcomes` | Trade events (open, close, tp1_hit) | signal_hash, event_type, pnl_pct |
| `signals` | Incoming signal evaluations | signal_hash, symbol, side, score, recommendation |
| `klines_history` | Historical candles (15m/1h/4h) | symbol, timeframe, open_time, OHLCV |
| `scout_signals` | Autonomous shadow signals (v0.19.2: 8 types, 12 features) | symbol, side, signal_type, shadow_pnl_*, confidence |
| `derivatives_snapshots` | Funding/OI/LSR time-series (v0.19.1) | symbol, ts, funding_rate, long_short_ratio, oi_change_pct |
| `ml_predictions` | ML shadow predictions (v0.19.6) | pos_id, prob_profit, prediction, model_version, features_json |

### Audit Action Types

`position_closed`, `phantom_transition`, `observe_only`, `phantom_retry_ok`, `grace_proven`, `apollo_shadow`, `time_decay_exempt`, `move_sl`, `full_close`, `partial_close`, `*_failed`, `*_breaker`, `*_shadow`, `*_error`

**`position_closed` details JSONB fields** (v0.19.9):
`close_reason`, `source` (`position_tracker` / `bot_api` / `webhook_reversal`), `pnl_pct`, `price`, `max_pnl_pct`, `drawdown_pct`, `tp_progress_pct`, `detail` (reversal only), `new_signal_score` (reversal only).

## Code Conventions

- SQL placeholders use `?` (psycopg2 via `db_adapter.py`)
- Async throughout: `await pg_execute(...)`, `await pg_fetch_one(...)`
- Config via env vars with defaults in `src/config.py`
- Logs: `logger = logging.getLogger("risk-engine.tracker")`
- Service management: `sudo systemctl restart risk-engine`
- Health: `curl http://localhost:8000/health`
- Kline data: `SELECT * FROM klines_history WHERE symbol='X' AND timeframe='1h' ORDER BY open_time DESC LIMIT 10`
- Scout signals: `SELECT * FROM scout_signals ORDER BY created_at DESC LIMIT 20`
- Derivatives curve: `SELECT * FROM derivatives_snapshots WHERE symbol='X' ORDER BY ts DESC LIMIT 50`
- **Kline Collector**: `src/kline_collector.py` (2-tier: OKX → Bybit Proxy, adaptive source cache)
- **Scout Generator**: `src/scout.py` (8 signal types, 12 ML features, derivatives time-series, shadow mode)
- **Multi-horizon ML**: `future_pnl_12h`, `future_pnl_max_24h` in position_snapshots (v0.19.2)
- **Peak tracking**: `max_pnl_24h` in what_if_outcomes (v0.19.2)
- **Labeler**: `src/scripts/label_optimal_actions.py` (multi-horizon + Hard SL Cap guard, v0.19.2)
- **TP1 Soft BE**: ATR-adaptive skip when tp1_dist < 2×ATR(1h) — keeps original SL (v0.19.4)
- **TP1 BE config**: `TP1_BE_ATR_SKIP_MULT`, `TP1_BE_OFFSET_MULT`, `TP1_BE_MIN_PCT`, `TP1_BE_MAX_PCT` in `.env`
- **Deep kline backfill**: `src/scripts/backfill_klines_deep.py` — one-time paginated fetch (500 1h + 200 4h per symbol, v0.19.5)
- **Enriched audit diagnostics**: zone_full_exit/e_pnl_full_exit/sl_breakeven log volatility, af, vol_mod, regime_dd, heat, zone, base_thresh (v0.19.5)
- **ML Shadow Mode**: ExtraTrees+Optuna model loaded at startup from `src/models/et_shadow_v1.pkl` (v0.19.6)
- **ML Shadow config**: `ML_SHADOW_ENABLED`, `ML_SHADOW_CONFIDENCE_THRESHOLD` in `.env`
- **ML predictions**: `SELECT * FROM ml_predictions WHERE pos_id = N ORDER BY created_at`
- **ML shadow API**: `GET /api/ml-shadow` — recent predictions + model status
- **Train model**: `cd /opt/risk-engine/src && python3 scripts/train_shadow_model.py` (safe to re-run)
- **ML experiment scripts**: `src/scripts/ml_experiment_v4.py` (full zoo), `src/scripts/ml_experiment_v7.py` (ET+GPU)

## 3-Tier Volatility Classification (v0.19.5 analysis)

Validated on 200+ positions with natural data gaps (no symbol crosses between tiers):

| Tier | Vol range | Symbols | Behavior | Threshold impact |
|------|-----------|---------|----------|------------------|
| DEFINITE HIGH | >= 4.0 | SIREN, NOM, STO | Rockets possible, all zone_full_exit rockets here | Triple compression: af=0.5 × vol_mod=0.8 × regime → 66-77% threshold reduction |
| TRANSITIONAL | 2.0-4.0 | RIVER, CUSDT, KERNEL | Oscillate. HIGH phase = danger, not opportunity | Current thresholds correct or conservative |
| NORMAL | < 2.0 | All others (19 symbols) | Stable. Never reaches 2.0 | Thresholds work as designed |

Key finding: RIVER/CUSDT perform WORSE during high-vol phases (RIVER: -0.51% vs +0.81%, CUSDT: -2.95% vs -0.04%). Phase 2 threshold changes (if confirmed by April 21 data) apply ONLY to vol >= 4.0.

```sql
-- Analyze zone_full_exit by volatility tier (v0.19.5 enriched diagnostics)
SELECT
  CASE WHEN (d.mc_diagnostics::json->>'volatility')::float >= 4.0 THEN 'HIGH'
       WHEN (d.mc_diagnostics::json->>'volatility')::float >= 2.0 THEN 'TRANS'
       ELSE 'NORMAL' END AS tier,
  COUNT(*) AS n, ROUND(AVG((d.mc_diagnostics::json->>'af')::float), 3) AS avg_af,
  ROUND(AVG((d.mc_diagnostics::json->>'vol_mod')::float), 3) AS avg_vol_mod
FROM dumalka_audit_log d
WHERE d.action = 'full_close' AND d.timestamp >= '2026-04-07'
GROUP BY 1;
```
