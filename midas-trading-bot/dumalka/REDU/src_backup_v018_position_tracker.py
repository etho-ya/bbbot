"""
Position Tracker — v0.18.3 "Думалка" Smart Execution

AI Position Management System with multi-level decision pipeline (every 30s):
  Level A: Orderbook Verification / Observe-Only Mode — Execution Guard (v0.14.1)
  Level B: Zone-based Trailing Take-Profit (5 zones, calibrated on 425 positions)
  Level C: MC Forward Projection — full E[PnL] + skewness (v0.18.1)
  Level D: Differential MC — 10-tier optimal close fraction (v0.12.0)
  Level E: Portfolio Risk Override (Titan V, fallback)

Key Subsystems:
  - v0.18.3: Bot roster desync auto-close — symbol absent from /dumalka/positions N cycles (2026-04-02)
  - v0.18.2: Patience Protocol — Apollo Bail disabled (shadow-only), grace period 1.0h (2026-04-02)
  - v0.18.1: E_pnl Reform "Let Winners Run" — full E[PnL] across all MC paths,
    momentum coordination, skewness override (2026-04-02)
  - v0.15.1: 30s monitoring cycle (was 60s) — 2× faster SL→BE reaction
  - v0.15.1: SL Move Cooldown 8min (was 15min) — faster retries on transient failures
  - v0.15.0: Smart Size Guard (MIN_PARTIAL_CLOSE_USD=17) — gates partial_close by position size
  - v0.15.0: Full Exit Fallbacks — apollo_full_exit, zone_full_exit, e_pnl_full_exit
  - Apollo Strict Profit Rule: Partial close permitted ONLY in profit (v0.14.4)
  - Observe-Only Engine: Paper-trades unconfirmed positions without spamming bot APIs (v0.14.1)
  - Execution Scaling: R-Multiple Logic (2R/3R) and Young Position Grace Period (v0.14.0)
  - Apollo Protocol: Fractional Bail (70%) + Orderbook Shield SL + Yield-Aware time-decay
  - ATR-Adaptive SOFT BE: Bounded ATR offset (replaces static +0.2% BE completely) (v0.14.2)
  - Auto-Phantom-Close: 3× "no active trade" → auto prune (per-position counter) — v0.13.1
  - Smart Time-Decay (CHECK 3.5): 1-4h danger zone with momentum override
  - Per-Symbol Circuit Breaker: isolation + smart error classification + 5min cooldown
  - Correlation Shield: tighten thresholds 30% if BTC corr > 80% (v0.12.0)

ML Intelligence Features (v0.13.2):
  Every 30s snapshot captures 29 ML features including:
    btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio (OKX),
    funding_rate, oi_change_pct, spread_pct, trend_sum + 10 core + 4 engineered

INTEGRATION STATUS (2026-03-29):
  ✅ Bot deployed POST /trade-outcome + GET /dumalka/positions
  ✅ move_sl success rate: 91.6% (fixed 2026-03-28, was 9.5%)
  ✅ Bybit WebSocket Price Feed: 23 symbols real-time (shadow mode)
  ✅ Auth: X-Webhook-Secret (trade-outcome) + X-Dumalka-Token (positions)
"""
import asyncio
import os
import logging
import math
import time
import json
from datetime import datetime, timezone
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, get_db_pool

from config import config
from bybit import (fetch_market_data, fetch_volume_info, fetch_closed_pnl,
    fetch_funding_and_oi, fetch_orderbook_depth, fetch_orderbook_raw,
    fetch_multi_timeframe, fetch_wallet_balance,
    fetch_btc_change_1h, fetch_rsi_from_klines, compute_orderbook_imbalance,
    fetch_long_short_ratio)
from models import Portfolio, Position, CandidateTrade, MarketData, RiskLimits, RiskRequest
from core.monte_carlo import run_monte_carlo_risk, simulate_sl_tp_probability
from db import insert_trade_outcome, insert_position_snapshot, update_snapshot_future_pnl
from regime_detector import update_regime, get_zone_dd_sensitivity, get_cached_regime
import httpx

logger = logging.getLogger("risk-engine.tracker")

# ── Configuration ────────────────────────────────────────────────────────────
POSITION_TIMEOUT_HOURS = 24  # v0.17.0: raised from 12h — 12-24h trades have 63.8% WR
CYCLE_INTERVAL_SEC = 30      # v0.15.1: 30-second monitoring cycle (was 60s) — faster SL→BE reaction

# ── v0.15.2 TP1 Normalizer: cap effective TP distance for zone calculations ──
# Problem: When Midas sends TP1=+43% or TP3=+89%, zone thresholds become useless
# because even +5% PnL = only 6% tp_progress → Zone 0 → no protection.
# Fix: Cap the TP distance used in zone math at MAX_TP_FOR_ZONES_PCT.
# This does NOT change actual TP levels or SL — only internal zone calculations.
# Example: CUSDT TP3=+89% → effective_tp_dist=10% → +1% PnL = 10% tp_progress → Zone 1
MAX_TP_FOR_ZONES_PCT = float(os.getenv("MAX_TP_FOR_ZONES_PCT", "10.0"))

GHOST_RECOVERY_ENABLED = os.getenv("GHOST_RECOVERY_ENABLED", "false").lower() == "true"

# ── v0.9.4 FIX-C: Per-position cooldown for TIME-DECAY move_sl ───────────────
# Prevents retry spam: without this, TIME-DECAY fires every 60s cycle,
# generating dozens of failed move_sl attempts if bot rejects the command.
_sl_move_last_attempt: dict[int, float] = {}  # {pos_id: timestamp}
SL_MOVE_COOLDOWN_SEC = 480  # 8 minutes between move_sl retries per position (was 15min at 60s cycle)

# v0.9.4 FIX-4: Heartbeat for background task health monitoring
# main.py reads this dict to report in /health endpoint
_heartbeat: dict[str, float] = {}

# ── Telegram Digest: buffer actions, send hourly summary ─────────────────────
_tg_action_buffer: list[dict] = []
_tg_last_digest_time: float = 0.0
DIGEST_INTERVAL_SEC = 3600  # 1 hour between digest messages

# ── v0.17.0: Keepalive — prevent bot fallback to its own TP2→BE logic ────────
# Bot falls back if no command received for DUMALKA_FALLBACK_TIMEOUT_MIN (10 min).
# We send a no-op move_sl at current SL every KEEPALIVE_INTERVAL_SEC per symbol.
_keepalive_last_sent: dict[str, float] = {}  # {symbol: timestamp}
KEEPALIVE_INTERVAL_SEC = 300  # 5 min — well within the 10 min fallback window


async def _ensure_db_columns():
    """Ensure required DB columns exist (idempotent migrations)."""
    try:
        try:
            await pg_execute("ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS zone INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug(f"zone column check/add skipped: {e}")
        try:
            await pg_execute("ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS close_reason_detailed TEXT")
        except Exception as e:
            logger.debug(f"close_reason_detailed column check/add skipped: {e}")
        try:
            await pg_execute("ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS original_sl REAL")
            await pg_execute("UPDATE open_positions SET original_sl = current_sl WHERE original_sl IS NULL")
        except Exception as e:
            logger.debug(f"original_sl column check/add skipped: {e}")
    except Exception as e:
        logger.error(f"Failed to ensure DB columns: {e}")

# ── Circuit breaker: disable active mode after N consecutive failures ────────
# v0.9.2: Per-symbol isolation — errors on GRASSUSDT won't disable WIFUSDT
# v0.9.2: Auto-cooldown — resets after BREAKER_COOLDOWN_SEC to retry
# v0.9.2: Smart error classification — non-retriable errors don't increment counter
# v0.9.7: Expanded with bot execution failures (partial_close/move_sl failed)
_consecutive_failures: dict[str, int] = {}  # {symbol: failure_count}
_breaker_tripped_at: dict[str, float] = {}  # {symbol: timestamp when breaker tripped}
MAX_CONSECUTIVE_FAILURES = 5  # After 5 failures per symbol → fallback to shadow
BREAKER_COOLDOWN_SEC = 300    # Auto-reset breaker after 5 minutes

# Errors that should NOT increment the circuit breaker counter
# These are semantic/business errors, not connectivity problems
# v0.9.7 FIX-1: Added bot-side execution failures — these are Bybit API
# rejections (min order size, invalid params), NOT connectivity issues.
# Tripping breaker on these blocks ALL subsequent commands including
# critical SL moves, creating a dangerous feedback loop.
NON_RETRIABLE_ERRORS = [
    "no active trade",        # Position doesn't exist on bot side
    "position not found",     # Bot doesn't know about this position
    "already closed",         # Position was already closed
    "insufficient balance",   # Can't execute due to balance
    "move_sl failed",         # v0.9.7: Bot couldn't move SL (e.g. invalid price)
    "failed",                 # v0.9.7: Generic bot-side execution failure
    "unauthorized",           # Auth misconfiguration, not transient
    "forbidden",              # Auth misconfiguration, not transient
]


# v0.17.0: Apollo BAIL with cooldown retry — allows retry after APOLLO_RETRY_SEC.
# Old: set() with one-shot attempt. New: dict with timestamp, retries after cooldown.
_apollo_bail_attempted: dict[int, float] = {}  # {pos_id: timestamp of last attempt}
APOLLO_RETRY_SEC = 600  # 10 minutes between Apollo bail retries

# v0.13.1: Auto-Phantom-Close — detect desync'd positions and auto-close.
# When bot replies "no active trade" / "position not found" for a given pos_id,
# increment counter. After PHANTOM_THRESHOLD consecutive replies → close as phantom_sync.
# This stops the command spam (50+ commands/day to dead positions) and keeps DB clean.
# Reset on successful command or position close.
_no_trade_count: dict[int, int] = {}  # {pos_id: consecutive "no active trade" count}
PHANTOM_THRESHOLD = 3  # Close after 3 consecutive "no active trade" from bot

# v0.14.0: Bot command failure tracking for critical alert escalation
# After CMD_FAIL_ALERT_THRESHOLD consecutive failures for same pos_id → send Telegram CRITICAL
_cmd_fail_counts: dict[str, int] = {}  # {"cmd_fail_{pos_id}": count}
CMD_FAIL_ALERT_THRESHOLD = 5  # Alert after 5 consecutive failures (was silently failing 88x)

# v0.14.1: Bot Position Verification — observe-only mode for unconfirmed positions.
# On each cycle, fetch bot's active trades. If a position's symbol is NOT in the bot's
# list, it means the bot never opened the trade (KillSwitch, insufficient margin, etc.).
# These positions stay in DB for observation (PnL tracking, snapshots, ML data) but
# NO commands are sent — prevents the "6000 failed commands" spam.
_bot_confirmed_symbols: set[str] = set()   # Symbols confirmed active on bot this cycle
_bot_sync_failed: bool = False              # True if bot sync request failed (fallback: allow all)
BOT_VERIFY_GRACE_SEC = 120                  # Don't verify positions newer than 2 min (bot needs time to open)
# v0.18.3: Consecutive cycles where bot sync succeeded but this symbol was not in roster → DB cleanup
_bot_absent_streak: dict[int, int] = {}


async def _fetch_bot_active_symbols() -> set[str]:
    """Fetch set of symbols with active trades on the trading bot.
    
    Uses /dumalka/positions endpoint. Fallback: empty set (assume all unconfirmed).
    """
    global _bot_sync_failed
    try:
        url = f"{config.DUMALKA_BOT_URL.rstrip('/')}/dumalka/positions"
        headers = {"X-Dumalka-Token": config.DUMALKA_BOT_TOKEN}
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                positions = data.get("positions", [])
                symbols = {p.get("symbol", "") for p in positions if p.get("symbol")}
                _bot_sync_failed = False
                return symbols
    except Exception as e:
        logger.debug(f"Bot position sync failed: {e}")
    _bot_sync_failed = True
    return set()

# ── v0.14.0: Professional crypto position management ────────────────────────
# Young Position Grace Period — don't interfere with Midas's thesis
# for the first N hours. Let the trade breathe. Only Hard SL Cap and
# exchange SL/TP can close during this window.
YOUNG_POSITION_HOURS = config.YOUNG_POSITION_HOURS  # v0.18.2: from config (default 1.0h, was 0.5h)

# ATR Trailing Stop multiplier by volatility class
# Lower mult = tighter trail (for stable coins where moves are small)
# Higher mult = wider trail (for microcaps where 10% drawdowns are noise)
ATR_TRAIL_MULTIPLIER = {
    "LOW": 1.5,       # BTC, HBAR: tight trailing, small moves
    "STANDARD": 2.0,  # SOL, RENDER, WIF: standard trailing
    "MID": 3.0,       # GRASS, FARTCOIN, TAO: wider breathing room
    "HIGH": 4.0,      # RIVER, CUSDT: significant vol, wide trail
    "EXTREME": 6.0,   # SIREN: extreme vol, very wide trail
}
# Trailing only activates after position proves itself (max_pnl > this %)
TRAILING_ACTIVATION_PCT = 3.0


def classify_volatility(annualized_vol: float) -> str:
    """Classify asset into volatility tier for position management.
    
    Based on empirical data from 22 symbols (March 2026):
    BTC=0.47, SOL=0.64, GRASS=1.56, RIVER=2.78, SIREN=10.44
    """
    if annualized_vol > 5.0:
        return "EXTREME"
    elif annualized_vol > 2.0:
        return "HIGH"
    elif annualized_vol > 1.2:
        return "MID"
    elif annualized_vol > 0.6:
        return "STANDARD"
    else:
        return "LOW"


def _is_non_retriable_error(result: dict) -> bool:
    """Check if the bot error is a semantic/business error that shouldn't trip the breaker."""
    error_msg = str(result.get("error", "")).lower()
    return any(phrase in error_msg for phrase in NON_RETRIABLE_ERRORS)


# ══ Думалка: send command to trading bot ════════════════════════════════════════
async def send_command_to_bot(action: str, symbol: str, trace_id: str = None, **kwargs) -> dict:
    """
    Send a position management command to the trading bot.
    Returns response dict or error dict.
    Also logs the action to dumalka_audit_log DB table.
    
    v0.8.2: RC-4 fix — return early in shadow mode.
    v0.8.2: Circuit breaker — disable after MAX_CONSECUTIVE_FAILURES.
    v0.9.2: Per-symbol circuit breaker isolation.
    v0.9.2: Smart error classification + auto-cooldown recovery.
    v0.14.3: Phantom bypass — no HTTP calls if is_phantom=True.
    """

    # ── v0.14.3: Phantom Bypass (Shadow Analytics for closed trades) ──
    if kwargs.get("is_phantom"):
        logger.info(f"👻 [PHANTOM] Dumalka would send: {action} {symbol} (trace={trace_id})")
        # Do not save audit logs for phantom movements to avoid UI clutter
        return {"ok": True, "mode": "phantom"}

    # ── RC-4: Shadow mode → log + audit + buffer for TG digest (no HTTP) ──
    if not config.DUMALKA_ACTIVE_MODE:
        logger.info(f"🧠 Dumalka [SHADOW]: {action} {symbol} (trace={trace_id})")
        await _write_audit_log(trace_id, symbol, action + "_shadow", kwargs.get("diagnostics"))
        kwargs["_mode"] = "shadow"
        await _notify_telegram_action(action, symbol, trace_id, kwargs)
        return {"ok": True, "mode": "shadow"}

    # ── Circuit breaker: per-symbol failure tracking with auto-cooldown ───
    sym_failures = _consecutive_failures.get(symbol, 0)
    if sym_failures >= MAX_CONSECUTIVE_FAILURES:
        # v0.9.2: Auto-cooldown — try again after BREAKER_COOLDOWN_SEC
        tripped_at = _breaker_tripped_at.get(symbol, 0)
        elapsed = time.time() - tripped_at
        if elapsed >= BREAKER_COOLDOWN_SEC:
            # Cooldown expired → reset and retry
            logger.info(
                f"🧠 Dumalka [BREAKER RESET]: {symbol} cooldown expired "
                f"({elapsed:.0f}s > {BREAKER_COOLDOWN_SEC}s), retrying..."
            )
            _consecutive_failures[symbol] = 0
            _breaker_tripped_at.pop(symbol, None)
            sym_failures = 0
        else:
            remaining = BREAKER_COOLDOWN_SEC - elapsed
            logger.warning(
                f"🧠 Dumalka [CIRCUIT BREAKER]: {sym_failures} consecutive failures for {symbol}, "
                f"falling back to shadow for {action} (retry in {remaining:.0f}s)"
            )
            await _write_audit_log(trace_id, symbol, action + "_breaker", kwargs.get("diagnostics"))
            kwargs["_mode"] = "circuit_breaker"
            await _notify_telegram_action(action, symbol, trace_id, kwargs)
            return {"ok": False, "mode": "circuit_breaker"}

    url = f"{config.DUMALKA_BOT_URL.rstrip('/')}/dumalka/command"
    fallback_url = f"{config.DUMALKA_BOT_URL_FALLBACK.rstrip('/')}/dumalka/command" if config.DUMALKA_BOT_URL_FALLBACK else None
    payload = {"action": action, "symbol": symbol, "trace_id": trace_id, **kwargs}
    headers = {
        "Content-Type": "application/json",
        "X-Dumalka-Token": config.DUMALKA_BOT_TOKEN,
    }

    # Try primary URL first, then fallback
    urls_to_try = [url]
    if fallback_url and fallback_url != url:
        urls_to_try.append(fallback_url)

    last_error = None
    for attempt_url in urls_to_try:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(attempt_url, json=payload, headers=headers)
                try:
                    result = r.json()
                except Exception:
                    result = {"ok": False, "error": f"non-json response (HTTP {r.status_code})"}
                if r.status_code == 200 and result.get("ok"):
                    _consecutive_failures[symbol] = 0  # Reset on success (per-symbol)
                    _breaker_tripped_at.pop(symbol, None)
                    # v0.13.1: Reset phantom counter on success — bot confirmed position is alive
                    pid = kwargs.get("_pos_id")
                    if pid is not None:
                        _no_trade_count.pop(pid, None)
                    mode = result.get('mode', 'active').upper()
                    logger.info(f"🧠 Dumalka [{mode}]: {action} {symbol} OK (trace={trace_id}) via {attempt_url}")
                    await _write_audit_log(trace_id, symbol, action, kwargs.get("diagnostics"))
                    # ── Telegram notification on real actions ────────────────
                    kwargs["_mode"] = "active"
                    await _notify_telegram_action(action, symbol, trace_id, kwargs)
                else:
                    # v0.9.2: Smart classification — non-retriable errors don't trip breaker
                    if _is_non_retriable_error(result):
                        error_msg_lower = str(result.get("error", "")).lower()
                        is_phantom = "no active trade" in error_msg_lower or "position not found" in error_msg_lower
                        if is_phantom:
                            # v0.13.1: Track per-position phantom count for auto-close
                            # pos_id is injected by caller via kwargs if available
                            pid = kwargs.get("_pos_id")
                            if pid is not None:
                                _no_trade_count[pid] = _no_trade_count.get(pid, 0) + 1
                                cnt = _no_trade_count[pid]
                                logger.warning(
                                    f"🔮 Dumalka: {action} {symbol} PHANTOM #{cnt}/{PHANTOM_THRESHOLD}: "
                                    f"{result} (pos_id={pid})"
                                )
                        logger.warning(
                            f"🧠 Dumalka: {action} {symbol} NON-RETRIABLE: {result} "
                            f"(breaker NOT incremented)"
                        )
                        # v0.14.0: Critical alert escalation
                        pid = kwargs.get("_pos_id")
                        if pid is not None:
                            fail_key = f"cmd_fail_{pid}"
                            _cmd_fail_counts[fail_key] = _cmd_fail_counts.get(fail_key, 0) + 1
                            if _cmd_fail_counts[fail_key] == CMD_FAIL_ALERT_THRESHOLD:
                                logger.critical(
                                    f"🚨 [DESYNC SHIELD] {symbol} pos #{pid}: "
                                    f"Bot rejected {CMD_FAIL_ALERT_THRESHOLD}+ commands! "
                                    f"Converting to 👻 PHANTOM mode."
                                )
                                # v0.14.3: Phantom Analytics. Instead of spamming bot, transition state.
                                try:
                                    await pg_execute(
                                        "UPDATE open_positions SET status = 'phantom' WHERE id = ?",
                                        (pid,)
                                    )
                                    await _send_telegram_direct(
                                        f"👻 *PHANTOM MODE ENABLED: {symbol}*\n"
                                        f"Bot rejected {CMD_FAIL_ALERT_THRESHOLD} commands for pos #{pid}\n"
                                        f"Action: {action}\n"
                                        f"Risk Engine will now track this trade strictly for *Analytics Only*.\n"
                                        f"No further commands will be sent to the bot."
                                    )
                                except Exception as dbe:
                                    logger.error(f"Failed to update phantom status for {pid}: {dbe}")
                    else:
                        _consecutive_failures[symbol] = _consecutive_failures.get(symbol, 0) + 1
                        cnt = _consecutive_failures[symbol]
                        if cnt >= MAX_CONSECUTIVE_FAILURES:
                            _breaker_tripped_at[symbol] = time.time()
                        logger.warning(f"🧠 Dumalka: {action} {symbol} FAILED ({cnt}x): {result}")
                    # v0.9.4 FIX-A: save bot response for post-mortem analysis
                    await _write_audit_log(trace_id, symbol, action + "_failed", kwargs.get("diagnostics"), bot_response=result)
                return result
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
            if attempt_url != urls_to_try[-1]:
                logger.warning(f"🧠 Dumalka: {attempt_url} unreachable, trying fallback...")
                continue
            # Last URL also failed
            _consecutive_failures[symbol] = _consecutive_failures.get(symbol, 0) + 1
            cnt = _consecutive_failures[symbol]
            if cnt >= MAX_CONSECUTIVE_FAILURES:
                _breaker_tripped_at[symbol] = time.time()
            logger.error(f"🧠 Dumalka: ALL URLs failed for {action} {symbol} ({cnt}x): {e}")
            await _write_audit_log(trace_id, symbol, action + "_error", kwargs.get("diagnostics"), bot_response={"connection_error": str(e)})
            await _notify_telegram_action(action, symbol, trace_id, kwargs)
            return {"ok": False, "error": str(e)}
        except Exception as e:
            _consecutive_failures[symbol] = _consecutive_failures.get(symbol, 0) + 1
            cnt = _consecutive_failures[symbol]
            if cnt >= MAX_CONSECUTIVE_FAILURES:
                _breaker_tripped_at[symbol] = time.time()
            logger.error(f"🧠 Dumalka: Failed to send {action} to bot ({cnt}x): {e}")
            await _write_audit_log(trace_id, symbol, action + "_error", kwargs.get("diagnostics"), bot_response={"exception": str(e)})
            await _notify_telegram_action(action, symbol, trace_id, kwargs)
            return {"ok": False, "error": str(e)}

# ── Zone-Based Exit Policy (§10.5) — v0.8.6: data-calibrated thresholds ──────
# v0.8.6: Calibrated on 369 positions + 60K snapshots (2026-03-22).
# Zone 1: 10%→5% TP (38% of positions reach 5% vs 28.5% at 10%).
# DD thresholds SOFTENED (Z1:35→40%, Z2:25→30%): 79% of profitable positions
# have DD≥30% and many recover — too tight = false exits.
DEFAULT_ZONE_POLICY = [
    {"name": "Zone 0", "min_tp": 0,  "max_tp": 3,   "dd_thresh": None, "action": "hold"},
    {"name": "Zone 1", "min_tp": 3,  "max_tp": 20,  "dd_thresh": 40,   "action": "sl_breakeven"},
    {"name": "Zone 2", "min_tp": 20, "max_tp": 40,  "dd_thresh": 30,   "action": "full_close"},
    {"name": "Zone 3", "min_tp": 40, "max_tp": 70,  "dd_thresh": 25,   "action": "full_close"},
    {"name": "Zone 4", "min_tp": 70, "max_tp": 999, "dd_thresh": 15,   "action": "full_close"},
]

# Live mutable thresholds (loaded from DB at startup, recalibrated periodically)
ZONE_POLICY = list(DEFAULT_ZONE_POLICY)


async def load_calibrated_thresholds():
    """Load the latest active calibration from DB, or use defaults."""
    global ZONE_POLICY
    try:
        row = await pg_fetch_one(
            "SELECT * FROM zone_calibration WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        )
        if row:
            cal = dict(row)
            ZONE_POLICY[1]["dd_thresh"] = cal["zone_1_dd_thresh"]
            ZONE_POLICY[2]["dd_thresh"] = cal["zone_2_dd_thresh"]
            ZONE_POLICY[3]["dd_thresh"] = cal["zone_3_dd_thresh"]
            ZONE_POLICY[4]["dd_thresh"] = cal["zone_4_dd_thresh"]
            logger.info(
                f"Loaded calibrated thresholds (id={cal['id']}, "
                f"{cal['calibrated_at'][:10]}): "
                f"Z1={cal['zone_1_dd_thresh']}% Z2={cal['zone_2_dd_thresh']}% "
                f"Z3={cal['zone_3_dd_thresh']}% Z4={cal['zone_4_dd_thresh']}% "
                f"(CR={cal['capture_ratio_avg']:.1%}, n={cal['n_positions_used']})"
            )
            return
    except Exception as e:
        logger.warning(f"Could not load calibrated thresholds: {e}")

    logger.info("Using default zone thresholds (no calibration in DB)")


def get_zone(tp_progress_pct: float) -> dict:
    """Get the active zone for a given TP progress percentage."""
    for zone in reversed(ZONE_POLICY):
        if tp_progress_pct >= zone["min_tp"]:
            return zone
    return ZONE_POLICY[0]


# ── Adaptive Factor (§10.6) — v0.12.0: regime-aware cap ──────────────────────
def compute_adaptive_factor(p_tp: float, p_sl: float, is_profit_zone: bool = False, regime: str = "normal") -> float:
    """
    adaptive_factor = clamp(P_tp / P_sl, 0.5, cap)

    v0.8.2 FIX (RC-2): For profit-taking zones (Zone 2-4), cap prevents
    over-relaxation of drawdown thresholds.
    v0.12.0 (Dmitry feedback): Cap is now regime-aware:
      - trending → 1.5 (let profits run)
      - normal   → 1.2 (balanced)
      - ranging  → 0.9 (take profits faster)
    For Zone 1 (breakeven), allow up to 2.0 regardless of regime.
    """
    if is_profit_zone:
        regime_caps = {"trending": 1.5, "normal": 1.2, "ranging": 0.9}
        cap = regime_caps.get(regime, 1.2)
    else:
        cap = 2.0
    if p_sl <= 0.01:  # v0.8.2: floor p_sl at 0.01 instead of 0
        p_sl = 0.01
    factor = p_tp / p_sl
    return max(0.5, min(cap, factor))


def compute_volume_modifier(volume_ratio: float) -> float:
    """
    Volume ratio as threshold modifier (§10.11):
      < 0.5 → tighten (0.8x = stricter)
      0.5-2.0 → normal (1.0x)
      > 3.0 → tighten (0.7x = stricter, anomalous volume)
    """
    if volume_ratio < 0.5:
        return 0.8
    elif volume_ratio > 3.0:
        return 0.7
    return 1.0



class NpEncoder(json.JSONEncoder):
    """
    JSON Encoder that handles NumPy data types (int, float, ndarray).
    Used primarily for serializing GPU Monte Carlo metrics (VaR, P_sl) into the audit log.
    """
    def default(self, obj):
        """Override the default method to intercept NumPy types before serialization."""
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)

async def _write_audit_log(trace_id, symbol, action, diagnostics=None, bot_response=None):
    """Write to dumalka_audit_log DB table.
    
    v0.9.4 FIX-A: bot_response is merged into mc_diagnostics JSON
    so we can diagnose why the bot rejected a command from the DB alone,
    without needing to dig through stderr logs.
    """
    try:
        # Merge diagnostics + bot_response into one JSON blob
        combined = {}
        if diagnostics and isinstance(diagnostics, dict):
            combined.update(diagnostics)
        if bot_response is not None:
            # Preserve JSON structure for dicts/lists, fallback to string for anything else
            if isinstance(bot_response, (dict, list)):
                combined["bot_response"] = bot_response
            else:
                combined["bot_response"] = str(bot_response)[:500]
        diagnostics_json = json.dumps(combined, cls=NpEncoder) if combined else None
        await pg_execute(
            '''INSERT INTO dumalka_audit_log (timestamp, trace_id, symbol, action, mc_diagnostics)
               VALUES (?, ?, ?, ?, ?)''',
            (datetime.now(timezone.utc).isoformat(), trace_id, symbol, action, diagnostics_json)
        )
    except Exception as db_err:
        logger.error(f"Failed to write audit log: {db_err}")


async def _notify_telegram_action(action, symbol, trace_id, kwargs):
    """Buffer Dumalka actions for hourly digest instead of per-action spam.
    Critical actions (full_close, portfolio_override) are sent immediately.
    """
    try:
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            return

        fraction = kwargs.get('fraction', 0)
        reason = kwargs.get('reason', '')
        # Use only single-codepoint emojis to avoid Telegram rendering bugs
        emoji = {"move_sl": "🔰", "full_close": "🔒"}.get(action, "🧠")

        mode = kwargs.get("_mode", "active")

        entry = {
            "time": datetime.now(timezone.utc).strftime("%H:%M"),
            "emoji": emoji,
            "action": action.upper(),
            "symbol": symbol,
            "fraction": fraction,
            "reason": reason,
            "mode": mode,  # v0.9.2: track active/shadow/circuit_breaker
        }

        # Critical actions → send immediately (no buffering)
        if action in ("full_close", "portfolio_override"):
            reason_safe = _escape_md(reason)
            msg = (
                f"🚨 *ДУМАЛКА: {action.upper()}*\n"
                f"Symbol: {symbol}\n"
            )
            if fraction:
                msg += f"Fraction: {fraction*100:.0f}%\n"
            if reason_safe:
                msg += f"Reason: {reason_safe}\n"
            msg += f"Trace: `{trace_id or 'N/A'}`"
            await _send_telegram_direct(msg)
            return

        # Non-critical → buffer for digest (cap at 500 to prevent memory leak)
        if len(_tg_action_buffer) < 500:
            _tg_action_buffer.append(entry)

    except Exception:
        pass  # Non-critical, don't break the flow


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special characters."""
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


async def _send_telegram_direct(msg: str):
    """Send a message directly via Telegram Bot API."""
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": msg, "parse_mode": "Markdown"
            })
            if resp.status_code != 200:
                logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


async def _flush_telegram_digest():
    """Send buffered actions as one formatted digest message.
    Called by the main tracker loop every DIGEST_INTERVAL_SEC.
    """
    global _tg_last_digest_time
    _tg_last_digest_time = time.time()

    if not _tg_action_buffer:
        return

    # Group by symbol
    by_symbol: dict[str, list] = {}
    for entry in _tg_action_buffer:
        sym = entry["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(entry)

    # Build digest message
    total = len(_tg_action_buffer)
    times = [e["time"] for e in _tg_action_buffer]
    time_range = f"{times[0]}–{times[-1]}" if len(times) > 1 else times[0]

    msg = f"📋 *ДУМАЛКА ДАЙДЖЕСТ*  ({total} действий)\n"
    msg += f"{time_range} UTC\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"

    for sym, actions in by_symbol.items():
        # Count action types
        action_counts: dict[str, int] = {}
        last_reason = ""
        sym_mode = "active"  # v0.9.2: track dominant mode for this symbol
        for a in actions:
            key = a["action"]
            action_counts[key] = action_counts.get(key, 0) + 1
            last_reason = a["reason"]
            sym_mode = a.get("mode", "active")  # last mode wins

        parts = []
        for act, cnt in action_counts.items():
            parts.append(f"{act} x{cnt}")
        summary = ", ".join(parts)

        # v0.9.2: Mode badge — clearly distinguish real vs shadow
        if sym_mode == "shadow":
            mode_badge = "👻"
        elif sym_mode == "circuit_breaker":
            mode_badge = "⚡"
        else:
            mode_badge = actions[0]['emoji']

        msg += f"\n{mode_badge} *{sym}*: {summary}\n"
        if sym_mode != "active":
            mode_label = "SHADOW" if sym_mode == "shadow" else "BREAKER"
            msg += f"    ⚠️ Режим: {mode_label} (не исполнено)\n"
        if last_reason:
            reason_safe = _escape_md(last_reason)
            msg += f"    {reason_safe}\n"

    await _send_telegram_direct(msg)
    _tg_action_buffer.clear()
    logger.info(f"📋 Telegram digest sent: {total} actions across {len(by_symbol)} symbols")


# ── GPU: MC Forward Projection (§10.6) — v0.18.1: full E[PnL] ──────────────
async def run_forward_projection(symbol, side, size, entry, current_price, current_vol, sl, tp3, equity=None):
    """
    GPU (Titan V): Run multi-step MC paths, compute barrier probabilities
    and full expected PnL across all paths.

    Returns: (p_tp, p_sl, e_pnl_pct, full_e_pnl_pct, pnl_skewness, mc_var, latency_ms)

    v0.18.1 (2026-04-02): "Let Winners Run" reform.
      - TP cap removed: real TP3 passed to MC (was capped at MAX_TP_FOR_ZONES_PCT=10%).
      - Returns full_e_pnl_pct (mean PnL across ALL paths) and pnl_skewness.
      - Jump-diffusion params from config (was hardcoded).
    v0.17.0 (2026-03-26): Replaced VaR-based heuristic with path simulation.
    """
    t0 = time.perf_counter()

    if equity is None:
        equity = config.DEFAULT_EQUITY

    # Determine SL/TP prices for the simulation
    if sl and sl > 0:
        sl_price = sl
    else:
        sl_price = current_price * (0.97 if side == "long" else 1.03)

    if tp3 and tp3 > 0:
        tp_price = tp3
    else:
        tp_price = current_price * (1.05 if side == "long" else 0.95)

    # v0.18.1: TP cap removed from MC — pass real TP3 to simulation.
    # MAX_TP_FOR_ZONES_PCT is for zone progress math only (as documented at line 64-69).
    # The old cap artificially reduced perceived upside, making E_pnl biased negative
    # for positions with distant TP3 (e.g. CUSDT TP3=+99% was capped to 10%).

    sim_result = await asyncio.to_thread(
        simulate_sl_tp_probability,
        symbol=symbol,
        side=side,
        current_price=current_price,
        sl_price=sl_price,
        tp_price=tp_price,
        volatility=current_vol,
        n_scenarios=min(config.SCENARIOS, 50_000),
        horizon_hours=24.0,
        n_steps=48,
        lambda_jump=config.MC_LAMBDA_JUMP,
        mu_jump=config.MC_MU_JUMP,
        sigma_jump=config.MC_SIGMA_JUMP,
    )

    latency_ms = (time.perf_counter() - t0) * 1000

    p_tp = sim_result["p_tp"]
    p_sl = sim_result["p_sl"]
    e_pnl_pct = sim_result["e_pnl_pct"]
    full_e_pnl_pct = sim_result["full_e_pnl_pct"]
    pnl_skewness = sim_result["pnl_skewness"]

    # mc_var: still useful for portfolio-level decisions, run lightweight
    req_portfolio = Portfolio(equity=equity, positions=[
        Position(symbol=symbol, side=side, size=size, entry_price=entry)
    ])
    mc_result = await asyncio.to_thread(
        run_monte_carlo_risk,
        portfolio=req_portfolio,
        candidate=CandidateTrade(symbol=symbol, side=side, size=0),
        market=MarketData(prices={symbol: current_price}, volatility={symbol: current_vol}),
        limits=RiskLimits(max_var=1.0, max_cvar=1.0, max_liquidation_prob=1.0),
        n_scenarios=10_000,
    )

    logger.debug(
        f"[MC Projection] {symbol}: P_tp={p_tp:.3f} P_sl={p_sl:.3f} "
        f"E_pnl={e_pnl_pct:+.2f}% Full_E={full_e_pnl_pct:+.2f}% "
        f"Skew={pnl_skewness:+.2f} VaR={mc_result.var:.4f} ({latency_ms:.0f}ms)"
    )

    return p_tp, p_sl, e_pnl_pct, full_e_pnl_pct, pnl_skewness, mc_result.var, latency_ms


# ── GPU: Differential MC (§10.7) — runs ONLY on trigger ─────────────────────
async def run_differential_mc(symbol, side, size, entry, current_price, current_vol):
    """
    GPU (Titan V): Run 10 MC scenarios across fine-grained close fractions
    to find optimal close fraction via CVaR optimization.

    v0.12.0 (Dmitry feedback): Expanded from 4 fractions [0, 0.3, 0.5, 1.0]
    to 10 fractions for much finer-grained GPU optimization, especially
    around the moonbag range (0.85-0.95).

    Returns: (optimal_close_fraction, latency_ms)
    """
    t0 = time.perf_counter()
    fractions = [0.0, 0.15, 0.25, 0.35, 0.50, 0.65, 0.75, 0.85, 0.95, 1.00]
    cvars = []

    for frac in fractions:
        remaining_size = size * (1.0 - frac)
        if remaining_size < 1e-8:
            remaining_size = 0.0

        positions = []
        if remaining_size > 0:
            positions.append(Position(symbol=symbol, side=side, size=remaining_size, entry_price=entry))

        portfolio = Portfolio(equity=config.DEFAULT_EQUITY, positions=positions)
        mc = await asyncio.to_thread(
            run_monte_carlo_risk,
            portfolio=portfolio,
            candidate=None,
            market=MarketData(prices={symbol: current_price}, volatility={symbol: current_vol}),
            limits=RiskLimits(max_var=1.0, max_cvar=1.0, max_liquidation_prob=1.0),
            n_scenarios=config.SCENARIOS,
        )
        cvars.append(mc.cvar)

    latency_ms = (time.perf_counter() - t0) * 1000

    # Optimal = best balance of risk reduction vs upside preservation
    # alpha=0.6 (preserve upside), beta=0.4 (reduce risk)  — from §10.7
    alpha, beta = 0.6, 0.4
    best_frac = 0.0
    best_score = float('inf')
    cvar_hold = cvars[0] if cvars[0] > 0 else 1e-6

    for i, frac in enumerate(fractions):
        upside_loss = frac  # normalized: closing 100% = max upside loss
        risk_remaining = cvars[i] / cvar_hold  # normalized: 1.0 = no reduction
        score = alpha * upside_loss + beta * risk_remaining
        if score < best_score:
            best_score = score
            best_frac = frac

    # Log a compact summary of CVaR landscape
    cvar_summary = ' '.join(f"{int(f*100)}%={c:.4f}" for f, c in zip(fractions, cvars))
    logger.info(
        f"[Differential MC] CVaRs: {cvar_summary} → optimal={best_frac*100:.0f}% "
        f"({latency_ms:.0f}ms)"
    )
    return best_frac, latency_ms


# ══════════════════════════════════════════════════════════════════════════════
# MAIN TRACKING LOOP
# ══════════════════════════════════════════════════════════════════════════════
async def track_open_positions():
    """
    Main Position Tracking Loop (Думалка Core).

    Runs as a background asyncio task, cycling every ~60 seconds.
    For each open position, executes a 7-step pipeline:

    Steps
    -----
    1. **State Ingestion**: Fetch live price from Bybit, compute PnL,
       update max_pnl_pct (high-water mark), calculate drawdown from peak.
    2. **Global Shield (Sentinel)**: Check BTC flash-crash status.
       If triggered → emergency FULL_CLOSE all positions.
    3. **Time-Decay (Apollo Protocol)**: Positions stuck 1-4h in Zone 0
       → Fractional Bail (70% close) + Soft Breakeven on remainder.
    4. **GPU Monte Carlo Oracle**: Run 100K scenarios on Titan V.
       Returns P(TP), P(SL), E[PnL]. Adapts zone thresholds dynamically.
    5. **Zone Policy**: Determine zone (0-4) based on TP progress.
       If drawdown exceeds adaptive threshold → trigger partial_close or sl_breakeven.
    6. **Idempotent Execution**: Send command to Trading Bot via HTTP.
       Track cumulative closed_fraction to prevent double-closes.
    7. **ML Snapshot**: Record 30+ feature snapshot to position_snapshots
       for future Supervised ML / Reinforcement Learning training.

    Additional per-cycle tasks:
    - Flush Telegram digest (hourly).
    - Reload zone calibration from DB (every ~24h).
    - Compound Growth shadow reporting (if enabled).
    """

    # Load calibrated thresholds from DB (or use defaults)
    await load_calibrated_thresholds()

    await _ensure_db_columns()

    # Note: position_manager.py was archived (v0.13.1) — its logic was a subset of this module.

    mode = "LIVE" if config.DUMALKA_ACTIVE_MODE else "SHADOW"
    logger.info(
        f"🧠 Position Tracker (Думалка) started | mode={mode} | "
        f"cycle={CYCLE_INTERVAL_SEC}s | zones=5 | recalibrate_every={1440} cycles"
    )

    cycle_count = 0
    RECALIBRATE_EVERY = 2880  # reload thresholds every ~24h (2880 cycles × 30s)

    while True:
        try:
            await asyncio.sleep(CYCLE_INTERVAL_SEC)
            cycle_count += 1

            # Flush Telegram digest hourly
            if time.time() - _tg_last_digest_time > DIGEST_INTERVAL_SEC:
                await _flush_telegram_digest()

            # Periodic threshold reload (in case auto-recalibration ran)
            if cycle_count % RECALIBRATE_EVERY == 0:
                await load_calibrated_thresholds()

            positions = await pg_fetch_all("SELECT * FROM open_positions WHERE status IN ('open', 'phantom')")

            if not positions:
                _heartbeat["last_success"] = time.time()
                _heartbeat["cycle"] = cycle_count
                continue

            logger.info(f"━━━ Tracking {len(positions)} open positions ━━━")

            # ── Cache market data per symbol (FIX #4) ─────────────────────
            market_cache = {}
            volume_cache = {}
            funding_cache = {}    # v0.8.4: funding_rate, oi_change per symbol
            spread_cache = {}     # v0.8.4: orderbook spread per symbol
            trend_cache = {}      # v0.8.4: multi-TF trend sum per symbol
            rsi_cache = {}        # v0.13.2: RSI-14 per symbol
            lsr_cache = {}        # v0.13.2: long/short ratio per symbol
            ob_imbalance_cache = {}  # v0.13.2: orderbook imbalance per symbol
            symbols_needed = set(dict(row)["symbol"] for row in positions)

            # v0.13.2: BTC change (shared across all symbols)
            try:
                btc_change = await fetch_btc_change_1h()
            except Exception:
                btc_change = 0.0

            for symbol in symbols_needed:
                price, vol = await fetch_market_data(symbol)
                volume_ratio = await fetch_volume_info(symbol)
                market_cache[symbol] = (price, vol)
                volume_cache[symbol] = volume_ratio
                # v0.8.4: Fetch enriched ML features
                try:
                    fr, oi_chg = await fetch_funding_and_oi(symbol)
                    funding_cache[symbol] = (fr, oi_chg)
                except Exception:
                    funding_cache[symbol] = (0.0, 0.0)
                try:
                    sp, _ = await fetch_orderbook_depth(symbol)
                    spread_cache[symbol] = sp
                except Exception:
                    spread_cache[symbol] = 0.0
                try:
                    tf_trends = await fetch_multi_timeframe(symbol)
                    # trend_sum: +1 per bullish TF, -1 per bearish, 0 neutral
                    tsum = 0.0
                    if tf_trends:
                        for tf_label, direction in tf_trends.items():
                            if direction == "bullish":
                                tsum += 1.0
                            elif direction == "bearish":
                                tsum -= 1.0
                    trend_cache[symbol] = tsum
                except Exception:
                    trend_cache[symbol] = 0.0
                # v0.13.2: New ML Intelligence features
                try:
                    rsi_cache[symbol] = await fetch_rsi_from_klines(symbol)
                except Exception:
                    rsi_cache[symbol] = None
                try:
                    lsr_cache[symbol] = await fetch_long_short_ratio(symbol)
                except Exception:
                    lsr_cache[symbol] = 1.0
                try:
                    bids, asks = await fetch_orderbook_raw(symbol)
                    ob_imbalance_cache[symbol] = compute_orderbook_imbalance(bids, asks)
                except Exception:
                    ob_imbalance_cache[symbol] = 1.0

            # v0.8.4: Fetch real wallet equity for MC calculations
            live_equity = config.DEFAULT_EQUITY
            try:
                wallet = await fetch_wallet_balance()
                if wallet and wallet.get("equity", 0) > 0:
                    live_equity = float(wallet["equity"])
                    logger.info(f"Using live equity: ${live_equity:.2f}")

                    # v0.11.0 REDU-PATCH: Compound Growth Shadow Report
                    try:
                        from compound_growth import get_compound_engine
                        cg = get_compound_engine()
                        shadow = cg.get_shadow_report(live_equity, flat_size_usd=5.0)
                        logger.info(shadow)
                    except Exception as cg_err:
                        logger.debug(f"Compound growth shadow skipped: {cg_err}")
            except Exception:
                pass  # fallback to DEFAULT_EQUITY

            # v0.8.4: Get current regime for snapshots
            current_regime = None
            try:
                regime_info = get_cached_regime()
                if regime_info:
                    current_regime = regime_info.get("regime", "normal")
            except Exception:
                pass

            # ── v0.7.0: Update regime detector using BTC data ──────────
            regime_dd_sensitivity = 1.0
            if config.REGIME_DETECTOR_ENABLED:
                btc_data = market_cache.get("BTCUSDT")
                btc_vol_ratio = volume_cache.get("BTCUSDT", 1.0)
                btc_vol = btc_data[1] if btc_data else 0.5
                try:
                    # fetch BTC klines for regime detection
                    import httpx
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        r = await client.get(f"{config.BYBIT_PROXY_URL}/klines/BTCUSDT?interval=60&limit=60")
                        if r.status_code == 200:
                            kdata = r.json()
                            klines = kdata.get("klines", kdata) if isinstance(kdata, dict) else kdata
                            if isinstance(klines, list):
                                klines.reverse()  # newest-first → chronological
                                await update_regime(klines, btc_vol_ratio, btc_vol)
                except Exception as e:
                    logger.debug(f"Regime update skipped: {e}")
                regime_dd_sensitivity = get_zone_dd_sensitivity()

            # ── v0.12.0 (Dmitry): Portfolio Heat Check — BTC Correlation Shield ──
            # If most open positions are highly correlated to BTC,
            # tighten ALL zone thresholds to protect against slow BTC bleeds.
            heat_modifier = 1.0  # default: no modification
            try:
                if len(symbols_needed) >= 2:
                    from core.gpu_analytics import gpu_correlation_matrix
                    # Collect 24h returns from kline cache for each symbol
                    symbol_returns = {}
                    for sym in symbols_needed:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as hc:
                                r = await hc.get(f"{config.BYBIT_PROXY_URL}/klines/{sym}?interval=60&limit=24")
                                if r.status_code == 200:
                                    kd = r.json()
                                    kls = kd.get("klines", kd) if isinstance(kd, dict) else kd
                                    if isinstance(kls, list) and len(kls) >= 5:
                                        closes = [float(k[4]) for k in kls]
                                        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                                                   for i in range(1, len(closes))]
                                        symbol_returns[sym] = returns
                        except Exception:
                            pass

                    # Add BTC as reference anchor (if not already in portfolio)
                    if "BTCUSDT" not in symbol_returns:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as hc:
                                r = await hc.get(f"{config.BYBIT_PROXY_URL}/klines/BTCUSDT?interval=60&limit=24")
                                if r.status_code == 200:
                                    kd = r.json()
                                    kls = kd.get("klines", kd) if isinstance(kd, dict) else kd
                                    if isinstance(kls, list) and len(kls) >= 5:
                                        closes = [float(k[4]) for k in kls]
                                        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                                                   for i in range(1, len(closes))]
                                        symbol_returns["BTCUSDT"] = returns
                        except Exception:
                            pass

                    if len(symbol_returns) >= 2 and "BTCUSDT" in symbol_returns:
                        corr_result = gpu_correlation_matrix(symbol_returns)
                        # Find average correlation of open positions to BTC
                        btc_idx = corr_result["symbols"].index("BTCUSDT") if "BTCUSDT" in corr_result.get("symbols", []) else -1
                        if btc_idx >= 0 and corr_result.get("matrix"):
                            btc_corrs = []
                            for i, sym in enumerate(corr_result["symbols"]):
                                if sym != "BTCUSDT" and sym in symbols_needed:
                                    btc_corrs.append(abs(corr_result["matrix"][btc_idx][i]))
                            if btc_corrs:
                                avg_btc_corr = sum(btc_corrs) / len(btc_corrs)
                                if avg_btc_corr > 0.80:
                                    heat_modifier = 0.7  # aggressive tightening
                                elif avg_btc_corr > 0.60:
                                    heat_modifier = 0.85  # moderate tightening
                                logger.info(
                                    f"🔥 Portfolio Heat: avg_btc_corr={avg_btc_corr:.3f}, "
                                    f"positions={len(btc_corrs)}, heat_modifier={heat_modifier}"
                                )
            except Exception as e:
                logger.debug(f"Portfolio heat check skipped: {e}")

            # ── v0.14.1: Bot Position Sync — fetch active symbols from bot ──
            _bot_confirmed_symbols.clear()
            bot_syms = await _fetch_bot_active_symbols()
            _bot_confirmed_symbols.update(bot_syms)
            if bot_syms:
                logger.info(f"🤖 Bot sync: {len(bot_syms)} active trades: {', '.join(sorted(bot_syms))}")
            elif not _bot_sync_failed:
                logger.debug("🤖 Bot sync: 0 active trades")

            for row in positions:
                pos = dict(row)
                pos_id = pos["id"]
                pos_status = pos.get("status", "open")
                async def _send_bot(*args, **kwargs):
                    return await send_command_to_bot(*args, is_phantom=(pos_status == 'phantom'), **kwargs)

                symbol = pos["symbol"]
                side = pos["side"]
                entry = pos["entry_price"]
                size = pos["size"]
                sl = pos["current_sl"]
                original_sl = pos.get("original_sl") or sl  # v0.14.0: Midas original SL
                tp1 = pos["current_tp1"]
                tp3 = pos["current_tp3"]
                opened_at_str = pos["opened_at"]

                current_price, current_vol = market_cache.get(symbol, (0.0, 0.8))
                volume_ratio = volume_cache.get(symbol, 1.0)
                if current_price <= 0:
                    continue

                # Determine position age for multiple grace periods
                try:
                    _op_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                    _age_sec = (datetime.now(timezone.utc) - _op_at).total_seconds()
                except Exception:
                    _age_sec = 9999

                # ── v0.18.3: Bot roster desync — DB still OPEN but /dumalka/positions has no symbol ──
                # Observe-only alone leaves status='open' forever on dashboard. After N successful
                # syncs without the symbol, mark closed (manual close on exchange, bot restart, etc.).
                # Trade-off: rows that were never opened on bot (KillSwitch, etc.) also have no
                # symbol in roster — they will close after the same N cycles (~10 min default), ending
                # long paper observation; raise BOT_ABSENT_CLOSE_CYCLES if longer observe-only is needed.
                if side == "long":
                    _early_pnl = ((current_price - entry) / entry) * 100
                else:
                    _early_pnl = ((entry - current_price) / entry) * 100
                _early_max = max(pos.get("max_pnl_pct") or 0, _early_pnl)
                _early_dd = max(0, _early_max - _early_pnl)
                _early_tp = pos.get("tp_progress_pct") or 0
                if not _bot_sync_failed:
                    if _age_sec > BOT_VERIFY_GRACE_SEC and symbol not in _bot_confirmed_symbols:
                        _bot_absent_streak[pos_id] = _bot_absent_streak.get(pos_id, 0) + 1
                        _need_cycles = config.BOT_ABSENT_CLOSE_CYCLES  # clamped in config (min 1)
                        if _bot_absent_streak[pos_id] >= _need_cycles:
                            logger.warning(
                                f"📴 [BOT ROSTER DESYNC] Pos #{pos_id} {symbol}: absent from bot "
                                f"{_bot_absent_streak[pos_id]} syncs → closing as bot_sync_desync"
                            )
                            await _close_position(
                                pos_id, "bot_sync_desync", _early_pnl, current_price,
                                _early_max, _early_dd, _early_tp, symbol=symbol,
                                detail="Symbol not in /dumalka/positions for consecutive syncs",
                            )
                            signal_hash = pos.get("signal_hash")
                            asyncio.create_task(insert_trade_outcome(
                                signal_hash=signal_hash,
                                event_type="bot_sync_desync",
                                symbol=symbol, side=side,
                                price_at_event=current_price,
                                pnl_pct=_early_pnl,
                                size_remaining=0.0,
                            ))
                            continue
                    else:
                        _bot_absent_streak.pop(pos_id, None)

                # ── v0.14.1: Observe-Only mode for unconfirmed positions ──
                # If bot sync succeeded and symbol NOT in bot's active trades,
                # this position was never opened (KillSwitch, margin, etc.).
                # We still OBSERVE (PnL, snapshots, SL/TP hit detection) but
                # send NO commands to the bot.
                _observe_only = False
                if not _bot_sync_failed and symbol not in _bot_confirmed_symbols:
                    # Grace: don't verify very fresh positions (bot may still be opening)
                    if _age_sec > BOT_VERIFY_GRACE_SEC:
                        _observe_only = True
                        logger.info(
                            f"👁️ [OBSERVE-ONLY] Pos #{pos_id} {symbol}: not in bot active trades "
                            f"→ tracking as paper position (no commands)"
                        )

                # ── v0.13.1: Auto-Phantom-Close — check if bot confirmed this position is dead ──
                # v0.14.1: Skip phantom check for observe-only (we already know it's not on bot)
                if not _observe_only:
                    phantom_count = _no_trade_count.get(pos_id, 0)
                    if phantom_count >= PHANTOM_THRESHOLD:
                        logger.warning(
                            f"🔮 [PHANTOM AUTO-CLOSE] Pos #{pos_id} {symbol} {side}: "
                            f"{phantom_count}× 'no active trade' from bot → closing as phantom_sync"
                        )
                        current_pnl_pct_tmp = ((current_price - entry) / entry * 100) if side == "long" else ((entry - current_price) / entry * 100)
                        await _close_position(
                            pos_id, "phantom_sync", current_pnl_pct_tmp, current_price,
                            0, 0, 0, symbol=symbol,
                            detail=f"Auto-phantom: {phantom_count}× 'no active trade' from bot"
                        )
                        signal_hash = pos.get("signal_hash")
                        asyncio.create_task(insert_trade_outcome(
                            signal_hash=signal_hash,
                            event_type="phantom_sync",
                            symbol=symbol, side=side,
                            price_at_event=current_price,
                            pnl_pct=current_pnl_pct_tmp,
                            size_remaining=0.0,
                        ))
                        continue  # skip rest of evaluation for auto-closed position

                # ── Calculate PnL metrics ─────────────────────────────────
                if side == "long":
                    current_pnl_pct = ((current_price - entry) / entry) * 100
                    raw_tp_dist_pct = ((tp3 - entry) / entry) * 100 if tp3 and tp3 > entry else MAX_TP_FOR_ZONES_PCT
                else:
                    current_pnl_pct = ((entry - current_price) / entry) * 100
                    raw_tp_dist_pct = ((entry - tp3) / entry) * 100 if tp3 and tp3 < entry else MAX_TP_FOR_ZONES_PCT

                # v0.15.2 TP1 Normalizer: cap TP distance for zone calculations
                # Without this, TP3=+89% makes +5% PnL = only 5.6% tp_progress → Zone 0
                # With cap at 10%, +1% PnL = 10% tp_progress → Zone 1 → SL→BE
                effective_tp_dist = min(raw_tp_dist_pct, MAX_TP_FOR_ZONES_PCT)
                if raw_tp_dist_pct > MAX_TP_FOR_ZONES_PCT:
                    logger.debug(
                        f"[TP1 Norm] {symbol}: raw TP dist={raw_tp_dist_pct:.1f}% "
                        f"capped to {effective_tp_dist:.1f}% for zone calc"
                    )
                dist_to_tp = (abs(current_pnl_pct) / effective_tp_dist) * 100 if effective_tp_dist > 0 else 0
                if current_pnl_pct < 0:
                    dist_to_tp = 0  # negative PnL = 0% progress

                max_pnl_pct = max(pos["max_pnl_pct"] or 0, current_pnl_pct)
                drawdown_from_peak_pct = max(0, max_pnl_pct - current_pnl_pct)
                tp_progress_pct = max(0, min(100, dist_to_tp))

                # ══ Phase 1 CHECK 0: Hard SL Cap — SHADOW MODE ═══════════
                if current_pnl_pct < -config.MAX_LOSS_PCT:
                    if config.PHASE1_ACTIVE_MODE:
                        signal_hash = pos.get("signal_hash")
                        if not _observe_only:
                            logger.warning(
                                f"🛑 Pos #{pos_id} {symbol} {side} HARD SL CAP: "
                                f"loss {current_pnl_pct:.2f}% exceeds -{config.MAX_LOSS_PCT}% → full_close"
                            )
                            result = await _send_bot(
                                "full_close", symbol,
                                trace_id=signal_hash,
                                reason=f"HARD SL CAP: loss {current_pnl_pct:.1f}% > -{config.MAX_LOSS_PCT}%",
                                diagnostics={"trigger": "hard_sl_cap", "pnl": current_pnl_pct,
                                             "max_loss": config.MAX_LOSS_PCT},
                                _pos_id=pos_id,
                            )
                            if not result.get("ok"):
                                logger.warning(f"🛑 Pos #{pos_id} {symbol}: hard SL cap full_close rejected: {result}")
                                continue
                        await _close_position(pos_id, "hard_sl_cap", current_pnl_pct, current_price,
                                              max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol)
                        asyncio.create_task(insert_trade_outcome(
                            signal_hash=signal_hash,
                            event_type="hard_sl_cap",
                            symbol=symbol, side=side,
                            price_at_event=current_price,
                            pnl_pct=current_pnl_pct,
                            size_remaining=0.0,
                        ))
                        continue
                    elif config.PHASE1_SHADOW_MODE:
                        # SHADOW: log only, do NOT close (analytics mode)
                        logger.info(
                            f"📊 Phase 1 SHADOW SL: Pos #{pos_id} {symbol} {side} "
                            f"loss {current_pnl_pct:.2f}% > -{config.MAX_LOSS_PCT}% — would force-close"
                        )

                # ══ CHECK 1: SL/TP3 HIT → auto-close ═══════════════════════
                close_reason = None
                if side == "long":
                    if sl and current_price <= sl:
                        close_reason = "sl_hit"
                    elif tp3 and current_price >= tp3:
                        close_reason = "tp3_hit"
                elif side == "short":
                    if sl and current_price >= sl:
                        close_reason = "sl_hit"
                    elif tp3 and current_price <= tp3:
                        close_reason = "tp3_hit"

                if close_reason:
                    if pos_status == 'phantom':
                        close_reason = f"phantom_{close_reason}"
                    
                    logger.info(f"🔒 Pos #{pos_id} {symbol} {side} CLOSED: {close_reason} (pnl={current_pnl_pct:.2f}%)")
                    await _close_position(pos_id, close_reason, current_pnl_pct, current_price,
                                          max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol)
                    
                    signal_hash = pos.get("signal_hash")

                    # ── v0.15.0: GHOST RECOVERY DISABLED (Apollo Audit) ──
                    # No evidence of benefit in production data. Risk of doubling losses
                    # on real reversals. Re-enable via GHOST_RECOVERY_ENABLED=true after ML validation.
                    if GHOST_RECOVERY_ENABLED and close_reason == "sl_hit" and -1.0 <= current_pnl_pct <= 1.0 and not _observe_only and signal_hash:
                        try:
                            sig_row = await pg_fetch_one("SELECT multi_tf_trends_json FROM signals WHERE signal_hash = ?", (signal_hash,))
                            if sig_row and sig_row.get("multi_tf_trends_json"):
                                mtf = json.loads(sig_row["multi_tf_trends_json"])
                                target_trend = "bullish" if side == "long" else "bearish"
                                if mtf.get("1h") == target_trend and mtf.get("4h") == target_trend:
                                    logger.warning(
                                        f"👻 [GHOST RECOVERY] Pos #{pos_id} {symbol} closed BE. "
                                        f"Trend {target_trend.upper()} is fully intact on HTF! Firing re-entry webhook."
                                    )
                                    payload = {
                                        "source": "ghost_recovery",
                                        "symbol": symbol,
                                        "side": side,
                                        "size": 1.0,
                                        "signal_hash": signal_hash + "_ghost",
                                        "midas_comment": f"Ghost Recovery of {signal_hash[:8]}"
                                    }
                                    headers = {"X-Webhook-Secret": config.WEBHOOK_SECRET, "Content-Type": "application/json"}
                                    async with httpx.AsyncClient(timeout=5.0) as client:
                                        await client.post("http://127.0.0.1:8000/tv-webhook", json=payload, headers=headers)
                        except Exception as e:
                            logger.error(f"❌ Ghost recovery failed for {symbol}: {e}")

                    # Record trade outcome (hash may be None — graceful fallback)
                    asyncio.create_task(insert_trade_outcome(
                        signal_hash=signal_hash,
                        event_type=close_reason,
                        symbol=symbol,
                        side=side,
                        price_at_event=current_price,
                        pnl_pct=current_pnl_pct,
                        size_remaining=0.0,
                    ))
                    continue

                # ══ CHECK 2: TP1 HIT → move SL to breakeven (protect profit, keep full position)
                if tp1:
                    tp1_hit = (side == "long" and current_price >= tp1) or \
                              (side == "short" and current_price <= tp1)
                    if tp1_hit:
                        signal_hash = pos.get("signal_hash")

                        if not _observe_only:
                            hourly_vol_tp1 = current_vol / math.sqrt(8760)
                            atr_4h_tp1 = hourly_vol_tp1 * math.sqrt(4)
                            soft_be_offset = max(0.002, min(0.03, atr_4h_tp1 * 1.5))
                            be_sl = entry * (1 - soft_be_offset) if side == "long" else entry * (1 + soft_be_offset)
                            logger.info(
                                f"🎯 Pos #{pos_id} {symbol}: TP1 HIT! → SL→BE "
                                f"(pnl={current_pnl_pct:.2f}%, be_sl={be_sl:.6f})"
                            )
                            result_sl = await _send_bot(
                                "move_sl", symbol,
                                trace_id=signal_hash,
                                new_sl=be_sl,
                                reason=f"TP1 hit → ATR Soft BE ({soft_be_offset*100:.1f}%)",
                                diagnostics={"trigger": "tp1_hit", "pnl": current_pnl_pct,
                                             "tp1": tp1, "entry": entry, "be_sl": be_sl,
                                             "offset_pct": soft_be_offset*100},
                                _pos_id=pos_id,
                            )
                            if result_sl.get("ok"):
                                await _update_position_sl(pos_id, be_sl)

                        asyncio.create_task(insert_trade_outcome(
                            signal_hash=signal_hash,
                            event_type="tp1_hit",
                            symbol=symbol,
                            side=side,
                            price_at_event=current_price,
                            pnl_pct=current_pnl_pct,
                            size_remaining=size,
                        ))

                # ══ CHECK 3: TIMEOUT — stuck in Zone 0 (§10.9 Level A) ════════
                try:
                    opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                    hours_open = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
                except Exception as e:
                    logger.debug(f"Failed to parse opened_at_str '{opened_at_str}': {e}")
                    hours_open = 0

                if hours_open > POSITION_TIMEOUT_HOURS and tp_progress_pct < 20 and current_pnl_pct < 0.5:
                    signal_hash = pos.get("signal_hash")
                    if not _observe_only:
                        logger.info(
                            f"⏰ Pos #{pos_id} {symbol}: TIMEOUT ({hours_open:.1f}h in Zone 0) "
                            f"→ sending full_close to bot (pnl={current_pnl_pct:.2f}%)"
                        )
                        result = await _send_bot(
                            "full_close", symbol,
                            trace_id=signal_hash,
                            reason=f"TIMEOUT: {hours_open:.1f}h in Zone 0",
                            diagnostics={"trigger": "timeout", "hours_open": hours_open,
                                         "tp_progress": tp_progress_pct, "pnl": current_pnl_pct},
                            _pos_id=pos_id,
                        )
                        if not result.get("ok"):
                            logger.warning(f"⏰ Pos #{pos_id} {symbol}: TIMEOUT full_close rejected: {result}")
                            continue
                    else:
                        logger.info(
                            f"⏰ Pos #{pos_id} {symbol}: TIMEOUT ({hours_open:.1f}h) "
                            f"→ observe-only, marking closed in DB only"
                        )
                    await _close_position(pos_id, "timeout", current_pnl_pct, current_price,
                                          max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol)
                    asyncio.create_task(insert_trade_outcome(
                        signal_hash=signal_hash,
                        event_type="timeout",
                        symbol=symbol,
                        side=side,
                        price_at_event=current_price,
                        pnl_pct=current_pnl_pct,
                        size_remaining=0.0,
                    ))
                    continue

                # ══ LEVEL B: MC Forward Projection (GPU — every cycle) ═══════
                p_tp, p_sl, e_pnl_pct, full_e_pnl_pct, pnl_skewness, mc_var, mc_latency = await run_forward_projection(
                    symbol, side, size, entry, current_price, current_vol, sl, tp3,
                    equity=live_equity
                )

                # ══ CHECK 3.5: SMART TIME-DECAY EXIT — "Danger Zone" 1-4h (v0.9.3) ══
                # v0.14.0: Young Position Grace Period — let Midas's thesis play out.
                # In crypto, the biggest mistake is being shaken out of a winner.
                # For the first YOUNG_POSITION_HOURS, ONLY Hard SL Cap and exchange
                # SL/TP can close. No Apollo, no RC-3, no Zone Policy.
                _skip_active_gates = False
                if _observe_only:
                    _skip_active_gates = True  # v0.14.1: observe-only, no commands at all
                elif hours_open < YOUNG_POSITION_HOURS:
                    vol_cls = classify_volatility(current_vol)
                    logger.debug(
                        f"🛡️ [YOUNG POS] Pos #{pos_id} {symbol}: {hours_open:.1f}h < {YOUNG_POSITION_HOURS}h "
                        f"→ grace period, vol_class={vol_cls}, Midas SL={original_sl:.5f} protected"
                    )
                    _skip_active_gates = True

                #
                # Data-driven (373 closed trades):
                #   Winners (20/94): avg TP_progress=27.1%, avg max_pnl=2.69%
                #   Losers  (87/94): avg TP_progress=0.0%,  avg max_pnl=0.60%
                #
                # Logic: Force SL→breakeven for stale trades, BUT exempt trades
                # where momentum is still alive (MC or price action confirm).
                TIME_DECAY_START_H = 1.0   # Start of danger zone
                TIME_DECAY_END_H = 4.0     # End of danger zone (after 4h, trades recover)
                TIME_DECAY_TP_THRESHOLD = 15  # TP progress % below which trade is "stale"

                # v0.18.1: Promote momentum_alive to shared flag — used by CHECK 3.5 AND CHECK 4.
                # Conditions: position has demonstrable forward momentum (any one is enough).
                momentum_alive = (
                    tp_progress_pct >= TIME_DECAY_TP_THRESHOLD
                    or current_pnl_pct >= 2.0
                    or (p_tp > p_sl * 1.5 and p_tp > 0.4)
                )

                # ── v0.10.1: Yield-Aware Apollo — Funding Rate adjusts time-decay window ──
                # If extreme funding is IN our favor (we earn carry) → extend window (hold longer)
                # If extreme funding is AGAINST us (we pay carry) → shrink window (exit faster)
                try:
                    funding_rate, _ = await fetch_funding_and_oi(symbol)
                    is_long = (side == "long")
                    # Positive funding = longs pay shorts. Negative = shorts pay longs.
                    funding_in_our_favor = (
                        (is_long and funding_rate < -0.0005)    # We're long, shorts are paying us
                        or (not is_long and funding_rate > 0.0005)  # We're short, longs are paying us
                    )
                    funding_against_us = (
                        (is_long and funding_rate > 0.0005)     # We're long, we're paying shorts
                        or (not is_long and funding_rate < -0.0005)  # We're short, we're paying longs
                    )
                    if funding_in_our_favor:
                        TIME_DECAY_END_H = 6.0  # Extend: we're earning carry, hold longer
                        logger.debug(
                            f"💰 [APOLLO YIELD] Pos #{pos_id} {symbol}: "
                            f"funding={funding_rate:.6f} IN OUR FAVOR → extend window to {TIME_DECAY_END_H}h"
                        )
                    elif funding_against_us:
                        TIME_DECAY_END_H = 2.0  # Shrink: we're bleeding carry, exit faster
                        logger.debug(
                            f"💸 [APOLLO YIELD] Pos #{pos_id} {symbol}: "
                            f"funding={funding_rate:.6f} AGAINST US → shrink window to {TIME_DECAY_END_H}h"
                        )
                except Exception as e:
                    logger.debug(f"[APOLLO YIELD] Pos #{pos_id}: funding fetch failed, using default window: {e}")

                # v0.18.2: Patience Protocol — Apollo Bail converted to shadow-only.
                # Evidence (31 Mar — 1 Apr): 3/3 Apollo Bail exits were net-negative after
                # commissions (avg PnL +0.57%, but fees > profit). Kills potential big winners.
                # Step 2 (SL→soft BE) remains active — protects capital without closing.
                # Re-enable via APOLLO_BAIL_ENABLED=true env var if data warrants it.
                if (not _skip_active_gates
                        and TIME_DECAY_START_H <= hours_open <= TIME_DECAY_END_H
                        and current_pnl_pct > 0.0):

                    if not momentum_alive:
                        signal_hash = pos.get("signal_hash")

                        if config.APOLLO_BAIL_ENABLED:
                            last_bail = _apollo_bail_attempted.get(pos_id, 0)
                            bail_cooldown_ok = (time.time() - last_bail) >= APOLLO_RETRY_SEC
                            if bail_cooldown_ok:
                                _apollo_bail_attempted[pos_id] = time.time()

                                logger.info(
                                    f"🚀 [APOLLO BAIL] Pos #{pos_id} {symbol}: {hours_open:.1f}h stale → FULL CLOSE "
                                    f"(pnl={current_pnl_pct:.2f}%)"
                                )
                                result = await _send_bot(
                                    "full_close", symbol,
                                    trace_id=signal_hash,
                                    reason=f"APOLLO BAIL: {hours_open:.1f}h stale",
                                    diagnostics={"trigger": "apollo_bail", "hours_open": hours_open,
                                                 "tp_progress": tp_progress_pct, "pnl": current_pnl_pct},
                                    _pos_id=pos_id,
                                )
                                if result.get("ok"):
                                    await _close_position(
                                        pos_id, "apollo_full_exit", current_pnl_pct, current_price,
                                        max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol,
                                        detail=f"Apollo full exit: {hours_open:.1f}h stale"
                                    )
                                    asyncio.create_task(insert_trade_outcome(
                                        signal_hash=signal_hash,
                                        event_type="apollo_full_exit",
                                        symbol=symbol, side=side,
                                        price_at_event=current_price,
                                        pnl_pct=current_pnl_pct,
                                        size_remaining=0.0,
                                    ))
                                    continue
                        else:
                            logger.info(
                                f"👻 [APOLLO SHADOW] Pos #{pos_id} {symbol}: {hours_open:.1f}h stale, "
                                f"pnl={current_pnl_pct:.2f}%, Full_E={full_e_pnl_pct:+.2f}% "
                                f"→ would bail but DISABLED (Patience Protocol v0.18.2)"
                            )

                        # ── APOLLO Step 2: Orderbook Shielding + Soft Breakeven ──
                        # v0.10.1: Dynamic SL placed behind orderbook "walls"
                        # instead of static offset. Falls back to ATR-based offset if no wall.
                        #
                        # v0.13.1: ATR-adaptive SOFT BE — replaces fixed -0.8% with vol-scaled offset.
                        # Formula: offset = clamp(ATR(4h) × 1.5, 0.8%, 5%)
                        # For low-vol (BTC ~60% ann): offset ≈ 0.9% (≈ same as before)
                        # For mid-vol (ASTER ~120%):  offset ≈ 1.9% (2.4× wider)
                        # For high-vol (RIVER ~200%): offset ≈ 3.2% (4× wider)
                        # Capped at 5% to prevent absurdly wide SL on extreme vol coins.
                        #
                        # Runs EVERY cycle independently of Step 1.
                        # Built-in guards prevent spam:
                        #   - sl_already_better: stops after SL is set
                        #   - price_allows_sl: skips when price is wrong
                        hourly_vol = current_vol / math.sqrt(8760)  # annualized → hourly
                        atr_4h = hourly_vol * math.sqrt(4)           # 4h ATR estimate
                        soft_be_offset = max(0.008, min(0.05, atr_4h * 1.5))
                        soft_be_price = entry * (1 - soft_be_offset) if side == "long" else entry * (1 + soft_be_offset)
                        wall_found = False

                        try:
                            bids, asks = await fetch_orderbook_raw(symbol)
                            # For LONG: scan bids below current price for support walls
                            # For SHORT: scan asks above current price for resistance walls
                            levels = bids if side == "long" else asks
                            if levels and len(levels) >= 5:
                                # Filter levels in range: -0.3% to -2.0% from entry
                                # LONG: scan bids from entry*0.980 to entry*0.997
                                # SHORT: scan asks from entry*1.003 to entry*1.020
                                if side == "long":
                                    range_lo, range_hi = entry * 0.980, entry * 0.997
                                else:
                                    range_lo, range_hi = entry * 1.003, entry * 1.020

                                filtered = []
                                for price, qty in levels:
                                    if range_lo <= price <= range_hi:
                                        filtered.append((price, qty))

                                if filtered:
                                    volumes = [q for _, q in filtered]
                                    median_vol = sorted(volumes)[len(volumes) // 2]
                                    # Wall = level with volume >= 5x median
                                    WALL_MULTIPLIER = 5.0
                                    walls = [(p, v) for p, v in filtered if v >= median_vol * WALL_MULTIPLIER]

                                    if walls:
                                        if side == "long":
                                            # Best wall = closest to current price (highest bid)
                                            best_wall = max(walls, key=lambda x: x[0])
                                            # Place SL 2 ticks below wall
                                            tick_size = best_wall[0] * 0.0001  # ~0.01%
                                            soft_be_price = best_wall[0] - 2 * tick_size
                                        else:
                                            # For short: lowest ask wall
                                            best_wall = min(walls, key=lambda x: x[0])
                                            tick_size = best_wall[0] * 0.0001
                                            soft_be_price = best_wall[0] + 2 * tick_size
                                        wall_found = True
                                        # v0.14.0: Volatility-aware SL floor (was hardcoded -2%)
                                        # For SIREN (vol=10.44), -2% = 0.19× ATR → instant death
                                        # Now: dynamic floor = 2× soft_be_offset, capped 2-15%
                                        dynamic_max_loss = max(0.02, min(0.15, soft_be_offset * 2))
                                        max_loss_sl = entry * (1 - dynamic_max_loss) if side == "long" else entry * (1 + dynamic_max_loss)
                                        # CRITICAL: Never tighter than original Midas SL
                                        original_midas_sl = sl  # Midas SL from signal
                                        if original_midas_sl is not None and original_midas_sl > 0:
                                            if side == "long":
                                                max_loss_sl = min(max_loss_sl, original_midas_sl)
                                            else:
                                                max_loss_sl = max(max_loss_sl, original_midas_sl)
                                        if side == "long" and soft_be_price < max_loss_sl:
                                            soft_be_price = max_loss_sl
                                        elif side == "short" and soft_be_price > max_loss_sl:
                                            soft_be_price = max_loss_sl
                                        logger.info(
                                            f"🛡️ [ORDERBOOK SHIELD] Pos #{pos_id} {symbol}: "
                                            f"wall at {best_wall[0]:.5f} (vol={best_wall[1]:.1f}, "
                                            f"{best_wall[1]/median_vol:.0f}x median) → "
                                            f"SL={soft_be_price:.5f}"
                                        )
                        except Exception as e:
                            logger.debug(f"[ORDERBOOK SHIELD] Pos #{pos_id}: failed, using static -0.8%: {e}")

                        if not wall_found:
                            logger.debug(
                                f"[ORDERBOOK SHIELD] Pos #{pos_id} {symbol}: "
                                f"no wall found, using static Soft BE at {soft_be_price:.5f}"
                            )
                        current_sl_val = pos.get("current_sl")

                        sl_already_better = False
                        if current_sl_val is not None:
                            sl_val = float(current_sl_val)
                            if side == "long" and sl_val >= soft_be_price * 0.999: sl_already_better = True
                            if side == "short" and sl_val <= soft_be_price * 1.001: sl_already_better = True

                        # v0.14.0: Apollo Soft BE only activates when position is in profit
                        # Prevents tightening SL while position is underwater (SIREN fix)
                        price_allows_sl = False
                        if side == "long" and soft_be_price < current_price and current_pnl_pct > 0:
                            price_allows_sl = True
                        if side == "short" and soft_be_price > current_price and current_pnl_pct > 0:
                            price_allows_sl = True

                        if sl_already_better:
                            logger.debug(
                                f"⏳ [APOLLO SKIP] Pos #{pos_id} {symbol}: "
                                f"SL already at or better than Soft BE"
                            )
                        elif not price_allows_sl:
                            logger.debug(
                                f"⏳ [APOLLO WAIT] Pos #{pos_id} {symbol}: "
                                f"price {current_price:.5f} doesn't allow Soft BE at {soft_be_price:.5f}, "
                                f"will retry next cycle"
                            )
                        else:
                            # Use existing SL cooldown to avoid API spam
                            last_attempt = _sl_move_last_attempt.get(pos_id, 0)
                            cooldown_remaining = SL_MOVE_COOLDOWN_SEC - (time.time() - last_attempt)
                            if cooldown_remaining > 0:
                                logger.debug(
                                    f"⏳ [APOLLO COOLDOWN] Pos #{pos_id} {symbol}: "
                                    f"SL move retry in {cooldown_remaining:.0f}s"
                                )
                            else:
                                logger.info(
                                    f"🚀 [APOLLO SL] Pos #{pos_id} {symbol}: "
                                    f"Moving Moonbag SL to Soft BE (-0.8%): {soft_be_price:.5f}"
                                )
                                _sl_move_last_attempt[pos_id] = time.time()
                                result = await _send_bot(
                                    "move_sl", symbol,
                                    trace_id=signal_hash,
                                    new_sl=soft_be_price,
                                    reason=f"APOLLO SOFT BE (-{soft_be_offset*100:.1f}%): {hours_open:.1f}h stale",
                                    diagnostics={"trigger": "apollo_soft_be", "hours_open": hours_open,
                                                 "pnl": current_pnl_pct, "soft_be_offset": soft_be_offset,
                                                 "atr_4h": atr_4h, "current_vol": current_vol},
                                    _pos_id=pos_id,
                                )
                                if result.get("ok"):
                                    await _update_position_sl(pos_id, soft_be_price)
                                    _sl_move_last_attempt.pop(pos_id, None)
                                    logger.info(
                                        f"✅ [APOLLO OK] Pos #{pos_id} {symbol}: "
                                        f"Soft BE set at {soft_be_price:.5f}"
                                    )
                                else:
                                    logger.warning(
                                        f"❌ [APOLLO FAIL] Pos #{pos_id} {symbol}: "
                                        f"move_sl rejected: {result.get('error', 'unknown')}. "
                                        f"Will retry next cycle if price allows."
                                    )
                    else:
                        logger.info(
                            f"⏳ [TIME-DECAY EXEMPT] Pos #{pos_id} {symbol}: {hours_open:.1f}h in danger zone "
                            f"but momentum alive: TP={tp_progress_pct:.1f}%, PnL={current_pnl_pct:.2f}%, "
                            f"MC P_tp={p_tp:.3f}/P_sl={p_sl:.3f} → letting it ride"
                        )

                # ══ CHECK 4: Risk-Adjusted Exit — v0.18.1 "Let Winners Run" ══════
                # v0.18.1 reform: Uses full_e_pnl (all MC paths) instead of simplified
                # e_pnl (barrier-only). Respects momentum_alive from CHECK 3.5 and
                # skewness (fat-tail potential). Prevents killing breakout trades.
                #
                # Evidence: CUSDT closed at +1.38% → went to +15.2%;
                #           RIVER closed at +3.98% → went to +16%.
                # Old formula ignored 40-70% of MC paths and had no significance threshold.
                _e_pnl_would_close = (
                    not _skip_active_gates
                    and full_e_pnl_pct < config.E_PNL_EXIT_THRESHOLD
                    and current_pnl_pct > 0.5
                    and not momentum_alive
                    and pnl_skewness < config.E_PNL_SKEW_OVERRIDE
                )

                if not _skip_active_gates and current_pnl_pct > 0.5 and not _e_pnl_would_close:
                    if full_e_pnl_pct < 0:
                        logger.debug(
                            f"📊 [E_PNL HOLD] Pos #{pos_id} {symbol}: Full_E={full_e_pnl_pct:+.2f}% "
                            f"(old={e_pnl_pct:+.2f}%) Skew={pnl_skewness:+.2f} "
                            f"momentum={'alive' if momentum_alive else 'dead'} "
                            f"→ holding (threshold={config.E_PNL_EXIT_THRESHOLD}%)"
                        )

                if _e_pnl_would_close:
                    signal_hash = pos.get("signal_hash")
                    logger.info(
                        f"📉 Pos #{pos_id} {symbol}: Full_E_pnl={full_e_pnl_pct:.2f}% "
                        f"(old={e_pnl_pct:.2f}%) Skew={pnl_skewness:.2f} "
                        f"in profit ({current_pnl_pct:.2f}%) → FULL CLOSE"
                    )
                    result = await _send_bot(
                        "full_close", symbol,
                        trace_id=signal_hash,
                        reason=f"Full E_pnl={full_e_pnl_pct:.1f}% (thresh={config.E_PNL_EXIT_THRESHOLD}%) in profit",
                        diagnostics={"p_tp": p_tp, "p_sl": p_sl, "e_pnl": e_pnl_pct,
                                     "full_e_pnl": full_e_pnl_pct, "skew": pnl_skewness},
                        _pos_id=pos_id,
                    )
                    if result.get("ok"):
                        await _close_position(
                            pos_id, "e_pnl_full_exit", current_pnl_pct, current_price,
                            max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol,
                            detail=f"Full E_pnl={full_e_pnl_pct:.1f}% (skew={pnl_skewness:.1f}) exit"
                        )
                        asyncio.create_task(insert_trade_outcome(
                            signal_hash=signal_hash,
                            event_type="e_pnl_full_exit",
                            symbol=symbol, side=side,
                            price_at_event=current_price,
                            pnl_pct=current_pnl_pct,
                            size_remaining=0.0,
                        ))
                        continue

                # ══ v0.14.0: ATR TRAILING STOP — replaces RC-3 arbitrary % ════════
                # Professional crypto trailing: instead of arbitrary "DD > 50% of max",
                # trail by N × ATR where N depends on volatility class.
                # Trailing only activates AFTER position proves itself (max > 3%).
                # This gives volatile assets room to breathe through normal shakeouts.
                if not _skip_active_gates:
                    vol_class = classify_volatility(current_vol)
                    hourly_vol_trail = current_vol / math.sqrt(8760)
                    atr_4h_pct = hourly_vol_trail * math.sqrt(4) * 100  # ATR as % of price
                    atr_mult = ATR_TRAIL_MULTIPLIER.get(vol_class, 2.0)

                    if max_pnl_pct > TRAILING_ACTIVATION_PCT:
                        trail_distance_pct = atr_mult * atr_4h_pct
                        trailing_sl_pct = max(0, max_pnl_pct - trail_distance_pct)
                        if side == "long":
                            trailing_sl_price = entry * (1 + trailing_sl_pct / 100)
                        else:
                            trailing_sl_price = entry * (1 - trailing_sl_pct / 100)

                        current_sl_val = pos.get("current_sl") or 0
                        sl_should_move = False
                        # v0.14.0 GUARD: Only trail if we lock REAL profit (>0.5%), not breakeven.
                        # For EXTREME vol (SIREN), trail_distance=134% >> max_pnl → trailing_sl_pct=0
                        # → would snap SL to entry (breakeven) → exactly the mistake we want to avoid.
                        # In this case, Midas SL handles the protection.
                        if trailing_sl_pct > 0.5:
                            if side == "long" and trailing_sl_price > float(current_sl_val) and trailing_sl_price < current_price:
                                sl_should_move = True
                            elif side == "short" and trailing_sl_price < float(current_sl_val) and trailing_sl_price > current_price:
                                sl_should_move = True

                        if sl_should_move:
                            logger.info(
                                f"📈 [ATR TRAIL] Pos #{pos_id} {symbol}: max={max_pnl_pct:.2f}% "
                                f"trail={trail_distance_pct:.1f}% ({atr_mult}×ATR) → "
                                f"SL {float(current_sl_val):.5f} → {trailing_sl_price:.5f} "
                                f"(locks {trailing_sl_pct:.1f}% profit, vol_class={vol_class})"
                            )
                            signal_hash = pos.get("signal_hash")
                            result = await _send_bot(
                                "move_sl", symbol,
                                trace_id=signal_hash,
                                new_sl=trailing_sl_price,
                                reason=f"ATR TRAIL: max={max_pnl_pct:.1f}% trail={trail_distance_pct:.1f}% vol={vol_class}",
                                diagnostics={"trigger": "atr_trailing", "max_pnl": max_pnl_pct,
                                             "trail_dist": trail_distance_pct, "atr_mult": atr_mult,
                                             "vol_class": vol_class, "atr_4h_pct": atr_4h_pct},
                                _pos_id=pos_id,
                            )
                            if result.get("ok"):
                                await _update_position_sl(pos_id, trailing_sl_price)

                    # ══ R-MULTIPLE PROFIT LOCK (v0.14.0 → v0.16: SL-only, no partial close)
                    # R = distance from entry to Midas SL. Lock profit via SL moves at 2R and 3R.
                    if original_sl and original_sl > 0 and entry > 0:
                        initial_risk_pct = abs(entry - original_sl) / entry * 100
                        if initial_risk_pct > 0.1:
                            signal_hash = pos.get("signal_hash")

                            # 2R threshold: lock 1R profit via SL
                            if current_pnl_pct > initial_risk_pct * 2:
                                lock_1r_sl = entry + initial_risk_pct / 100 * entry if side == "long" else entry - initial_risk_pct / 100 * entry
                                logger.info(
                                    f"💰 [R-MULT 2R] Pos #{pos_id} {symbol}: PnL={current_pnl_pct:.2f}% > 2R "
                                    f"({initial_risk_pct*2:.1f}%) → SL to 1R lock ({lock_1r_sl:.6f})"
                                )
                                result_sl = await _send_bot(
                                    "move_sl", symbol,
                                    trace_id=signal_hash,
                                    new_sl=lock_1r_sl,
                                    reason=f"R-MULT 2R: lock 1R profit (PnL={current_pnl_pct:.1f}%)",
                                    diagnostics={"trigger": "r_multiple_2r", "pnl": current_pnl_pct,
                                                 "initial_risk": initial_risk_pct, "lock_sl": lock_1r_sl},
                                    _pos_id=pos_id,
                                )
                                if result_sl.get("ok"):
                                    await _update_position_sl(pos_id, lock_1r_sl)

                            # 3R threshold: lock 2R profit via SL
                            if current_pnl_pct > initial_risk_pct * 3:
                                lock_2r_sl = entry + initial_risk_pct * 2 / 100 * entry if side == "long" else entry - initial_risk_pct * 2 / 100 * entry
                                logger.info(
                                    f"💰💰 [R-MULT 3R] Pos #{pos_id} {symbol}: PnL={current_pnl_pct:.2f}% > 3R "
                                    f"({initial_risk_pct*3:.1f}%) → SL to 2R lock ({lock_2r_sl:.6f})"
                                )
                                result_sl = await _send_bot(
                                    "move_sl", symbol,
                                    trace_id=signal_hash,
                                    new_sl=lock_2r_sl,
                                    reason=f"R-MULT 3R: lock 2R profit (PnL={current_pnl_pct:.1f}%)",
                                    diagnostics={"trigger": "r_multiple_3r", "pnl": current_pnl_pct,
                                                 "initial_risk": initial_risk_pct, "lock_sl": lock_2r_sl},
                                    _pos_id=pos_id,
                                )
                                if result_sl.get("ok"):
                                    await _update_position_sl(pos_id, lock_2r_sl)

                # ══ LEVEL A: Zone Policy with adaptive thresholds ════════════
                zone = get_zone(tp_progress_pct)
                zone_action = "hold"

                if not _skip_active_gates and zone["dd_thresh"] is not None and max_pnl_pct > 0:
                    is_profit_zone = zone["action"] == "full_close"
                    current_regime_str = current_regime or "normal"
                    adaptive_factor = compute_adaptive_factor(p_tp, p_sl, is_profit_zone, regime=current_regime_str)
                    vol_modifier = compute_volume_modifier(volume_ratio)
                    adjusted_threshold = zone["dd_thresh"] * adaptive_factor * vol_modifier * regime_dd_sensitivity * heat_modifier

                    dd_pct_of_max = (drawdown_from_peak_pct / max_pnl_pct) * 100
                    if dd_pct_of_max > adjusted_threshold:
                        zone_action = zone["action"]

                        logger.info(
                            f"🧠 [ДУМАЛКА] Pos #{pos_id} {symbol}: {zone['name']} TRIGGERED! "
                            f"DD={dd_pct_of_max:.1f}% > adj_thresh={adjusted_threshold:.1f}% "
                            f"(base={zone['dd_thresh']}% × af={adaptive_factor:.2f} × vol={vol_modifier:.2f}) "
                            f"→ {zone_action} | "
                            f"MC: P_tp={p_tp:.3f} P_sl={p_sl:.3f} Full_E={full_e_pnl_pct:.2f}%"
                        )

                        if zone_action == "full_close":
                            signal_hash = pos.get("signal_hash")
                            result = await _send_bot(
                                "full_close", symbol,
                                trace_id=signal_hash,
                                reason=f"{zone['name']} DD={dd_pct_of_max:.1f}%>{adjusted_threshold:.1f}%",
                                zone=int(zone['name'].replace('Zone ', '')),
                                diagnostics={"p_tp": p_tp, "p_sl": p_sl, "e_pnl": e_pnl_pct,
                                             "dd_pct": dd_pct_of_max, "thresh": adjusted_threshold},
                                _pos_id=pos_id,
                            )
                            if result.get("ok"):
                                await _close_position(
                                    pos_id, "zone_full_exit", current_pnl_pct, current_price,
                                    max_pnl_pct, drawdown_from_peak_pct, tp_progress_pct, symbol=symbol,
                                    detail=f"{zone['name']} DD={dd_pct_of_max:.1f}% full exit"
                                )
                                asyncio.create_task(insert_trade_outcome(
                                    signal_hash=signal_hash,
                                    event_type="zone_full_exit",
                                    symbol=symbol, side=side,
                                    price_at_event=current_price,
                                    pnl_pct=current_pnl_pct,
                                    size_remaining=0.0,
                                ))
                                continue
                        elif zone_action == "sl_breakeven":
                            # v0.17.0: ATR-adaptive breakeven offset (was static 0.2%)
                            hourly_vol_be = current_vol / math.sqrt(8760)
                            atr_4h_be = hourly_vol_be * math.sqrt(4)
                            be_offset = max(0.002, min(0.03, atr_4h_be * 1.5))
                            be_sl = entry * (1 + be_offset) if side == "long" else entry * (1 - be_offset)
                            current_sl = pos.get("stop_loss")
                            # Check if SL already at or better than breakeven
                            if current_sl is not None and abs(float(current_sl) - be_sl) / entry < 0.0003:
                                pass # Already at breakeven
                            else:
                                signal_hash = pos.get("signal_hash")
                                result = await _send_bot(
                                    "move_sl", symbol,
                                    trace_id=signal_hash,
                                    new_sl=be_sl,
                                    reason=f"{zone['name']} DD={dd_pct_of_max:.1f}% -> SL breakeven+0.2%",
                                    zone=int(zone['name'].replace('Zone ', '')),
                                    diagnostics={"trigger": "sl_breakeven", "zone": zone['name'],
                                                 "dd_pct": dd_pct_of_max, "threshold": adjusted_threshold,
                                                 "pnl": current_pnl_pct, "entry": entry, "be_sl": be_sl},
                                    _pos_id=pos_id,
                                )
                                # RC-5: Update SL in RE's database
                                if result.get("ok"):
                                    await _update_position_sl(pos_id, be_sl)

                logger.debug(
                    f"Pos #{pos_id} {symbol} {side}: PnL={current_pnl_pct:.2f}% "
                    f"Max={max_pnl_pct:.2f}% DD={drawdown_from_peak_pct:.2f}% "
                    f"TP={tp_progress_pct:.1f}% {zone['name']}→{zone_action} "
                    f"vol_ratio={volume_ratio:.2f} MC={mc_latency:.0f}ms "
                    f"P_tp={p_tp:.3f} P_sl={p_sl:.3f}"
                )

                # ── v0.17.0: Keepalive — prevent bot fallback to own SL/TP logic ──
                if not _observe_only and not _skip_active_gates and config.DUMALKA_ACTIVE_MODE:
                    last_ka = _keepalive_last_sent.get(symbol, 0)
                    if time.time() - last_ka >= KEEPALIVE_INTERVAL_SEC:
                        current_sl_ka = pos.get("current_sl")
                        if current_sl_ka and float(current_sl_ka) > 0:
                            signal_hash_ka = pos.get("signal_hash")
                            await send_command_to_bot(
                                "move_sl", symbol,
                                trace_id=signal_hash_ka,
                                new_sl=float(current_sl_ka),
                                reason="keepalive",
                                _pos_id=pos_id,
                            )
                            _keepalive_last_sent[symbol] = time.time()
                            logger.debug(f"🏓 [KEEPALIVE] {symbol}: sent move_sl at current SL")

                # ── Save metrics to DB (BUG-2 FIX: also save zone) ────────
                zone_num = int(zone['name'].replace('Zone ', '')) if 'name' in zone else 0
                await pg_execute("""
                    UPDATE open_positions
                    SET current_price = ?, current_pnl_pct = ?,
                        max_pnl_pct = ?, drawdown_from_peak_pct = ?,
                        tp_progress_pct = ?, zone = ?
                    WHERE id = ?
                """, (
                    current_price, current_pnl_pct, max_pnl_pct,
                    drawdown_from_peak_pct, tp_progress_pct, zone_num, pos_id
                ))

                # ── Record ML snapshot (v0.8.4: enriched) ───────────────
                sym_funding = funding_cache.get(symbol, (0.0, 0.0))
                asyncio.create_task(insert_position_snapshot(
                    pos_id=pos_id, symbol=symbol, side=side,
                    current_price=current_price, entry_price=entry,
                    pnl_pct=current_pnl_pct, max_pnl_pct=max_pnl_pct,
                    drawdown_pct=drawdown_from_peak_pct, tp_progress_pct=tp_progress_pct,
                    hours_open=hours_open, zone=zone_num,
                    volatility=current_vol, volume_ratio=volume_ratio,
                    mc_p_tp=p_tp, mc_p_sl=p_sl, mc_var=mc_var,
                    full_e_pnl=full_e_pnl_pct, pnl_skewness=pnl_skewness,
                    signal_score=pos.get('initial_signal_score') or 0,
                    signal_hash=pos.get('signal_hash'),
                    action_taken=zone_action,
                    funding_rate=sym_funding[0],
                    oi_change_pct=sym_funding[1],
                    regime=current_regime,
                    spread_pct=spread_cache.get(symbol, 0.0),
                    trend_sum=trend_cache.get(symbol, 0.0),
                    btc_change_1h=btc_change,
                    rsi_14=rsi_cache.get(symbol),
                    orderbook_imbalance=ob_imbalance_cache.get(symbol),
                    long_short_ratio=lsr_cache.get(symbol),
                ))

                # v0.8.0: Retroactively fill future_pnl_1h/4h for old snapshots
                asyncio.create_task(update_snapshot_future_pnl(
                    pos_id=pos_id,
                    current_pnl_pct=current_pnl_pct,
                ))
            # v0.9.4 FIX-4: Report heartbeat
            _heartbeat["last_success"] = time.time()
            _heartbeat["cycle"] = cycle_count

        except Exception as e:
            logger.error(f"Error in position tracker loop: {e}", exc_info=True)
            _heartbeat["last_error"] = time.time()
            _heartbeat["error"] = str(e)[:100]


async def _close_position(pos_id, reason, pnl, price, max_pnl, dd, tp_prog, symbol=None, detail=None):
    """Helper to mark position as closed in DB. BUG-4 FIX: also sets close_reason_detailed."""
    # Clean up idempotency tracking
    _apollo_bail_attempted.pop(pos_id, None)  # v0.17.0: cleanup Apollo bail cooldown
    _no_trade_count.pop(pos_id, None)       # v0.13.1: cleanup phantom counter
    _bot_absent_streak.pop(pos_id, None)   # v0.18.3: roster desync streak

    # BUG-4: Generate detailed reason if not provided
    if not detail:
        detail = reason
    
    await pg_execute("""
        UPDATE open_positions
        SET status = 'closed', closed_at = ?, close_reason = ?,
            close_reason_detailed = ?,
            realized_pnl_pct = ?, current_price = ?,
            current_pnl_pct = ?, max_pnl_pct = ?,
            drawdown_from_peak_pct = ?, tp_progress_pct = ?,
            closed_fraction = 0
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(), reason,
        detail,
        pnl, price, pnl, max_pnl, dd, tp_prog, pos_id
    ))
    # v0.8.4: Backfill remaining future_pnl NULLs with final PnL
    asyncio.create_task(update_snapshot_future_pnl(pos_id, pnl))
    # Enrich with real USDT PnL from Bybit (background)
    if symbol:
        asyncio.create_task(_enrich_closed_pnl(pos_id, symbol))


async def admin_force_close_stale_position(pos_id: int, detail: str = "manual admin force close") -> dict:
    """
    Mark open/phantom row closed — operator recovery when DB diverged from exchange/bot.

    v0.18.3 (2026-04-02): Dashboard ghost OPEN (bot down, manual close, etc.).
    """
    row = await pg_fetch_one(
        "SELECT * FROM open_positions WHERE id = ? AND status IN ('open', 'phantom')",
        (pos_id,),
    )
    if not row:
        return {"ok": False, "error": f"No open/phantom position id={pos_id}"}
    pos = dict(row)
    symbol = pos["symbol"]
    try:
        px, _vol = await fetch_market_data(symbol)
        current_price = float(px)
    except Exception:
        current_price = float(pos.get("current_price") or 0)
    if current_price <= 0:
        return {"ok": False, "error": "Could not resolve current price"}
    entry = float(pos.get("entry_price") or 0)
    side = pos.get("side") or "long"
    if entry <= 0:
        return {"ok": False, "error": "Invalid entry_price"}
    if side == "long":
        pnl = ((current_price - entry) / entry) * 100
    else:
        pnl = ((entry - current_price) / entry) * 100
    max_pnl = max(pos.get("max_pnl_pct") or 0, pnl)
    dd = max(0, max_pnl - pnl)
    tp = pos.get("tp_progress_pct") or 0
    await _close_position(
        pos_id, "manual_desync", pnl, current_price,
        max_pnl, dd, tp, symbol=symbol, detail=detail,
    )
    signal_hash = pos.get("signal_hash")
    asyncio.create_task(insert_trade_outcome(
        signal_hash=signal_hash,
        event_type="manual_desync",
        symbol=symbol,
        side=side,
        price_at_event=current_price,
        pnl_pct=pnl,
        size_remaining=0.0,
    ))
    return {"ok": True, "pos_id": pos_id, "symbol": symbol, "pnl_pct": round(pnl, 4)}


async def _update_position_sl(pos_id: int, new_sl: float):
    """RC-5: Update SL in DB after breakeven move."""
    try:
        await pg_execute(
            "UPDATE open_positions SET current_sl = ? WHERE id = ?",
            (new_sl, pos_id)
        )
        logger.debug(f"Pos #{pos_id}: SL updated to {new_sl:.6f}")
    except Exception as e:
        logger.error(f"Failed to update SL for pos #{pos_id}: {e}")


async def _enrich_closed_pnl(pos_id: int, symbol: str):
    """Fetch real USDT PnL from Bybit and store in DB."""
    try:
        await asyncio.sleep(3)  # wait for Bybit to process the close
        records = await fetch_closed_pnl(symbol=symbol, limit=5)
        if records:
            # Sum all recent closed PnL for this symbol (may be partial closes)
            total_pnl = sum(r.get("closedPnl", 0) for r in records[:3])
            await pg_execute(
                "UPDATE open_positions SET realized_pnl_usdt = ? WHERE id = ?",
                (round(total_pnl, 6), pos_id)
            )
            logger.info(f"💰 Pos #{pos_id} {symbol}: real PnL from Bybit = ${total_pnl:.4f}")
    except Exception as e:
        logger.warning(f"Failed to enrich PnL for pos #{pos_id}: {e}")
