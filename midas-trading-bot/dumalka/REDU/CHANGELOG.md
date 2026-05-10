# Changelog

All notable changes to the **Risk Engine** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]
### Added
- Phase 5: XGBoost Scoring + Supervised ML on 72K+ snapshots (67,555 labeled, baseline trained)
- Phase 7: Full Autonomy (planned)

---

## [0.19.9] - 2026-04-12 — Audit Log Complete Coverage

### Fixed
- **`main.py` — `/trade-outcome` audit gap**: Close events from bot (`sl_hit`, `tp3_hit`, `dumalka_close`, `flip_close`, `manual_close`, etc.) updated `open_positions` but never wrote `position_closed` to `dumalka_audit_log`. Fixed: before UPDATE, fetches `max_pnl_pct`, `drawdown_from_peak_pct`, `tp_progress_pct`; after UPDATE, inserts structured `position_closed` entry with `source: "bot_api"` and full JSONB details.
- **`main.py` — reversal/re-entry audit gap**: When `/tv-webhook` closes an existing position for a reversal or re-entry signal, the `full_close` command was logged but the final `position_closed` entry was never written. Fixed: now inserts audit entry with `source: "webhook_reversal"`, including `new_signal_score`.
- **`main.py` — reversal/re-entry `realized_pnl_pct`**: Reversal/re-entry DB close (`UPDATE open_positions`) was setting `status='closed'` and `close_reason` but not `realized_pnl_pct`. Fixed: `current_pnl_pct` is now captured before UPDATE and written to `realized_pnl_pct`.
- **`main.py` — missing `import json`**: Module-level `import json` was absent; audit writes via `json.dumps()` crashed silently (caught by inner try/except). Added `import json` to top-level imports.

### Changed
- **DB backfill**: 20 historical `position_closed` audit entries backfilled for positions closed after v0.18.6 (9 entries: reversal/re-entry/bot_api paths; 11 entries: v0.18.6 deploy day). Source field set to `backfill_v0.19.9` / `backfill_pre_audit` for traceability.
- **DB cleanup**: 54 orphan audit entries (from deleted test positions) and 2 `test_action` entries removed.

### Coverage
- `dumalka_audit_log.position_closed` coverage: **100%** (103/103 closed positions post-v0.18.6). Previously ~81% (83/103) due to two unaudited close paths.
- Three `position_closed` sources now queryable: `position_tracker` (Dumalka-initiated), `bot_api` (bot HTTP close events), `webhook_reversal` (signal-triggered reversal).

---

## [0.19.8] - 2026-04-11 — API-first Bot Integration

### Added
- `docs/BOT_API_CONTRACT.md` — comprehensive HTTP API contract for trading bot developer (v2.0). Covers all 4 communication paths, full request/response schemas, 14 event types, decision mapping, error handling, timeout behavior, cURL examples, and 3-phase migration checklist.
- `notifications.py`: `send_signal_report()` — human-readable TG signal evaluation report ported from `telegram_bridge.format_and_send_report`. Fires asynchronously when `source=bot_direct` or `source=approval_flow`.
- `notifications.py`: `send_trade_event_report()` — TG trade lifecycle notification on close events (sl_hit, tp3_hit, full_close, etc.). Fired from `/trade-outcome` endpoint.
- `/trade-outcome`: `event: "open"` handler — upserts entry into `open_positions` with real exchange fill price. If position exists (registered by `/tv-webhook`), updates `entry_price`/`current_price`/`size`; if missing, inserts new row. Safety net for slippage reconciliation.

### Fixed
- `main.py`: Dedup guard (`DEDUP_WINDOW_SEC=5s`) was bypassed only for `source=approval_flow`. Direct bot API calls with `source=bot_direct` were silently dedup-rejected on retry within 5s window. Fixed: added `bot_direct` to `_skip_dedup` check alongside `approval_flow`.
- `models.py`: `rejection_reason` (values: `already_in_position`, `reentry_cooldown`, `whipsaw_protection`, `duplicate_webhook_dedup`) was passed to `EnrichedRiskResult` constructor but not declared in the Pydantic model — silently dropped during serialization. Added `rejection_reason: Optional[str] = None` to model.

### Changed
- `main.py`: FastAPI version bumped `0.19.6` → `0.19.8`.
- `notifications.py`: Module docstring updated with v0.19.8 changelog entry.

---

## [0.19.7] - 2026-04-11 — Health Watchdog

### Added
- `src/health_watchdog.py` (new module): async background task (`health_watchdog_loop`) that monitors all 9 system components (position_tracker, watchlist_scanner, exit_quality, shadow_pnl, sentinel, bybit_ws, kline_collector, scout, ml_labeler) plus bot connectivity (`_bot_last_success_ts`). Sends Telegram alerts on stale/down with severity levels (CRITICAL / WARNING / INFO), 30-min cooldown deduplication, and recovery notifications.
- `config.py`: `HEALTH_WATCHDOG_ENABLED` (default: true), `BOT_UNREACHABLE_ALERT_SEC` (default: 420s = 7 min), `WATCHDOG_ALERT_COOLDOWN_SEC` (default: 1800s = 30 min).
- `position_tracker.py`: `_bot_last_success_ts` — epoch timestamp of last successful `/dumalka/positions` response, read by watchdog to detect bot connectivity loss.
- `main.py`: `/health` endpoint now includes `bot_connection` status and `health_watchdog` heartbeat.

### Changed
- Startup grace period of 180s prevents false "not_started" alerts while components are initializing. Watchdog itself sleeps 90s before first check cycle.

---

## [0.19.6.1] - 2026-04-10 — Close Event Fix + Bot Integration

### Fixed
- **VALID_EVENTS gate** (`telegram_bridge.py:87`): Added `dumalka_close`, `manual_close`, `flip_close`. Previously these bot events were silently dropped at parse stage (line 365), making the close handler at line 502 unreachable. Dmitry's PR (20f6db3) fixed line 502 but missed this gate. Evidence: 14 `manual_close` events in DB resulted in wrong close reasons (bot_sync_desync/phantom_sl_hit) with 5-249 min delay.
- **/trade-outcome HTTP endpoint** (`main.py`): Added close-event handler that updates `open_positions` to `status='closed'`. Previously only `position_increased` was synced; close events left positions open until position_tracker desync detection.

### Added
- `BOT_INTEGRATION_CHANGELOG.md` — Dmitry's bot-side change documentation (v0.10.2-v0.10.6).
- Test suite `src/tests/test_v019_close_events.py` — 17 unit + 3 integration tests for close-event handling.
- Documentation: `docs/index.md` (navigation), `docs/log.md` (decision journal), scout C6 hypothesis.

### Changed
- `models.py`: Updated `TradeOutcomePayload.event` docstring with all 13 event types.
- `scout.py`: `_SPIKE_ATR_MULT` 3.0 -> 2.5 based on backtest (34 symbols, +2.1% avg PnL).

### Tech Debt Noted
- 16 analytics SQL queries in `main.py`/`db.py`/`watchlist_scanner.py` use hardcoded close-event lists without new types. Deferred to separate commit (analytics-only, no trade impact).

---

## [0.19.6] - 2026-04-08 — ML Shadow Mode

### Added
- **ML Shadow Mode**: ExtraTrees+Optuna model loaded at startup from `src/models/et_shadow_v1.pkl`. Predicts profit probability for each position after 5 snapshots (~2.5 min). Logs predictions to new `ml_predictions` table. No trade impact — purely observational for out-of-sample validation.
- **New table `ml_predictions`**: pos_id, prob_profit, prediction, model_version, features_json. Indexed by pos_id and created_at.
- **New endpoint `GET /api/ml-shadow`**: Recent ML predictions with join to position outcomes, model status, and configuration.
- **New config params**: `ML_SHADOW_ENABLED` (default: true), `ML_SHADOW_CONFIDENCE_THRESHOLD` (default: 0.65).
- **Training script**: `src/scripts/train_shadow_model.py` — trains ExtraTrees+Optuna (200 trials, 5-fold CV), saves model+imputer+metadata to `src/models/et_shadow_v1.pkl`. Safe to re-run anytime.

### ML Experiment Results (EXP-1 through EXP-7)
- Tested 12+ models: LightGBM, XGBoost, CatBoost, LogReg, RandomForest, HistGBM, ExtraTrees, SVM, Ridge, Stacking, TabNet (GPU), FT-Transformer (GPU), TabICL, FLAML, GaussianProcess.
- **Champion: ExtraTrees+Optuna** — LOO AUC 0.574-0.621 (varies by seed/trial count), best confidence filter: 100% win rate at 0.80 threshold (9 trades in-sample).
- GPU neural networks (TabNet, FT-Transformer) require 1K+ rows; foundation models (TabICL, TabPFN) need 300+ positions. Both underperformed at N=98.
- Key finding (H6): LONG trades systemically weaker than SHORT (54.7% vs 73.3% win rate, +1.0% vs +25.8% total PnL).

---

## [0.19.5] - 2026-04-07 — Volatility Risk Profile: Audit Enrichment + Deep Kline Backfill

### Added
- **Enriched audit diagnostics**: `zone_full_exit`, `e_pnl_full_exit`, and `sl_breakeven` events now log `volatility`, `adaptive_factor`, `vol_modifier`, `regime_dd_sensitivity`, `heat_modifier`, zone name, base threshold, and `volume_ratio` in `mc_diagnostics` JSON. Enables post-mortem analysis of the "triple compression" problem (af × vol_mod × regime can reduce Zone 3 threshold from 15% to 5%) without joining `position_snapshots`.
- **Deep kline backfill script**: `scripts/backfill_klines_deep.py` fetches 500 1h (~21 days) and 200 4h (~33 days) candles per symbol using paginated OKX/Bybit Proxy requests (100 per chunk, INSERT ON CONFLICT DO NOTHING). Covers all 41 tracked symbols including high-vol assets (SIREN, NOM, STO).

### Analysis (documented, no code changes)
- **3-tier volatility classification validated** with natural data gaps:
  - DEFINITE HIGH (vol >= 4.0): SIREN (7.6-10.9), NOM (5.1-6.0), STO (9.9) — never drops below 5.0. ALL zone_full_exit rockets are in this tier.
  - TRANSITIONAL (2.0-4.0): RIVER (2.17-3.42), CUSDT (2.33-3.47), KERNEL (2.18-2.40) — oscillate. HIGH phase = worse performance (RIVER: -0.51% avg vs +0.81% in NORMAL; CUSDT: -2.95% avg).
  - NORMAL (vol < 2.0): all other symbols — never reach 2.0. Stable performance.
- **Triple compression root cause**: for SIREN, adaptive_factor=0.5 (floor) × vol_modifier=0.8 (low liquidity) × regime=0.85-1.2 compresses Zone 3 threshold from 15% to 5.1-7.2%. Counterfactual on 4 post-HSC exits: raising af floor to 0.6 would help 1 trade (+14% captured), harm 0. Deferred to Phase 2 (April 21 checkpoint) pending N >= 10 data.

### Notes
- Phase 1 changes (audit enrichment + backfill) are zero-risk: no behavior changes, only richer data for analysis.
- Scout is now generating signals for SIREN, NOM, RIVER, VVV (crossed 55-candle threshold). 4/5 resolved shadow signals are profitable.
- Sync fix from v0.19.3+ confirmed effective: 0 phantom exits since April 5.
- Phase 2 (conditional DD threshold adjustment for vol >= 4.0 only) planned for April 21 after data accumulation.

---

## [0.19.4] - 2026-04-05 — ATR-adaptive TP1 Soft BE

### Fixed
- **TP1 Soft BE formula was effectively constant at 0.5%**: `max(0.002, min(0.005, atr_4h * 1.5))` always hit the 0.5% cap because all real assets have `atr_4h > 0.33%`. Replaced with ATR-scaled formula: `max(TP1_BE_MIN_PCT, min(TP1_BE_MAX_PCT, atr_1h × TP1_BE_OFFSET_MULT))`, range 0.5%–3.0%.

### Added
- **ATR noise zone skip**: When TP1 distance from entry is less than `TP1_BE_ATR_SKIP_MULT × ATR(1h)` (default 2.0), skip the BE move entirely and keep the original SL. Prevents stop-outs from normal price oscillation on volatile assets. Validated on 7/7 historical TP1-BE cases since 27.03 — skip equal or better in all, +21.9% cumulative PnL recovered.
- **Config parameters** in `config.py`: `TP1_BE_ATR_SKIP_MULT` (2.0), `TP1_BE_OFFSET_MULT` (0.5), `TP1_BE_MIN_PCT` (0.005), `TP1_BE_MAX_PCT` (0.030) — all adjustable via `.env` without redeploy.

### Notes
- Apollo Soft BE (stale positions, lines ~1672–1806) is a separate code path and is NOT modified by this release.
- The skip decision is logged with full diagnostics (tp1_dist, atr_1h, threshold) for observability.
- Key cases: NOM #597 (+0.26% → +7.66% potential), FARTCOIN #457 (+0.20% → +7.86%), HBAR #459 (+0.18% → +4.63%).

---

## [0.19.2] - 2026-04-04 — Rocket Catcher: Multi-Horizon ML + Peak Tracking

### Added
- **Multi-horizon ML columns** in `position_snapshots`: `future_pnl_12h` (46.2% fill rate), `future_pnl_max_24h` (100% fill rate). Enables ML to see long-term outcomes for each snapshot, not just 1h/4h.
- **Peak tracking** in `what_if_outcomes`: `max_pnl_24h` (max favorable PnL within 24h post-close), `max_pnl_24h_hour` (hour of peak). Zero extra API calls — piggybacks on existing kline loop.
- **Scout signal type #8**: `spike_consolidation_breakout` — detects spike → consolidation → breakout pattern for second-wave re-entry. Shadow mode only, accumulating data.
- **Backfill script**: `scripts/backfill_future_pnl_multihorizon.py` — PostgreSQL backfill for 12h/max_24h columns from existing snapshot sequences.

### Changed
- **Labeler v2**: `label_optimal_actions.py` now uses multi-horizon logic with Hard SL Cap guard. If drawdown > 3.5%, label = "close" regardless of future recovery (prevents ML from learning impossible hold scenarios). If short-term PnL is negative but `future_pnl_max_24h >= 3%`, label = "hold" (teaches ML to tolerate temporary dips before rockets).
- **Dynamic symbol tracking**: `kline_collector._get_symbols()` and Scout symbol list now include symbols with positions closed in last 48h (was: only open positions). Ensures kline/scout data continues after position close for ML training and what_if analysis.
- **Label distribution shift**: close 16,345 (was 13,324), hold 83,785 (was 86,399), partial_close 1,872 (was 1,951) — Hard SL Cap guard correctly reclassified ~3K snapshots.
- Scout scans **34 symbols** (was 26-27), now covering SIREN, RIVER, STO, VVV, and all recently-traded symbols.

### Notes
- All changes are shadow-mode / data-only — zero impact on live trading mechanics.
- `future_pnl_12h` fill rate is 46.2% because most positions last < 12h. This is expected; the column is most valuable for longer-lived positions.
- `future_pnl_max_24h` is updated via `GREATEST()` pattern on every tracker cycle (~30s) — running peak, not one-time fill.
- Exit quality recalculation triggered for all 580 closed positions (populates `max_pnl_24h`).
- `spike_consolidation_breakout` params: spike > 3x ATR above EMA20, consolidation < 1.5x ATR over 3 candles, breakout with volume > 1.5x avg. Conservative thresholds for data accumulation.

---

## [0.19.1] - 2026-04-04 — Scout Signal Enhancement + Derivatives Time-Series

### Added
- **3 new Scout signal types** (total 7): `funding_extreme_reversal` (contrarian on |funding|>0.03%), `volume_breakout` (vol>2.5x avg + directional candle), `rsi_divergence` (price/RSI divergence detection)
- **4 new ML features per signal** (total 12): `long_short_ratio` (OKX L/S ratio), `atr_pct` (ATR/price normalized vol), `ema_distance_pct` (momentum from EMA50), `price_change_4h` (from stored klines)
- **`derivatives_snapshots` table**: funding_rate + OI + long_short_ratio time-series per symbol, collected every Scout cycle (15 min). Foundation for funding curve regime detection and OI Z-Score
- **Investigation workflow update**: `dumalka-audit-log.mdc` now includes cross-reference steps for `klines_history`, `scout_signals`, and `derivatives_snapshots`

### Changed
- Scout confidence model refined: type-specific base scores, volume strength tiers, 4h alignment bonus
- Signal filtering: divergence/funding signals exempt from 4h trend filter (contrarian by design), volume_breakout exempt from volume floor filter
- Derivatives collection runs as Phase 3 of each Scout cycle (after signal gen + shadow resolve)

### Fixed
- **Derivatives API stagger**: Added 150ms sleep between API calls in `_collect_derivatives_snapshots` to prevent OKX/Bybit rate-limiting (78 rapid-fire calls → staggered)
- **Feature snapshot resilience**: `_snapshot_features` API calls now independent — one failing fetch no longer kills remaining features (funding/spread/volume/btc/lsr each in own try/except)
- **Scout cycle logging**: Now always logs cycle completion (`Scout cycle: 0 signals, 26 symbols scanned, 26 deriv snapshots (62.4s)`) instead of only when signals > 0

### Notes
- All changes are **SHADOW MODE** — zero impact on live trading
- `derivatives_snapshots` grows ~26 symbols × 96 rows/day ≈ 2,500 rows/day (lightweight)
- Funding extreme reversal: research shows 22-55% APR signal value on perpetual futures
- RSI divergence: classic reversal pattern, effective on 1h timeframe for altcoins
- Professional review: 14 unit tests, 4 API integration tests, DB schema + data flow tests, cross-system conflict tests — all passed

---

## [0.19.0] - 2026-04-04 — Kline Storage + Scout Infrastructure

### Added
- **Kline Historical Storage** (`klines_history` table): Stores 15m/1h/4h candles in PostgreSQL
  with composite PK `(symbol, timeframe, open_time)` for dedup. Background collector runs every 60s.
- **2-tier fetch with adaptive source cache**: OKX (geo-free, ~60% symbol coverage) → Bybit Proxy
  (100% coverage). Per-symbol cache learns optimal source after first cycle — Bybit-exclusive
  altcoins (STO, VVV, NOM, SYRUP, AVAAI) skip OKX automatically.
- **Scout Signal Generator** (`scout_signals` table): Autonomous shadow signal generator using
  EMA20/50 crossover, RSI-14, volume filter, regime filter, multi-TF 4h confluence.
  ATR-14 based SL/TP. SHADOW MODE ONLY — no commands sent to bot.
- **Feature snapshot per scout signal**: funding_rate, oi_change_pct, spread_pct, rsi_14,
  volume_ratio, btc_change_1h, multi_tf_trends, accumulation_score — future ML training dataset.
- **Shadow PnL tracking**: 1h/4h/24h shadow PnL and outcome (tp_hit/sl_hit/timeout) for
  scout signal quality evaluation.
- **API endpoints**: `/api/klines/{symbol}`, `/api/klines-stats`, `/api/scout-signals`.
- **Health monitoring**: kline_collector (stale: 5min) and scout (stale: 30min) in `/health`.
- **Auto-backfill**: On first startup, fetches 100 candles per symbol/TF to seed history.

### Fixed
- **kline_fetcher.py**: `end_ms` calculation now handles all Bybit intervals (was broken for
  4h/`"240"` — fell through to 5-minute default, causing wrong OKX time window).

### Notes
- Config flags: `KLINE_COLLECTOR_ENABLED`, `SCOUT_ENABLED` (both default `true`).
  Disable via `.env` for instant rollback.
- Storage: ~3,150 rows/day (~10 MB/month) — negligible vs current 74 MB DB.
- Proxy load: +10-30 req/min for Bybit-only symbols; well within 600 req/5s limit.
- Scout needs ~500+ signals with outcomes before ML training is viable (~2-4 weeks).

---

## [0.18.9] - 2026-04-04 — sl_breakeven Clamp Fix (SHORT)

### Fixed
- **sl_breakeven for SHORT positions**: ATR-adaptive offset (up to 3%) could push the
  breakeven SL below current market price for volatile SHORT positions. Bybit correctly
  rejected these as invalid (SHORT SL must be above market). Added clamp:
  `be_sl = max(be_sl, current_price * 1.001)` for SHORT, `min(be_sl, current_price * 0.999)` for LONG.
- **Impact**: All SHORT positions in Zone 1 now receive proper breakeven protection.
  Previously, ~100% of SHORT sl_breakeven attempts on volatile coins (RIVER, SIREN, etc.)
  were silently rejected by the bot.
- **Verified live**: RIVER #590 SL moved from 12.999 (original, -5.0%) to 12.173
  (breakeven, +1.67% locked) on first cycle after deploy.

### Changed
- **Zone 1 action**: `sl_breakeven` → `hold`. Breakeven SL at +1-2% profit caused shakeouts
  on micro-pullbacks, killing potential rockets. Data: 5 BE-stopped positions averaged +0.24%
  vs +3.40% if held 4h. RIVER #492 peaked at +8.76%, was BE-stopped at +0.09%.
  Hard SL Cap (-3.5%) already provides downside protection; Zone 1 BE was redundant.

### Notes
- No behavior change for LONG positions where offset < PnL (clamp is no-op).
- Edge case fix for LONG: if ATR offset equals PnL, SL is clamped 0.1% below current
  to avoid exact-price rejection.

---

## [0.18.8] - 2026-04-04 — Hard SL Cap Activation

### Changed
- **Hard SL Cap ACTIVATED**: `PHASE1_ACTIVE_MODE=true`, `MAX_LOSS_PCT=3.5%` (was 5.0% shadow-only).
  Backtest on 100 approve-only trades (since v0.16): PF improved from 0.946 to 1.263 (with ~0.5% slippage).
  10 catastrophic tail losses (KERNEL -14.57%, C -8.77%, NOM -7.71%) would have been capped at ~-3.5–4.0%.
- **Default `MAX_LOSS_PCT`** in `config.py` changed from 5.0 → 3.5.

### Notes
- Observe-only positions are NOT affected (existing `if not _observe_only` guard preserved).
- Partial close logic unchanged — no new partial close mechanisms introduced.
- Zone Policy, Apollo, Grace Period, all other Dumalka subsystems unchanged.
- This is Phase 1A of the loss-reduction roadmap. Data collection continues for next calibration.

---

## [0.18.7] - 2026-04-04 — ML Training Data Recovery

### Added
- **`finalize_snapshot_future_pnl`** (`db.py`): New function fills ALL remaining NULL `future_pnl_1h/4h` when a position closes, using `realized_pnl_pct` as ground truth. Recovers the last ~1h of each position's snapshots that were previously lost.
- **Auto-labeler backfill**: `ml_labeler_loop` automatically labels newly-filled snapshots. Result: 24,750 -> 33,659 labeled (+36%).

### Fixed
- **"Last hour gap"**: Snapshots from the final ~1h before position close had NULL `future_pnl_1h` because `update_snapshot_future_pnl` filtered `snapshot_at <= now() - 1 hour`. The new `finalize_snapshot_future_pnl` (called from `_close_position`) removes this constraint for closed positions.
- **One-time backfill**: SQL UPDATE recovered 10,670 `future_pnl_1h` + 25,475 `future_pnl_4h` for all historical closed positions.

### Documentation
- **`DUMALKA_NEXT_STEPS.md`**: Added ML Reality Check section (Apr 4, 2026) with verified data state, industry best practices (Triple Barrier, Meta-Labeling, Offline RL), and updated ML/RL roadmap with realistic timelines.

---

## [0.18.6] - 2026-04-04 — Structured Audit Log Expansion

### Added
- **`pos_id` column** (`dumalka_audit_log`): Direct FK to `open_positions.id` with partial index. Eliminates need for trace_id JOINs in post-mortem queries.
- **`details` JSONB column** (`dumalka_audit_log`): Structured decision context for every close, phantom transition, and state change event.
- **`market_state` auto-populated** (`dumalka_audit_log`): Regime + BTC 1h change on every audit entry (was always NULL).
- **`position_closed` audit event**: Written for ALL 13 close reasons (sl_hit, phantom_sl_hit, zone_full_exit, apollo_full_exit, timeout, bot_sync_desync, phantom_sync, etc.) with PnL, max_pnl, drawdown, tp_progress context.
- **`phantom_transition` audit event**: Written when DESYNC SHIELD converts a position to phantom mode (cmd_fail_count, last_action, last_bot_response).
- **`observe_only` audit event**: Written once per position when observe-only mode activates (rate-limited, not per-cycle).
- **`phantom_retry_ok` audit event**: Written when a phantom position reappears in bot roster after retry.
- **`grace_proven` audit event**: Written when a young position is marked "proven" (max_pnl, tp_progress, hours_open).
- **`apollo_shadow` audit event**: Written when Apollo bail would trigger but is disabled (Patience Protocol).
- **`time_decay_exempt` audit event**: Written when momentum override skips time-decay exit.
- **Keepalive diagnostics**: `send_command_to_bot` keepalive now passes `diagnostics={"trigger": "keepalive"}`.
- **`pos_id` threading**: All 6 `_write_audit_log` call sites in `send_command_to_bot` now pass `pos_id` from `kwargs["_pos_id"]`.

### Changed
- `_write_audit_log` signature expanded: `pos_id`, `details` params; auto-populates `market_state`.
- `_close_position` signature expanded: `trace_id` param for audit traceability.
- Audit log coverage: **~30% of decisions** (commands only) to **~95%** (commands + closes + state changes + shadow decisions).

---

## [0.18.5] - 2026-04-03 — Exit Quality Pipeline Fix + Bot v0.10.5 Integration

### Added
- **Webhook dedup** (`main.py`): In-memory `(symbol, side)` dedup with 5s window. Rejects duplicate webhooks within seconds (VVV double-signal case).
- **`position_increased` event handling** (`main.py`): Bot merges two signals into one Bybit position, RE syncs `open_positions`.
- **Auto SL→BE fields** (`models.py`, `main.py`): `auto_be_price` and `auto_be_trigger` in `EnrichedRiskResult`.
- **Phantom retry before auto-close** (`position_tracker.py`): 5s re-check before phantom_sync auto-close.
- **Support/Resistance keyword boost** (`main.py`): SR keywords in `midas_comment` boost score up to +0.09.
- **Integration tests** (`tests/test_v018_bot_integration.py`): 20 tests.

### Fixed
- **CHECK 2 TP1 breakeven sign reversed** (`position_tracker.py`): Long SL placed BELOW entry instead of ABOVE. Cap reduced 3% to 0.5%.
- **OKX SWAP-first** (`kline_fetcher.py`): 6/16 symbols required `-USDT-SWAP` format.
- **Sentinel rows** (`exit_quality.py`): Prevent infinite re-fetch of unprocessable positions.
- **Removed `close_reason` whitelist** (`exit_quality.py`): 69% of closed positions were invisible. All analyzed now.
- **DESC ordering + Throughput** (`exit_quality.py`): Newest first, batch 50/5min.

### Changed
- **Conviction mult 0.45-0.59**: Raised 0.5 to 0.7 (Bybit $5.5 min).
- Exit Quality coverage: 13% to 99%.

---

## [0.18.4] - 2026-04-02 — "Proven Position" grace bypass & TP1 idempotency
### Added
- **Proven Position grace bypass** (`position_tracker.py`): Sticky latch (`_proven_positions` set) — once a young (<1h) position reaches `max_pnl >= 3%` AND `tp_progress >= 50%`, zone policy / trailing / E_pnl gates unlock despite grace period. Prevents NOM #548 scenario: position reached +14.14% (Zone 4, tp 100%) but grace blocked `zone_full_exit` → SL hit at -3.71%, then NOM rallied +35%.
- **TP1 idempotency guard** (`position_tracker.py`): CHECK 2 `tp1_hit` now skips `move_sl` if current SL is already at or better than the calculated breakeven level (0.1% tolerance). Eliminates redundant API calls (NOM had 11 identical `move_sl` in 10 min).
- **Portfolio Stress Warning** (`main.py`): At signal intake, if same-direction open positions are underwater >5%, logs a warning and sends a Telegram alert. Informational only — no blocking. Catches concentrated directional exposure early.

### Changed
- Dashboard footer updated to v0.18.4.
- README badge updated to v0.18.4.
- Module docstrings in `position_tracker.py`, `config.py`, `main.py` updated with v0.18.4 date.

---

## [0.18.3] - 2026-04-02 — Bot roster desync & ghost OPEN cleanup
### Added
- **Auto-close on bot roster mismatch** (`position_tracker.py`): If `/dumalka/positions` succeeds and the symbol is absent for `BOT_ABSENT_CLOSE_CYCLES` consecutive tracker cycles (default **20** ≈ 10 min at 30s), the DB row is closed as `bot_sync_desync`. Fixes dashboard “OPEN” when the exchange/bot no longer has the trade (manual close, bot outage, etc.); observe-only alone never changed `status`.
- **Operator API** (`main.py`): `POST /api/admin/force-close-position` with JSON `{"pos_id": N, "detail": "..."}` and header `X-Force-Close-Secret` matching env `FORCE_CLOSE_SECRET`. Disabled (503) if secret unset.
- **`admin_force_close_stale_position()`** (`position_tracker.py`): closes with `manual_desync` + market PnL snapshot.

### Changed
- **Exit Quality backfill** (`exit_quality.py`): includes `bot_sync_desync` and `manual_desync` in eligible `close_reason` filters.

### Fixed (post-release review)
- **`admin_force_close_stale_position`**: now schedules `insert_trade_outcome` for `manual_desync` (parity with auto-close paths).
- **`BOT_ABSENT_CLOSE_CYCLES`**: invalid/empty env no longer crashes import — falls back to 20.
- **`/api/admin/force-close-position`**: header verified with `secrets.compare_digest` (timing-safe).
- **`BOT_ABSENT_CLOSE_CYCLES`**: clamped to `[1, 100_000]` after parse (negative/huge env typos).

---

## [0.18.2] - 2026-04-02 — Patience Protocol
### Changed
- **Apollo Bail disabled** (`position_tracker.py`): Step 1 (FULL CLOSE on stale 1-4h positions) converted to shadow-only logging `[APOLLO SHADOW]`. Step 2 (SL→soft breakeven) remains fully active. Re-enable via `APOLLO_BAIL_ENABLED=true` env var.
- **Young Position Grace Period** (`position_tracker.py`): increased from 0.5h to 1.0h (config-driven via `YOUNG_POSITION_HOURS` env var). First hour — only Hard SL Cap and exchange SL/TP can close; no Apollo, E_pnl, or Zone decisions.

### Added
- **2 new config params** (`config.py`): `APOLLO_BAIL_ENABLED` (default `false`), `YOUNG_POSITION_HOURS` (default `1.0`).

### Evidence (why Apollo Bail was disabled)
- 31 Mar — 1 Apr: 3/3 Apollo Bail exits were net-negative after commissions (WIF #525 +$0.16, WIF #527 -$0.68, GRASS #529 -$2.23). Avg PnL at close: +0.57% — fees exceeded profit.
- Apollo Bail killed positions at +0.15% to +0.96% — micro-profits that could become big winners per Kati's positive-skew strategy.
- SAHARAUSDT (1 Apr 23:35): closed by OLD e_pnl code at +0.5% — v0.18.1 was not deployed (service not restarted). This release includes service restart.

---

## [0.18.1] - 2026-04-02 — E_pnl Reform "Let Winners Run"
### Changed
- **Full E[PnL] formula** (`monte_carlo.py`): `simulate_sl_tp_probability()` now computes `full_e_pnl_pct` — the true mathematical expectation across ALL MC paths (barrier-hit + terminal price for open paths). The old formula (`P_tp*tp_dist - P_sl*sl_dist`) ignored 40-70% of paths (p_neither), systematically biasing E[PnL] negative.
- **TP cap removed from MC** (`position_tracker.py`): `run_forward_projection()` no longer applies `MAX_TP_FOR_ZONES_PCT` to the MC simulation. Real TP3 is passed to the simulator. The cap remains for zone progress math only (as originally documented). Evidence: CUSDT TP3=+99% was capped to 10%, making E[PnL] appear -0.06% when real full_E was +0.59%.
- **CHECK 4 redesigned** (`position_tracker.py`): Risk-Adjusted Exit now requires ALL conditions: `full_e_pnl < threshold` (-1.0% default), `not momentum_alive`, `skewness < 2.0`. Old CHECK 4 had no significance threshold (triggered at E=-0.001%) and no momentum coordination.
- **momentum_alive promoted**: computed once before CHECK 3.5, shared with CHECK 4. Previously was local to CHECK 3.5's time-decay block — CHECK 3.5 could say "momentum alive, letting it ride" and CHECK 4 would immediately override and close.
- **Jump-diffusion params configurable**: `MC_LAMBDA_JUMP`, `MC_MU_JUMP`, `MC_SIGMA_JUMP` in config.py (env-var-driven). Were hardcoded at lambda=2.0, mu=-0.05, sigma=0.10.

### Added
- **pnl_skewness metric** (`monte_carlo.py`): detects fat-tail/positive-skew trades (the "Kati strategy": small losses, big winners). Skewness > 2.0 overrides E_pnl exit — protects breakout positions.
- **pnl_p75 metric** (`monte_carlo.py`): 75th percentile PnL for tail analysis.
- **5 new config params** (`config.py`): `E_PNL_EXIT_THRESHOLD`, `E_PNL_SKEW_OVERRIDE`, `MC_LAMBDA_JUMP`, `MC_MU_JUMP`, `MC_SIGMA_JUMP`.
- **Snapshot enrichment**: `full_e_pnl`, `pnl_skewness` columns in `position_snapshots` for ML training.

### Evidence (why this was needed)
- **CUSDT** (2026-04-01): Closed LONG at +1.38% by e_pnl=-0.0%. Market then pumped to +15.2%. New full_e_pnl = +0.59%, skew=+1.41 → would NOT have closed.
- **RIVERUSDT** (2026-04-01): Closed SHORT at +3.98% by e_pnl=-3.4%. Market continued to +16%. New full_e_pnl = +0.34% → would NOT have closed.
- **e_pnl_full_exit** had 100% WR (only closed winners) with avg PnL +1.99% but actual potential +15%.

---

## [0.18.0] - 2026-04-01 — Exit Decision Quality Tracker
### Added
- **Exit Quality Module** (`exit_quality.py`): standalone analytical module evaluating ALL Dumalka exit decisions for training purposes. Zero impact on trading logic.
- **Kline Fetcher** (`kline_fetcher.py`): extracted `fetch_klines_with_fallbacks` from `main.py` to resolve circular import dependency.
- **Phase 1 — Post-close trajectory analysis**: background loop (`exit_quality_backfill_loop`, 15 min cycle) analyzes closed positions with SL/TP simulation + multi-horizon PnL (1h, 4h, 12h, 24h after close). Dual SQL filter covers all Dumalka + manual closes. ON CONFLICT idempotent upsert, kline drift sanity check.
- **Phase 2 — sl_breakeven effectiveness**: read-only analytics on `position_snapshots` to evaluate whether BE moves save capital or kill upside.
- **Phase 3 — Missed exit analysis**: read-only analytics on sl_hit positions where Dumalka did NOT close in time. Tracks leaked profit by zone, MC accuracy at peak, hours peak-to-SL. Auto-generates training insights.
- **Dashboard widget**: "Exit Quality — Dumalka Decision Analytics" with three sub-tables (Acted, BE Effectiveness, Missed Exits) + auto-generated training insights.
- **API endpoints**: `GET /api/exit-quality`, `POST /api/exit-quality/recalculate`, `GET /api/exit-quality/sl-breakeven`, `GET /api/exit-quality/missed-exits`. Old `POST /api/what-if-analysis` preserved for backward compatibility.
- **Schema migration**: `what_if_outcomes` gains `pnl_1h_after`, `pnl_4h_after`, `pnl_12h_after`, `pnl_24h_after` columns; `analyzed_at` converted from TEXT to TIMESTAMPTZ.
### Removed
- Old `what_if_analysis()` function (~170 LOC) from `main.py` — replaced by `exit_quality.py` module.

---

## [0.17.0] - 2026-03-31 — Same-Coin Re-Entry Protocol
### Added
- **Re-Entry Protocol**: New same-side signals for coins with active positions now pass to full scoring instead of blind `already_in_position` reject. If scored `approve`/`reduce`, old position is closed via Dumalka `full_close` before approving the new signal.
- **Config**: `REENTRY_ENABLED` (default `true`) kill switch, `REENTRY_COOLDOWN_HOURS` (default `2.0`) anti-whipsaw guard.
- **Dumalka Close for Reversals**: Existing Graceful Reversal Protocol (v0.14.5) now sends `full_close` to bot via Dumalka before DB update (was DB-only, leaving exchange position open).
- **midas_comment enrichment**: Signals that trigger re-entry or reversal include `RE_ENTRY: old #ID` or `REVERSAL: old #ID` in midas_comment for audit trail.
- **Safe position registration**: `_safe_register` now skips if recommendation was downgraded to `reject` (e.g. Dumalka close failed).
### Fixed
- **Reversal close gap**: v0.14.5 reversal only updated `open_positions` DB without sending `full_close` to bot — exchange position remained open. Now uses same Dumalka `send_command_to_bot` as position_tracker.

---

## [0.16.1] - 2026-03-31 — Score Correction & Data Integrity
### Added
- **Score Correction Backfill**: One-shot startup task that retroactively computes `corrected_score`, `corrected_recommendation`, and `score_quality_penalty` for all signals affected by the win_rate parsing bug. 62 of 287 signals would have had a different RE recommendation.
- **Corrected Benchmark Metrics**: `/api/midas-benchmark` now includes `corrected_approve_avg`, `corrected_reject_avg`, `corrected_delta_vs_midas`, `reclassified_count`, `penalty_affected_count`.
- **Dashboard Corrected Row**: Midas Benchmark panel shows corrected approve avg, corrected delta, and reclassified/affected counts.
- **Kline Drift Sanity Check**: Both shadow backfill passes now reject kline data with >24h timestamp misalignment (prevents stale API data from corrupting analytics).
- **resolved_at < created_at Guard**: Prevents impossible negative-time resolution data from being written to DB.
### Fixed
- **Guard INSERT price fallback** (`main.py:2404`): Changed from `entry_low or 0.0` to `entry_low or entry_high or stop_loss or 0.0`. Previously, whipsaw/already_in_position guard rejects wrote `price_at_signal = 0`, blocking shadow PnL backfill entirely.
- **Signal 527 (VVVUSDT)**: Reset corrupted shadow data caused by stale kline API response.

---

## [0.16.0] - 2026-03-31 — Midas Benchmark & Shadow Analytics
### Added
- **Midas Benchmark API** (`GET /api/midas-benchmark`): Live comparison of Midas signal quality vs RE filtering. Returns avg PnL by recommendation, TP win rates, resolve timing, and coverage stats.
- **Dashboard Midas Benchmark Panel**: 6-metric panel (Midas Avg, RE Approve, RE Delta, TP Win Rate, Avg Resolve Time, Coverage). Auto-refreshes every 30s.
- **Shadow Recheck (Second Pass)**: New loop in `shadow_pnl_backfill_loop` that re-examines stale "open" signals (checked >24h ago). Fetches klines from `shadow_checked_at` onward, batch of 5/cycle. Resolved 52 previously-stuck signals.
- **Shadow Resolution Tracking**: New columns `shadow_resolved_at` (TIMESTAMPTZ) and `shadow_resolved_candles` (INTEGER) record exact time and duration to SL/TP hit.
### Changed
- **Shadow PnL UPDATE**: Now uses `COALESCE(?, shadow_resolved_at)` to preserve existing resolution data on re-processing.

---

## [0.15.9] - 2026-03-30 — Database Hardening & Proxy Security
### Added
- **DB Constraints**: CHECK constraints on `signals.side`, `open_positions.status` (incl. 'phantom'). UNIQUE constraint on `signals.signal_hash`. FOREIGN KEY on `trade_outcomes.signal_hash → signals.signal_hash`.
- **ON CONFLICT**: Both `insert_signal()` and guard INSERT now use `ON CONFLICT (signal_hash) DO NOTHING` for idempotency.
- **Bybit Proxy Security**: Bound to Tailscale IP (`100.117.168.63:8002`) instead of `0.0.0.0`.
- **Proxy Error Handling**: `/market-data/{symbol}` wrapped in try/except, returns JSON error instead of 500.
- **Proxy File Logging**: RotatingFileHandler to `/opt/bybit-proxy/logs/proxy.log`.
- **Telegram Bridge Warning**: Reports missing `midas_win_rate`/`probability`/`risk_reward` in TG message.
### Fixed
- **Dashboard Signal History**: Fixed infinite "Loading..." caused by `size = NULL` rows. `get_recent_signals()` now normalizes NULL size to 1.0. `fetchJSON()` hardened with `r.ok` and Content-Type checks.
- **Bot win_rate Regex**: Fixed `signal_parser.py` regex to handle `"67.%"` format (`\s*%` → `\s*\.?\s*%`).

---

## [0.15.2] - 2026-03-30 — Forensic System Hardening
### Added
- **TP1 Normalizer**: Capped effective TP distance for Zone Policy math at 10% (`MAX_TP_FOR_ZONES_PCT`). This fixes the blind-spot bug causing positions with extreme TP targets (e.g. +43%, +89%) to never register internal zone progress.
- **Configurable Partial Closes**: Added `PARTIAL_CLOSE_ENABLED` feature flag to strictly gate partial closes while keeping full exits and SL moves active, addressing business stakeholder feedback on capture ratio.
- **Forensic System Analytics**: Added `night_analysis.py` enabling complete chronological event reconstruction between Midas Bot and Risk Engine tracking instances.

---

## [0.15.1] - 2026-03-29 — WebSocket Price Feed + Faster Cycle
### Added
- **Bybit WebSocket Price Feed** (`src/core/bybit_ws.py`): Real-time ticker stream via `wss://stream.bybit.com/v5/public/linear`. Subscribes to all 23 historically traded symbols. Shadow mode: observes prices, does NOT execute trade actions. Free, no geo-block from this server.
- **Dashboard Widget**: Premium "📡 Bybit WS Feed" widget showing live update rate, symbol count, uptime, and message stats. Cyan gradient styling.
- **API Endpoint**: `GET /api/ws-prices` returns real-time prices from WebSocket feed.
- **Health Reporting**: `bybit_ws` status included in `/health` endpoint.

### Changed
- **Monitoring Cycle 60s → 30s**: Position Tracker now runs every 30 seconds (was 60s), doubling the chance to catch profitable moments for SL→BE moves.
- **SL Move Cooldown 15min → 8min**: Faster retries for `move_sl` commands on transient failures, matching the 30s cycle cadence.
- **RECALIBRATE_EVERY**: Adjusted from 1440 to 2880 cycles to maintain ~24h zone recalibration interval.
- **WS Symbol Subscription**: Full portfolio (all historically traded symbols), not just currently open positions.

### Technical
- Version bump: `v0.15.0 → v0.15.1`
- New background task #6: Bybit WebSocket Price Feed
- Auto-reconnect with exponential backoff (5s → 60s after 10 consecutive failures)
- Re-subscribes every 5 minutes to pick up new symbols from open_positions table

---

## [0.15.0] - 2026-03-29 — Apollo Audit
### Changed
- **Apollo Protocol: Smart Size Guard** (`MIN_PARTIAL_CLOSE_USD=17`): All `partial_close` commands are now gated by position size. Bybit min notional = $5, so partial close only works at positions ≥ $17 (where 30% = $5.10). Below this threshold, the system uses `full_close` instead, eliminating **6,737 failed API calls** (99.96% failure rate in production).
- **Apollo Bail: Full Exit Fallback**: When position size is too small for fractional bail (70%), Apollo now sends `full_close` with proper `_close_position()` + `insert_trade_outcome()` tracking. The original fractional bail code is preserved and activates automatically when position sizes grow.
- **Zone Policy: Full Exit Fallback**: Zone 2-4 partial close triggers now fall back to `full_close` for small positions, with proper close reason tracking (`zone_full_exit`).
- **E_pnl Negative: Full Exit Fallback**: CHECK 4 (negative expected PnL while in profit) now uses `full_close` for small positions (`e_pnl_full_exit`).
- **R-Multiple: SL Lock Fallback**: At 2R/3R targets, small positions get `move_sl` to lock 1R/2R profit via trailing SL instead of failing partial close.
- **TP1 Hit: Skip Partial, Keep SL→BE**: At TP1 hit, small positions skip partial close but STILL get the ATR Soft Breakeven SL move (the useful part).

### Disabled (Feature Flags — code preserved, re-enable via env var)
- **Maker Limit Grid** (`MAKER_GRID_ENABLED=false`): Disabled after margin freeze analysis (v0.14.6). All grid code preserved, skipped at runtime. Re-enable: `MAKER_GRID_ENABLED=true`.
- **Ghost Recovery** (`GHOST_RECOVERY_ENABLED=false`): Disabled — no evidence of benefit in 472 closed positions. Risk of doubling losses on real reversals. Re-enable: `GHOST_RECOVERY_ENABLED=true` after ML validation.

### Technical
- Version bump: `v0.14.4 → v0.15.0` ("Smart Execution — Apollo Audit")
- Added `import os` for env var parsing in position_tracker.py module-level constants
- New close reason types: `apollo_full_exit`, `zone_full_exit`, `e_pnl_full_exit`
- New audit triggers: `r_multiple_2r_sl`, `r_multiple_3r_sl` (SL lock fallbacks)

---

## [0.14.6] - 2026-03-29
### Changed
- **HOTFIX: Maker Grid Anti-Spam (Margin Leak Fix)**: Eliminated an architectural replication bug in `position_tracker.py` where the engine duplicated the 30% Maker Limit commands for 2R/3R targets. Implemented an `age_hours < 0.1` birth-filter constraint to completely block engine amnesia span on system restarts, averting the re-sending of grid commands for legacy database positions. 
- **HOTFIX: Bybit Margin Locking Fix**: Added explicit `reduce_only: True` payload transmission mapping for the `place_limit_tp` Webhook JSON object to ensure the Trading Bot parses closing-only orders mathematically according to Bybit One-Way mode specifications, instantly remedying the 75% synthetic account margin drain freeze.

---

## [0.14.5] - 2026-03-29
### Changed
- **Graceful Reversal Protocol**: Architecturally removed the blunt `State Conflict Guard` that previously rejected any new opposing signals outright. The Risk Engine now accommodates valid trend reversals on Bybit One-Way Mode architectures.
- **Anti-Whipsaw Cooldown**: Added a temporal 4-hour filter for opposing signals. Reversals occurring within 4 hours of the active position are rejected as `whipsaw_protection` (consolidation noise), effectively neutralizing Midas' ranging market bleed while retaining macro trend reversals.
- **Pre-netting Database Synchronization**: Upon approving a valid reversal, the Risk Engine proactively updates the existing position state to `closed` (Reason: `trend_reversal`) *before* issuing the webhook approval, ensuring real-time alignment with Bybit's forced netting mechanism without phantom tracking anomalies.

---

## [0.14.4] - 2026-03-29
### Added
- **State Conflict Guard (Anti-Saw)**: Implemented strict state validation in `main.py` before approving new signals. For Bybit One-Way Mode, if an active trade already exists for a symbol, the Risk Engine aggressively REJECTS new signals (`conflict_with_active_trade`). This completely eliminates Phantom Sync bugs, prevents double-commissions in sideways chop, and stops opposing signals from netting out.
### Changed
- **Apollo Bail Strict Profit Rule**: Rewrote `Apollo Fractional Bailing (Step 1)` trigger condition from `current_pnl_pct > -1.0` to `current_pnl_pct > 0.0`. Apollo will no longer execute partial closes at a loss (which mathematically destroyed the R:R by locking in a loss and leaving the remainder vulnerable). "Stale" losing trades are now immune to Apollo and will be managed strictly by their Stop-Loss or Orderbook Shielding.

---

## [0.14.2] - 2026-03-28
### Added
- **Asymmetric Maker Limits (Grid Execution)**: Transitioned from 60s chron-based polling management to real-time execution by instantly dispatching 2R and 3R Limit Orders to the bot (`place_limit_tp` command) upon position confirmation. Captures milisecond volatility spikes.
- **Ghost Recovery (Auto-ReEntry)**: Implemented an AI 100% win-rate historical edge. If a position is stopped out near Break-Even (-1% to +1% PnL) but the underlying Midas `multi_tf_trends_json` (1H/4H) remains valid, the Engine fires a Ghost Webhook to instantly reopen the trade, preventing Whipsaws from stealing 84% targets.
- **Death of Static Break-Even (ATR for TP1)**: Deprecated the fatal `entry * 1.002` rigid BE buffer. TP1 Partial Closes now shield the remaining trade using a dynamically bounded `ATR(4h) * 1.5` offset, explicitly surviving MM liquidity grabs and stop-hunts.

---

## [0.14.1] - 2026-03-28
### Added
- **Observe-Only Mode (Bot Verification Phase)**: Solved the "6000+ API spam" desync loop caused by Midas Trading Bot's KillSwitch rejection. The Position Tracker (Думалка) now queries the bot's `/dumalka/positions` endpoint at the start of each cycle. If a position exists in the RE database but not on the real bot, it enters `_observe_only` mode. 
- **Paper Trading Engine**: Unconfirmed `_observe_only` positions continue to be tracked for PnL, snapshots, and ML data collection (as paper trades), but ALL bot command gates (TP1 partial close, SL breakeven, Apollo, Zone Policy) are strictly bypassed.

---

## [0.14.0] - 2026-03-28
### Added
- **Young Position Grace Period**: Implemented a 2-hour grace period (`YOUNG_POSITION_HOURS = 2`) for newly opened positions. During this window, Apollo Protocol and Zone Policy treaments are completely disabled. This allows Midas's original trading thesis time to unfold without premature AI interference. Exchange hard SL and TP are still honored.
- **R-Multiple Exit Logic**: Integrated risk-reward scaling. Positions now use the `original_sl` (the underlying Midas SL) to calculate 2R and 3R milestones.
  - **2R HIT**: 20% partial close.
  - **3R HIT**: SL moved to 1R (guaranteeing 1 unit of profit).
- **Critical Failure Escalation**: Added `CMD_FAIL_ALERT_THRESHOLD` (5). If Dumalka fails to execute a command on the bot 5 times consecutively, it fires a CRITICAL Telegram alert.

---

## [0.13.1] - 2026-03-27
### Added
- **ML Infrastructure (Phase 5 Pre-ML)**:
  - `label_optimal_actions.py` ported from SQLite to PostgreSQL (67,555 snapshots labeled: hold=59,442, close=7,275, partial_close=810)
  - `export_ml_dataset.py` — PG→CSV export (67,527 rows × 23 columns, 10 core + 3 engineered + 4 conditional features)
  - `train_exit_model.py` — XGBoost baseline (hold accuracy 92.7%, top feature: zone gain=662.8)
  - `core/trading_env.py` — Gym-compatible RL environment (232 episodes, 10-feature state, 4 actions)
  - Model artifacts: `xgboost_exit_v1.json` (2.4MB), `feature_importances.csv`, `ml_dataset_v1.csv` (9.7MB)
- **Auto-Phantom-Close (Desync Resolution)**: Per-position phantom counter (`_no_trade_count`) with threshold=3. After 3× consecutive "no active trade" / "position not found" from bot → position auto-pruned as `phantom_sync`. Counter resets on successful command. `_pos_id` injected into all 9 `send_command_to_bot` call sites for accurate per-position tracking.
- **ATR-Adaptive Apollo SOFT BE**: Replaced static -0.8% SOFT BE offset with volatility-adaptive formula: `offset = clamp(ATR(4h) × 1.5, 0.8%, 5.0%)`. BTC ≈0.96% (unchanged), RIVER ≈5.0% (4× wider, capped). Uses `annualized_vol` already available from `fetch_market_data()`. Prevents premature stop-outs on high-volatility memecoins while maintaining tight protection for stable assets.
- **Dmitry Integration Complete**: Bot deployed `POST /trade-outcome` (HTTP callback with `X-Webhook-Secret` auth) and `GET /dumalka/positions` (real-time position sync with `X-Dumalka-Token` auth). All 6 API field discrepancies resolved. Supported events: `open`, `tp1_hit`, `tp2_hit`, `tp3_hit`, `sl_hit`, `partial_close`, `full_close`. Connectivity tested 200 OK on both endpoints.

### Changed
- `position_tracker.py` LOC: 1556 → 1645 (auto-phantom + ATR + MC zone floor + startup logging)
- Apollo SOFT BE offset now computed dynamically per-asset (was hardcoded 0.008)
- Module docstring updated to v0.13.1 with integration status
- **MC Zone Floor Fix**: Differential MC result now floored by zone's `base_close_frac` — MC can increase fraction but never reduce below zone minimum. Fixes edge case where MC=0% swallowed zone TRIGGERED action.
- **Ghost Tracker KPI Cards**: `/api/opportunity-cost` now returns aggregate `summary` (total_trades, avg/max MFE/MAE 1h/4h, net_opportunity). Widget upgraded with 6 KPI cards.
- **Professional Startup Logging**: All 9 background modules now announce startup with config details (position_tracker, analytics_precompute, bybit_pnl_refresh, gpu_sampler, shadow_pnl, watchlist, accumulation, sentinel). Added startup banner.
- **GPU Sampler Resilience**: `sample_gpu_load` now wrapped in try/except — CUDA errors no longer kill the sampler silently.
- **Market Data Resilience**: Added Binance.US Spot fallback for orderbook/klines and OKX Public API fallback for funding rate/OI. ML feature fill rate: funding 5%→100%, spread 5%→95%+, multi_tf 5%→95%+. `_BINANCE_EXCLUDED` set for Bybit-only symbols, `_to_okx_inst_id()` mapper for OKX format.
- **ML Intelligence Layer (v0.13.2)**: 6 new ML features for XGBoost exit model — `btc_change_1h` (Binance.US klines; BTC-altcoin correlation), `rsi_14` (Wilder's smoothing; overbought/oversold detection), `orderbook_imbalance` (bid/ask volume ratio; buy/sell pressure), `long_short_ratio` (OKX public API; crowding indicator), `funding_rate_change` (engineered; momentum of crowding), `market_regime` (already computed, now exported). Total ML features: 10→17 core, 3→4 engineered, 4→4 conditional, +5 intelligence. Dataset expanded from 23 to 29 columns.

### Fixed
- Phantom positions no longer accumulate indefinitely — auto-pruned after 3× bot rejection
- High-volatility assets (RIVER, memecoins) no longer stopped out on normal price noise by Apollo SOFT BE
- **position_tracker crash on startup**: Removed dead `from position_manager import ...` at L723 that crashed position_tracker after `position_manager.py` was archived
- **Dead TG notification block**: Removed disabled Telegram notification at L2287 (duplicate of telegram_bridge.py, disabled since v0.9.1)

### Removed
- `position_manager.py` (425 LOC) → archived to `src/_archive/` (duplicate of position_tracker decision pipeline)
- 5 `*_v2_prototype.py` files (774 LOC) → archived to `src/_archive/` (design notes, not production code)
- Architectural ideas preserved in `IDEAS_FROM_PROTOTYPES.md`

---

## [0.14.2] - 2026-03-26
### Added
- **Repeat Signal Persistence Boost**: Post-scoring +0.03 boost for same symbol+side signals within 6h. Data-backed: repeat <6h WR=52% vs fresh 41% (408 trades). Auto-upgrades `reduce→approve` if boosted score ≥ 0.60.
- **Shadow PnL Tracker**: Background loop (`shadow_pnl_backfill_loop`) tracks what would have happened to ALL signals (rejected, reduced, approved). Two-phase: (1) price snapshots at 1h/4h, (2) SL/TP klines simulation using signal's own parameters. DB columns: `shadow_pnl_1h`, `shadow_pnl_4h`, `shadow_outcome`, `shadow_checked_at`.
- **Scoring Quality Endpoint** (`/api/scoring-quality`): Confusion Matrix (True/False Approve/Reject), Calibration Curve (score bucket → win rate), Threshold Optimizer (F1-maximizing cutoff), PnL timing analysis (1h vs 4h correlation).
- **Scoring Quality UI Widget**: Premium analytics widget with confusion matrix KPIs, calibration chart, outcome doughnut, and threshold optimization table.
- **Structured Trace Context (Observability)**: `TraceContextFilter` injects `trace_id`, `symbol`, `duration_ms` as structured JSON fields. `LoggerAdapter` propagates trace context through webhook lifecycle. Query: `jq 'select(.trace_id=="X")' logs/app.log`.
- **Error Rate Middleware**: HTTP middleware tracks 2xx/4xx/5xx counts, slow requests (>5s), last 50 errors with timestamps. Exposed via `/health` → `operational` section.
- **Request Correlation**: `X-Request-ID` header (UUID4[:8]) on every API response for client-side correlation.
- **Per-Phase Timing**: `duration_ms` logged separately for market_fetch, scoring, and Monte Carlo phases.
- **Business & Architecture Audit**: Comprehensive professional assessment of system quality, financial instruments, and market coverage.
- **Dmitry Integration Document**: Detailed API spec for `GET /positions` + `POST /trade-outcome` to fix RE↔Bot desynchronization.

### Changed
- **Noise Suppression**: `httpx`, `httpcore`, `uvicorn.access` loggers set to WARNING level (-70% log volume).
- **Blacklists Disabled**: `SYMBOL_BLACKLIST` and `UNPROFITABLE_HOURS_UTC` cleared to maximize data collection for ML training phase.
- **Phantom Cleanup**: Purged 5 phantom positions (RIVER×2, AVAAI, CUSDT, ASTER) from `open_positions`.
- **Enhanced /health**: Added `uptime_hours`, `error_rate_pct`, `status_counts`, `slow_requests_5s`, `log_size_mb` to health endpoint.
- **Documentation Audit**: Updated `ARCHITECTURE.md`, `PROJECT_RISK_ENGINE_FULL.md`, `DUMALKA_NEXT_STEPS.md`, `CHANGELOG.md`, and module docstrings to v0.14.2.

### Fixed
- **Config Graceful Handling**: Empty `SYMBOL_BLACKLIST` and `UNPROFITABLE_HOURS_UTC` strings no longer crash on startup.

---

## [0.12.0] - 2026-03-25
### Added
- **Portfolio Heat Check (Correlation Shield)**: Integrated `gpu_correlation_matrix()` into execution loop. Average BTC correlation > 80% → tighten all zone thresholds by 30%.
- **Dynamic Moonbag (Zone 4)**: Remaining position scales 5-30% based on MC confidence (`p_tp * 0.40`) instead of fixed 10%.
- **10-Tier Differential MC**: Expanded GPU optimizer from 4 coarse fractions to 10 fine-grained options `[0.0, 0.15, 0.25, ..., 0.95, 1.0]`.
- **Regime-Aware Threshold Caps**: Max adaptive multiplier now regime-dependent — trending: 1.5×, normal: 1.2×, ranging: 0.9×.
- **Compound Growth Engine (Shadow)**: `compound_growth.py` — dynamic position sizing with drawdown circuit breaker, ramp-up phase, and kill switch. Running in shadow/log-only mode.
- **Scoring v2 (Strategy Pattern)**: `scoring_v2.py` with `HeuristicScorer` and future `MLScorer` plugin support. Now the ACTIVE scorer (replaces `scoring.py`).

### Fixed
- **SL Breakeven Buffer**: SL breakeven now includes +0.2% profit margin to prevent premature stops from spread.

---

## [0.10.1] - 2026-03-24
### Added
- **Binance Sentinel**: Flash crash monitor via Binance Futures WebSocket. ALERT (≥0.5% drop/5s), EMERGENCY (≥1.0% drop/5s). Auto-reconnect, 60s heartbeat, `/health` integration.
- **DB Connection Hardening**: `db_adapter.py` — autocommit reads, health checks on borrow, auto-rollback on return. PG timeouts: `statement_timeout=30s`, `lock_timeout=5s`, `idle_in_transaction=60s`.
- **PostgreSQL Monitoring**: `pg_stat_statements` enabled, `track_io_timing=on`, `track_functions=all`.

### Changed
- **8 New Indexes**: 2 BRIN + 3 composite + 3 partial indexes on `position_snapshots` + `open_positions`. Total: 21 indexes.
- **DB Best Practices Section**: Added comprehensive PostgreSQL rules to `ARCHITECTURE.md`.

---

## [0.10.0] - 2026-03-24
### Added
- **Orderbook Shielding (Apollo Step 2)**: Dynamic SL behind orderbook walls (≥5× median volume). Falls back to static -0.8% if no wall found.
- **Yield-Aware Apollo**: Funding Rate adjusts TIME_DECAY_END_H — favorable carry → 6h window, adverse carry → 2h window.
- **Accumulation Scanner**: Background module tracking Open Interest, Volume, Funding, and Price Compression to detect "Coiled Spring" pump/dump setups. Includes Telegram digest at 00:00/12:00 UTC and `accumulation_snapshots` tracking table.
- **Analytics Accumulation Widget**: "Радар Монет" (Accumulation Radar) on `/analytics` page. Real-time scanning UI. *(Fixed state persistence by adding cache-busting `?_cb=` to `fetch`, and added multilingual explanation tooltips).*
- **Binance Sentinel**: Flash crash monitor via Binance Futures WebSocket. Two alert levels: ALERT (0.5%/5s) and EMERGENCY (1.0%/5s) with TG notifications. Auto-reconnect, heartbeat, /health status.
- **DB Optimization**: 8 indexes (BRIN + composite + partial) on `position_snapshots` and `open_positions`. `create_weekly_partitions()` function for future 1M+ scalability.
- **`fetch_orderbook_raw()`**: Raw bid/ask arrays for wall detection in `bybit.py`.
- **Strategic Research §7** in `DUMALKA_NEXT_STEPS.md`: 384-trade analysis, Score↔WR verification (no correlation → Score-based sizing cancelled).

---

## [0.9.7] - 2026-03-24
### Fixed
- **FIX-1: Circuit Breaker False Triggers**: Bot execution errors (`partial_close failed`, `move_sl failed`) no longer trip the Circuit Breaker. These are Bybit API rejections, not connectivity issues. Previously, they blocked ALL commands (including critical SL moves) for 5 minutes.
- **FIX-2: Apollo Retry Spam**: Introduced `_apollo_attempted` idempotency set. Apollo now attempts each position exactly ONCE per session. Failed attempts no longer cause 30+ retries per hour.
- **FIX-3: Time-Decay Gate**: Relaxed from `< 0.01` to `< 0.70` to allow Apollo to fire even after TP1 partial closes.

### Added
- **Observability**: `✅ [APOLLO OK]`, `❌ [APOLLO FAIL]`, `⏳ [APOLLO SKIP]` log markers for full post-mortem traceability.

---

## [0.9.6] - 2026-03-24
### Added
- **Apollo Protocol (Time-Decay v2.0)**: Replaced strict Time-Decay Breakeven SL with a hybrid strategy to solve the "Missed Rockets" problem (where positions pump 15%+ immediately after getting stopped out at entry).
- **Fractional Bailing**: When a position stagnates for 1-4 hours, the engine now immediately drops 70% of the position by Market Order to lock in capital safety.
- **Soft Breakeven (-0.8%)**: The remaining 30% "Moonbag" is given a wider Soft Breakeven (-0.8% instead of 0.0%) to survive standard market-maker stop hunts before the rocket takes off.

---

## [0.9.5] - 2026-03-24
### Added
- **Ghost Tracker (Opportunity Cost)**: Implemented `trade_opportunity_cost` database schema and a background worker (`core/opportunity_cost.py`) to scrape historic K-lines after a position closes.
- **Smart API Balancing**: Ghost Tracker prioritizes the free `api.binance.us` K-line API with a fallback to the Bybit Proxy Tunnel to aggressively conserve Bybit rate limits.
- **UI Widget (Ghost Tracker)**: Added a "Godlike" visualization to `analytics.html`. Tracks MFE (Missed Profit) vs MAE (Saved Drawdown) 4 hours after closure.
- **Hall of Fame/Shame**: Implemented "Missed Rockets" and "Saved from Abyss" tables to easily identify trades that pumped or dumped immediately after Risk Engine triggered an exit.

---

## [0.9.4] - 2026-03-24
### Added
- **Observability Audit & Fixes**: PostgreSQL migration for `daily_report.py` (switched from broken SQLite).
- **JSON Serialization Fix**: `NpEncoder` implemented to properly record `numpy.float64` metrics (VaR, P_sl) in the Audit Log, preventing silent `json.dumps` failures.
- **Task Heartbeats**: Opened `/health` staleness checks for background tasks (`position_tracker`, `watchlist_scanner`, `analytics_precompute`).
- **Думалка Audit**: Added `MOVE_SL` and `PARTIAL_CLOSE` statistics to the morning report.

### Fixed
- **Bare Exceptions Fix**: Swept through codebase to remove `except:` without variables (e.g., `position_tracker.py` timeout logic) to prevent hiding critical system bugs.
- **Silent Exceptions Fix**: 15+ `except: pass` blocks in `main.py` replaced with structured `logger.debug/warning` for analytics observability.
- **DB Tracing**: `db_adapter.py` now logs full SQL queries and params on error, with slow-query detection (>1.0s).
- **TG Spam Fix**: Added a 15-minute retry cooldown for `move_sl` attempts.

---

## [0.9.3] - 2026-03-23
### Added
- **Time-Decay Exits (CHECK 3.5)**: Data-driven "danger zone" detection. Trades stuck 1-4h without TP1 progress get forced SL→breakeven. Based on analysis of 373 closed trades showing 1-4h bucket has 20.2% Win Rate and -$113.6 cumulative loss, vs 65.7% WR for <1h and 63.8% WR for 12-24h.
- **Per-Symbol Circuit Breaker**: Errors on GRASSUSDT no longer disable WIFUSDT. Each symbol has an isolated failure counter.
- **Smart Error Classification**: Non-retriable errors ("no active trade", "position not found") don't increment the circuit breaker counter.
- **Auto-Cooldown (5 min)**: Circuit breaker auto-resets after 300s, allowing automatic recovery without manual restart.
- **Mode-Aware Telegram Digest**: Actions now labeled ✅ ACTIVE / 👻 SHADOW / ⚡ BREAKER in hourly digest.

### Fixed
- **Critical Bug**: Global circuit breaker contamination — one symbol's "no active trade" errors were disabling Dumalka for ALL symbols permanently until restart.

---

## [0.9.2] - 2026-03-23
### Added
- **Contra-Trend Alerts**: Watchlist Scanner now detects price anomalies against the trend (>3% pump in bearish trend, or dump in bullish).
- **Multi-Factor Confidence Scoring**: Alerts are scored (0-100%) based on Trend Alignment, Volume Context, Funding Alignment, OI Decline, and Magnitude.
- **Event Lifecycle Tracking**: Replaced simple cooldowns with a robust `_active_events` dict to track one alert per unique pump/dump event until normalization.
- **Dump→LONG Hypothesis**: Began active data collection for mean-reversion analysis on dumps (currently 18 events evaluated).

### Fixed
- **Analytics Error**: Fixed a type mismatch (str → int) in `alert_sent` that was causing 500 errors in the UI.
- **Post-Mortem Parsing**: Fixed float parsing errors for price fields in `backfill_post_mortem`.

### Changed
- Telegram alerts for Contra-Trend events are now only sent for MED (≥50%) or HIGH (≥70%) confidence scores to prevent spam.

---

## [0.9.1] - 2026-03-23
### Added
- **Hourly Digest Buffer**: Routine actions from Думалка (like `MOVE_SL` or `partial_close`) are now buffered (max 500 entries) and sent to Telegram as a clean hourly digest.
- **Direct Bot API**: Critical actions (like `full_close` or `portfolio_override`) trigger immediate Telegram alerts using the direct Bot API.

### Changed
- **Anti-Spam**: SL breakeven deduplication — skips sending `MOVE_SL` command if the `stop_loss` is already at the `entry_price`.

---

## [0.9.0] - 2026-03-22
### Added
- **ML Readiness**: Enriched `position_snapshots` with 5 new columns: funding, oi_change, spread, trends, and regime.
- **Database Tracking**: In-memory tracking established for Open Interest (OI) changes.
- **Labeling Tool**: Introduced `label_optimal_actions.py` which successfully labeled 58,744 (97.8%) historical snapshots for future ML training.
- **Scout Filter**: Added a `ranging` market filter to `watchlist_scanner.py`.

### Changed
- **PostgreSQL Migration**: Migrated the core database from SQLite to PostgreSQL v18, utilizing `psycopg2`, `asyncio.to_thread`, and Unix sockets for high performance.
- **Dynamic Sizing**: Replaced hardcoded balance logic with real wallet balance polling for GPU Monte Carlo Risk ($22.63 base).

### Fixed
- **System Audit**: Fixed a persistence bug where `_closed_fractions` was not saving to the DB.
- **System Audit**: Fixed TP1 logic so it immediately commands a `partial_close` and `move_sl` to the bot (Active Mode).
- **System Audit**: Implemented Circuit Breaker disconnecting Active Mode after 5 consecutive errors.

---

## [0.8.2] - 2026-03-22
### Added
- **Active Mode Rollout**: Думалка transitioned from shadow mode to `active` mode, sending real operational commands (`partial_close`, `sl_breakeven`) to the Trading Bot.
- **Leakage Metrics**: Added `leaked_count`, `leaked_pnl_total`, `potential_saved`, and `capture_ratio` to the `/api/analytics` endpoint.
- **UI Element**: Added "Dumalka Profit Leakage Tracking" widget inside Analytics dashboard.

### Fixed
- **Profit Leakage (RC-1..RC-6)**: Fixed bugs preventing proper profit capturing, including `UnboundLocalError`, flawed Monte Carlo forward projections, missing fallback take-profits, and lack of idempotency.

---

## [0.8.1] - 2026-03-22
### Added
- **Watchlist Scanner**: Implemented background market scanner analyzing 8 key symbols every 5 minutes independent of Midas signals.
- **Dump/Pump Detection**: Automatic detection of >2.5% shifts over 1h or >3.75% over 4h.
- **Post-Mortem Tracking**: Scanner saves `would_have_pnl_pct` across 1h/4h horizons via `watchlist_alerts` for future ML dataset generation.

---

## [0.8.0] - 2026-03-21
### Added
- **Observability**: Standardized `python-json-logger` for structured logging across RE and Trading Bot.
- **Log Rotation**: Deployed `RotatingFileHandler` with 100MB × 30 file deep history handling.
- **Financial Audit Log**: Introduced `dumalka_audit_log` inside the DB tracking all shadow/active management decisions.
- **Traceability**: Extended `signal_hash` to act as a universal `trace_id` connecting the RE and Trading Bot.
- **Backups**: Created `backup_db.py` to run daily cron backups of the database straight to Telegram.

---

## [0.7.1] - 2026-03-20
### Added
- **RE Effectiveness Report**: Added script to validate performance.
- **Data Quality Penalty**: Scoring mechanism penalizes missing metadata (`win_rate`, `probability`, `risk_reward`) by -15% per missing point, floor at 55%.

---

## [0.7.0] - 2026-03-19
### Added
- **Market Regime Detector**: Automatically defines current market context (trending, ranging, volatile, low_liquidity) influencing signal evaluation.
- **Backtest Module**: Created GPU CuPy engine scaling over 19 coins and 40 days to benchmark logic configurations.
- **In-Memory Tracking**: Enabled Open Interest and Funding adjustments directly into active features.

---

## [0.6.0] - 2026-03-15
### Added
- **Portfolio Allocator**: Integrated sector exposure limits ensuring no single category exceeds 50% capital allocation.
- **Multi-TF EMA Confluence**: Signal confidence boosted dynamically when 15m, 1h, and 4h EMA trends agree.
- **Liquidity Check**: Dynamic adjustment of `liquidity_ok` metric using real-time orderbook depth proxies.

---

## [0.1.0 - 0.5.0] - Historical Phase
### Added
- **Core Engine**: Monte Carlo VaR/CVaR processing via NVIDIA Titan V FP64 instance running 100K+ concurrent jump-diffusion scenarios.
- **API Server**: FastAPI setup with endpoints `/webhook`, `/evaluate`, and health checks.
- **TradingView Integration**: Initial parsing capabilities for incoming Telegram signals.
- **Dashboard UI**: Vanilla JS + Chart.js layout analyzing scores and basic risk predictions.
- **Shadow Position Tracker**: Early form of Думалка evaluating zone policies strictly internally without emitting execution commands.

---
*Generated by Risk Engine System on 2026-03-23*
