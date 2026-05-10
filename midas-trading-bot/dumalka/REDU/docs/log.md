# Risk Engine — Decision Log

> Append-only journal of significant decisions with rationale.
> Newest entries at the top. Each entry: date, decision, reasoning, outcome/status.

---

## 2026-04-12 — Audit Log Complete Coverage (v0.19.9)

**Decision**: Fix two code paths in `main.py` where `position_closed` was never written to `dumalka_audit_log`, bringing coverage from ~81% to 100%.
**Reasoning**: After the API-first bot migration (v0.19.8), close events arriving via `POST /trade-outcome` (bot-initiated: `sl_hit`, `dumalka_close`, `flip_close`, etc.) and via `POST /tv-webhook` reversal/re-entry protocol both updated `open_positions` correctly but silently skipped the audit write. Root cause: these were new paths added in v0.19.8 after the audit infrastructure was built in v0.18.6 — no regression, but an omission. Additionally, reversal closes didn't persist `realized_pnl_pct`. Backfilled 20 historical entries. Cleaned 56 orphan audit rows from deleted test positions.
**Status**: ✅ 103/103 closed positions (post-v0.18.6) have `position_closed` audit. Three queryable sources: `position_tracker`, `bot_api`, `webhook_reversal`.

## 2026-04-12 — Full Integration Verified: RE v0.19.8 ↔ Bot v0.10.3

**Decision**: Complete 3-phase integration testing (Phase 1: initial discovery, Phase 2: RE stress tests, Phase 3: bot-initiated tests) and resolve all findings before production trading.
**Reasoning**: After API-first migration, systematic verification revealed 2 critical issues: (1) `WEBHOOK_SECRET` was empty → `/tv-webhook` accepted unauthenticated requests (fixed: generated secure token, added startup CRITICAL log), (2) `reduce` signals returned `approved=false` blocking profitable trades (shadow data N=155: reduce avg PnL +1.82% vs approve +0.30%) → changed to `approved=true` with 0.7x conviction multiplier. Bot developer fixed hash linking in trade-outcomes during testing (round 3: `linked_to_signal=true`). 100+ test requests across all endpoints, all verified. See `docs/INCIDENT_REPORT_2026_04_12.md`.
**Status**: ✅ Integration complete. Both sides ready for production.

## 2026-04-11 — API-first Bot Integration (v0.19.8)

**Decision**: Migrate all Bot↔RE machine-to-machine communication from Telegram text parsing to pure HTTP API. Telegram retained exclusively for human-facing observability.
**Reasoning**: Telegram text parsing is fragile, untyped, and lacks retry semantics. Three critical gaps identified in initial plan: (1) dedup guard rejected `source=bot_direct` retries, (2) `rejection_reason` was silently dropped by Pydantic `extra="ignore"`, (3) `/trade-outcome` had no `event:open` upsert for exchange fill price sync. All fixed before deployment. `docs/BOT_API_CONTRACT.md` created as authoritative spec for bot developer (Phase 2 migration).
**Status**: RE side deployed. Bot migration completed (v0.10.3).

## 2026-04-11 — Health Watchdog (v0.19.7)

**Decision**: Implement async health watchdog (`health_watchdog.py`) monitoring 9 system components + bot connectivity, with Telegram alerts on 7-minute silence and 30-minute cooldown.
**Reasoning**: Production system had no automated alerting for component failures. Manual discovery of issues (GPU OOM, DB pool exhaustion, bot disconnect) caused untracked downtime and missed position management cycles.
**Status**: Deployed. All 9 components monitored. Recovery notifications confirmed working.

## 2026-04-10 — GitHub profile + README update (v0.19.6.1)

**Decision**: Full README rewrite + GitHub repo description/topics update.
**Reasoning**: README was frozen at v0.19.2 — missing 4 minor versions of features (Rocket Catcher, Scout, ML Shadow, ATR-adaptive BE, volatility classification, close-event sync). Performance metrics were 6 weeks stale (366 signals → 700+ positions, 60K → 103K snapshots). GitHub repo had empty description and no topics. Updated: version badge, performance table, features section (v0.19.x), project structure (scout.py, kline_collector.py, models/, docs/), tech stack (+scikit-learn, Optuna, WebSocket), roadmap (Phase 2 checkpoint), documentation links, config reference.
**Status**: Deployed. 8 topics added: trading, crypto, risk-management, monte-carlo, machine-learning, fastapi, gpu, python.

## 2026-04-10 — Close-event handling fix (v0.19.6.1)

**Decision**: Fix 3 bugs in close-event processing: VALID_EVENTS gate, /trade-outcome endpoint, PR merge.
**Reasoning**: Dmitry's PR (20f6db3) added new close events (dumalka_close, manual_close, flip_close) to handler at line 502 but missed the VALID_EVENTS gate at line 87 that rejects unknown events at parse stage. Additionally, /trade-outcome HTTP endpoint had no close handler at all. DB evidence: 14 manual_close events resulted in wrong close_reason (bot_sync_desync/phantom_sl_hit) with delays of 5-249 minutes. Fix adds events to VALID_EVENTS, adds close handler to HTTP endpoint, includes 20 tests (17 unit + 3 integration). Tech debt: 16 analytics SQL queries still use hardcoded event lists — deferred (analytics-only).
**Status**: Deployed and verified. 40/40 tests passed. Live integration test confirmed: POST manual_close -> position closed correctly.
**TD1 closed 2026-04-12**: All 14 analytics SQL queries updated to include `dumalka_close`, `manual_close`, `flip_close`. Measured impact: +22 trades now included, avg PnL corrected from -0.25% to +0.04% (377 vs 355 close events).

## 2026-04-09 — Scout spike threshold 3.0 to 2.5

**Decision**: Lower `_SPIKE_ATR_MULT` from 3.0 to 2.5 in `scout.py`.
**Reasoning**: Backtest on 34 symbols (548+ 1h candles) showed 3.0 catches "burned out" impulses (32 signals, WR 34%, avg PnL -0.5%), while 2.5 catches real second waves (47 signals, WR 36%, avg PnL +2.1%). SIREN rocket Apr 4 (spike=2.76x, +107% in 4h) missed at 3.0, caught at 2.5. Threshold 2.0 rejected (76 signals but avg PnL +1.4% — more noise).
**Status**: Deployed. Monitoring checkpoint April 21: revert if avg_pnl_1h < -1% at N >= 5.

## 2026-04-09 — Scout accumulation_score bugfix

**Decision**: Fix `composite_score` → `accumulation_score` column reference in `scout.py`.
**Reasoning**: Silent exception hid the error; scout feature was always NULL for `accumulation_score`. Column in `accumulation_snapshots` is `accumulation_score`, not `composite_score`.
**Status**: Fixed and deployed.

## 2026-04-08 — ML Shadow Mode (v0.19.6)

**Decision**: Deploy ExtraTrees+Optuna model in shadow mode — log predictions, no trade impact.
**Reasoning**: After EXP-1 through EXP-7 (12+ models tested), ExtraTrees+Optuna emerged as champion (LOO AUC 0.574-0.621). At 0.80 confidence threshold: 100% win rate on 9 in-sample trades. N=98 too small for neural nets (need 300-1000+). Shadow mode provides out-of-sample validation at zero risk.
**Status**: Live. First prediction: SIREN #648 SHORT, prob=0.680 (HIGH CONFIDENCE). Evaluate at N >= 30 predictions.

## 2026-04-08 — Duplicate ML prediction prevention

**Decision**: Add DB dedup check before prediction insert + seed in-memory set from DB on startup.
**Reasoning**: Service restarts caused duplicate predictions for already-predicted positions. In-memory `_ml_shadow_logged` set alone is insufficient across restarts.
**Status**: Fixed and deployed.

## 2026-04-07 — Enriched audit diagnostics (v0.19.5)

**Decision**: Log volatility, adaptive_factor, vol_modifier, regime_dd, heat, zone, base_thresh in mc_diagnostics for zone_full_exit, e_pnl_full_exit, sl_breakeven events.
**Reasoning**: "Triple compression" (af=0.5 x vol_mod=0.8 x regime) reduces Zone 3 threshold from 15% to 5-7% on high-vol assets. Without these fields in audit log, root cause analysis required joining position_snapshots — slow and error-prone.
**Status**: Deployed. Data accumulating for Phase 2 (April 21 checkpoint).

## 2026-04-07 — Deep kline backfill (v0.19.5)

**Decision**: One-time backfill of 500 1h + 200 4h candles per symbol (41 symbols).
**Reasoning**: Scout needs >= 55 candles for signal generation. Young tokens (SIREN, NOM, RIVER) had insufficient depth. Without backfill, spike_consolidation_breakout could never trigger for the most interesting assets.
**Status**: Completed. Scout now generates signals for SIREN, NOM, RIVER, VVV.

## 2026-04-07 — 3-tier volatility classification (analysis only)

**Decision**: Classify assets into DEFINITE HIGH (vol >= 4.0), TRANSITIONAL (2.0-4.0), NORMAL (< 2.0). Do NOT change thresholds yet — wait for data.
**Reasoning**: RIVER/CUSDT perform WORSE during high-vol phases (contrary to intuition). Only SIREN/NOM/STO are truly high-vol and benefit from different treatment. Phase 2 threshold changes apply ONLY to vol >= 4.0, pending N >= 10 zone exits by April 21.
**Status**: Documented as H1. Monitoring in progress.

## 2026-04-07 — H3 correction: RIVER LONG unprofitable (not SHORT)

**Decision**: Correct H3 from "RIVER SHORT unprofitable" to "RIVER LONG unprofitable".
**Reasoning**: Re-verification showed RIVER LONG: avg -1.57%, 0/3 winners. RIVER SHORT: avg -0.25%, 2/4 winners (2 profitable trades lost to HSC/early exit). Previous analysis had sides swapped.
**Status**: Documented. No code changes — purely analytical finding for future signal filtering.

## 2026-04-05 — ATR-adaptive TP1 Soft BE (v0.19.4)

**Decision**: Add ATR noise zone skip for TP1 Soft BE. When TP1 distance < 2x ATR(1h), keep original SL.
**Reasoning**: NOM #597 lost +7.4% potential because BE moved SL into noise zone (TP1 only 0.26% from entry, ATR was 2.1%). Validated on 7/7 historical TP1-BE cases: skip equal or better in all, +21.9% cumulative PnL recovered.
**Status**: Deployed. Config-adjustable via .env (TP1_BE_ATR_SKIP_MULT, TP1_BE_OFFSET_MULT, TP1_BE_MIN_PCT, TP1_BE_MAX_PCT).

## 2026-04-04 — Scout infrastructure + kline storage (v0.19.0-0.19.2)

**Decision**: Build autonomous shadow signal generator (8 signal types), kline historical storage (3 timeframes), and derivatives time-series collection.
**Reasoning**: System could not learn from or detect multi-wave parabolic moves (SIREN +191% in 17h). Scout provides re-entry signals, klines provide context, derivatives provide market microstructure. All shadow-mode — zero risk.
**Status**: Deployed. 34 symbols tracked, 8 signal types, 12 ML features per signal.

## 2026-04-04 — Telegram proxy fallback (v0.19.3)

**Decision**: Implement SOCKS5 proxy-first strategy with direct fallback for Telegram notifications.
**Reasoning**: Network gateway change broke Telegram connectivity. Proxy is faster and more reliable from Proxmox VM, but must not be single point of failure.
**Status**: Deployed. Zero missed notifications since.

## 2026-04-02 — MC sentinel value discovery

**Decision**: Filter p_sl=1.0/p_tp=0.1 sentinel values from all analytics queries.
**Reasoning**: MC module was writing hardcoded defaults instead of actual calculations for an extended period (pre-April 2). Including these values in analysis produces misleading correlations.
**Status**: Fixed. Sentinel filter added to analytics queries. MC confirmed operational since April 2.
