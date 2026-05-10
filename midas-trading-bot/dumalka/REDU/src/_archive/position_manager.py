"""
Position Manager — Phase 4C (Думалка Decision Engine)
═══════════════════════════════════════════════════════
4-level decision architecture from PROJECT_RISK_ENGINE_FULL.md §10.9:

  Level A: Zone-based Trailing Take-Profit (calibrated thresholds)
  Level B: MC Forward Projection → adaptive_factor adjusts zone thresholds
  Level C: Differential MC → optimal close fraction (30/50/100%)
  Level D: Portfolio Risk Override → force-close if portfolio VaR > limit

Shadow Mode: decisions are logged but NOT sent to Trading Bot.
Enable with POSITION_MANAGER_ENABLED=true in config.
"""
import asyncio
import logging
import time
import httpx
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict
from datetime import datetime, timezone

from config import config

logger = logging.getLogger("risk-engine.manager")


# ══════════════════════════════════════════════════════════════════════════════
# Data Models
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionState:
    """Snapshot of a position for decision-making."""
    pos_id: int
    signal_hash: Optional[str]
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    current_price: float
    current_pnl_pct: float
    max_pnl_pct: float
    drawdown_from_peak_pct: float
    tp_progress_pct: float
    hours_open: float
    # Zone info
    zone: int
    zone_name: str
    # MC Forward data
    mc_p_tp: float = 0.0
    mc_p_sl: float = 0.0
    mc_e_pnl: float = 0.0
    mc_var: float = 0.0
    # Market context
    volatility: float = 0.0
    volume_ratio: float = 1.0
    # Targets
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp3: Optional[float] = None
    # Original scoring
    initial_score: float = 0.0
    initial_recommendation: str = "unknown"


@dataclass
class ManagerDecision:
    """Output of the Position Manager decision pipeline."""
    action: str  # "hold", "partial_close", "full_close", "move_sl", "sl_breakeven"
    close_fraction: float = 0.0  # 0.0-1.0
    new_sl: Optional[float] = None
    reason: str = ""
    level: str = ""  # which level triggered: "A", "B", "C", "D"
    confidence: float = 0.0  # 0-1
    details: Dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# Level A: Zone Policy (calibrated thresholds from Phase 4A)
# ══════════════════════════════════════════════════════════════════════════════

# v0.8.6: Calibrated on 369 positions + 60K snapshots (2026-03-22).
# Synced with position_tracker.py DEFAULT_ZONE_POLICY.
ZONE_THRESHOLDS = {
    0: {"dd_thresh": None, "base_frac": 0.0,  "action": "hold"},
    1: {"dd_thresh": 40,   "base_frac": 0.0,  "action": "sl_breakeven"},
    2: {"dd_thresh": 30,   "base_frac": 0.25, "action": "partial_close"},
    3: {"dd_thresh": 25,   "base_frac": 0.55, "action": "partial_close"},
    4: {"dd_thresh": 15,   "base_frac": 0.90, "action": "partial_close"},
}


def level_a_zone_decision(pos: PositionState) -> ManagerDecision:
    """
    Level A: Zone-based exit policy.
    Checks if drawdown from peak exceeds zone threshold.
    """
    zone_config = ZONE_THRESHOLDS.get(pos.zone, ZONE_THRESHOLDS[0])
    dd_thresh = zone_config["dd_thresh"]

    # Zone 0: always hold (no drawdown threshold)
    if dd_thresh is None:
        return ManagerDecision(action="hold", reason="Zone 0: hold", level="A")

    # Calculate drawdown as % of max PnL
    if pos.max_pnl_pct <= 0:
        return ManagerDecision(action="hold", reason=f"{pos.zone_name}: no profit peak yet", level="A")

    dd_pct_of_max = (pos.drawdown_from_peak_pct / pos.max_pnl_pct) * 100

    if dd_pct_of_max > dd_thresh:
        action = zone_config["action"]
        close_frac = zone_config["base_frac"]

        # SL breakeven → calculate new SL
        new_sl = None
        if action == "sl_breakeven":
            new_sl = pos.entry_price

        return ManagerDecision(
            action=action,
            close_fraction=close_frac,
            new_sl=new_sl,
            reason=f"{pos.zone_name}: DD {dd_pct_of_max:.1f}% > thresh {dd_thresh}%",
            level="A",
            confidence=min(1.0, dd_pct_of_max / dd_thresh),
            details={"dd_pct_of_max": dd_pct_of_max, "threshold": dd_thresh}
        )

    return ManagerDecision(
        action="hold",
        reason=f"{pos.zone_name}: DD {dd_pct_of_max:.1f}% < thresh {dd_thresh}%",
        level="A"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Level B: MC Forward Projection → Adaptive Factor
# ══════════════════════════════════════════════════════════════════════════════

def level_b_mc_adjustment(pos: PositionState, decision_a: ManagerDecision) -> ManagerDecision:
    """
    Level B: Adjust zone thresholds using MC forward projection.
    
    adaptive_factor = clamp(P_tp / P_sl × sensitivity, 0.5, 2.0)
    - P_tp/P_sl > 1 → market favors us → relax thresholds (let it run)
    - P_tp/P_sl < 1 → market against us → tighten thresholds (exit sooner)
    
    Also applies volume_ratio modifier.
    """
    if pos.mc_p_sl <= 0:
        # No MC data → pass through Level A decision
        return decision_a

    # Compute adaptive factor
    sensitivity = 1.0
    adaptive_factor = max(0.5, min(2.0, (pos.mc_p_tp / pos.mc_p_sl) * sensitivity))

    # Volume modifier (§10.11)
    if pos.volume_ratio < 0.5:
        vol_modifier = 0.8  # low volume → tighten
    elif pos.volume_ratio > 3.0:
        vol_modifier = 0.7  # anomalous volume → tighten
    else:
        vol_modifier = 1.0

    combined_modifier = adaptive_factor * vol_modifier

    # Re-evaluate with adjusted threshold
    zone_config = ZONE_THRESHOLDS.get(pos.zone, ZONE_THRESHOLDS[0])
    dd_thresh = zone_config["dd_thresh"]

    if dd_thresh is None or pos.max_pnl_pct <= 0:
        return decision_a

    adjusted_thresh = dd_thresh * combined_modifier
    dd_pct_of_max = (pos.drawdown_from_peak_pct / pos.max_pnl_pct) * 100

    if dd_pct_of_max > adjusted_thresh:
        action = zone_config["action"]
        new_sl = pos.entry_price if action == "sl_breakeven" else None

        return ManagerDecision(
            action=action,
            close_fraction=zone_config["base_frac"],
            new_sl=new_sl,
            reason=(
                f"{pos.zone_name} [MC-adj]: DD {dd_pct_of_max:.1f}% > "
                f"adj_thresh {adjusted_thresh:.1f}% "
                f"(base={dd_thresh}% × af={adaptive_factor:.2f} × vol={vol_modifier:.2f})"
            ),
            level="B",
            confidence=min(1.0, dd_pct_of_max / adjusted_thresh),
            details={
                "adaptive_factor": adaptive_factor,
                "vol_modifier": vol_modifier,
                "adjusted_thresh": adjusted_thresh,
                "dd_pct_of_max": dd_pct_of_max,
                "mc_p_tp": pos.mc_p_tp,
                "mc_p_sl": pos.mc_p_sl,
            }
        )

    # MC says: chill, threshold not hit with adjustment
    return ManagerDecision(
        action="hold",
        reason=(
            f"{pos.zone_name} [MC-adj]: DD {dd_pct_of_max:.1f}% < "
            f"adj_thresh {adjusted_thresh:.1f}% "
            f"(af={adaptive_factor:.2f}, mc_p_tp={pos.mc_p_tp:.2f}, mc_p_sl={pos.mc_p_sl:.2f})"
        ),
        level="B",
        details={"adaptive_factor": adaptive_factor, "adjusted_thresh": adjusted_thresh}
    )


# ══════════════════════════════════════════════════════════════════════════════
# Level C: Differential MC → Optimal Close Fraction
# ══════════════════════════════════════════════════════════════════════════════

async def level_c_differential_mc(
    pos: PositionState,
    decision_ab: ManagerDecision,
    run_diff_mc_fn  # async fn(symbol, side, size, entry, price, vol) → (frac, latency_ms)
) -> ManagerDecision:
    """
    Level C: When Level A/B triggers partial_close, run Differential MC
    to find optimal close fraction (30%, 50%, or 100%).
    Only runs on trigger — not every cycle.
    """
    if decision_ab.action != "partial_close":
        return decision_ab

    try:
        optimal_frac, latency_ms = await run_diff_mc_fn(
            pos.symbol, pos.side, pos.size,
            pos.entry_price, pos.current_price, pos.volatility
        )

        return ManagerDecision(
            action="partial_close",
            close_fraction=optimal_frac,
            reason=f"{decision_ab.reason} | DiffMC → close {optimal_frac*100:.0f}% ({latency_ms:.0f}ms)",
            level="C",
            confidence=decision_ab.confidence,
            details={
                **decision_ab.details,
                "diff_mc_fraction": optimal_frac,
                "diff_mc_latency_ms": latency_ms,
            }
        )
    except Exception as e:
        logger.warning(f"Differential MC failed for {pos.symbol}: {e}, using base fraction")
        return decision_ab


# ══════════════════════════════════════════════════════════════════════════════
# Level D: Portfolio Risk Override
# ══════════════════════════════════════════════════════════════════════════════

def level_d_risk_override(
    pos: PositionState,
    decision_abc: ManagerDecision,
    portfolio_var: float,
    portfolio_var_limit: float,
) -> ManagerDecision:
    """
    Level D: If portfolio VaR exceeds the limit, force-close the position
    with highest VaR contribution, overriding all other decisions.
    """
    if portfolio_var <= portfolio_var_limit:
        return decision_abc

    # Portfolio VaR exceeded — override to full close
    return ManagerDecision(
        action="full_close",
        close_fraction=1.0,
        reason=(
            f"RISK OVERRIDE: Portfolio VaR {portfolio_var:.2%} > limit {portfolio_var_limit:.2%}. "
            f"Original decision was: {decision_abc.action} ({decision_abc.reason})"
        ),
        level="D",
        confidence=1.0,
        details={
            "portfolio_var": portfolio_var,
            "portfolio_var_limit": portfolio_var_limit,
            "overridden_action": decision_abc.action,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# E_pnl Check: Negative expected PnL while in profit → recommend close
# ══════════════════════════════════════════════════════════════════════════════

def check_negative_expectation(pos: PositionState) -> Optional[ManagerDecision]:
    """
    If MC says E[pnl] < 0 but we're currently in profit, recommend closing.
    This is a soft signal — can be overridden by zone policy.
    """
    if pos.mc_e_pnl < 0 and pos.current_pnl_pct > 0:
        return ManagerDecision(
            action="full_close",
            close_fraction=1.0,
            reason=(f"E[pnl] negative ({pos.mc_e_pnl:.2f}%) while in profit "
                    f"({pos.current_pnl_pct:.2f}%) → close recommended"),
            level="B",
            confidence=0.7,
            details={"mc_e_pnl": pos.mc_e_pnl, "current_pnl_pct": pos.current_pnl_pct}
        )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Main Decision Pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def evaluate_position(
    pos: PositionState,
    run_diff_mc_fn=None,
    portfolio_var: float = 0.0,
) -> ManagerDecision:
    """
    Run the full 4-level decision pipeline for a position.
    Returns the final ManagerDecision.
    """
    # Level A: Zone policy
    decision = level_a_zone_decision(pos)

    # Level B: MC adaptive adjustment
    decision = level_b_mc_adjustment(pos, decision)

    # E_pnl check: override if negative expectation while profitable
    e_pnl_check = check_negative_expectation(pos)
    if e_pnl_check and decision.action == "hold":
        decision = e_pnl_check

    # Level C: Differential MC (only on partial_close trigger)
    if run_diff_mc_fn and decision.action == "partial_close":
        decision = await level_c_differential_mc(pos, decision, run_diff_mc_fn)

    # Level D: Portfolio Risk Override
    decision = level_d_risk_override(
        pos, decision,
        portfolio_var=portfolio_var,
        portfolio_var_limit=config.PORTFOLIO_VAR_LIMIT,
    )

    return decision


# ══════════════════════════════════════════════════════════════════════════════
# Callback to Trading Bot
# ══════════════════════════════════════════════════════════════════════════════

async def send_manage_callback(pos: PositionState, decision: ManagerDecision):
    """
    Send position management command to Trading Bot.
    Only executes if POSITION_MANAGER_ENABLED=true.
    """
    if not config.POSITION_MANAGER_ENABLED:
        logger.info(
            f"[SHADOW] {pos.symbol} {pos.side} → {decision.action} "
            f"({decision.close_fraction*100:.0f}%) | {decision.reason}"
        )
        return False

    payload = {
        "signal_hash": pos.signal_hash,
        "symbol": pos.symbol,
        "side": pos.side,
        "action": decision.action,
        "close_fraction": decision.close_fraction,
        "new_sl": decision.new_sl,
        "reason": decision.reason,
        "level": decision.level,
        "confidence": decision.confidence,
        "secret": config.CALLBACK_SECRET,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(config.BOT_MANAGE_URL, json=payload)
            if resp.status_code == 200:
                logger.info(
                    f"[LIVE] Callback sent: {pos.symbol} → {decision.action} "
                    f"({decision.close_fraction*100:.0f}%) | status={resp.status_code}"
                )
                return True
            else:
                logger.warning(
                    f"[LIVE] Callback failed: {pos.symbol} → {decision.action} | "
                    f"status={resp.status_code} body={resp.text[:200]}"
                )
                return False
    except Exception as e:
        logger.error(f"[LIVE] Callback error for {pos.symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Logging helpers
# ══════════════════════════════════════════════════════════════════════════════

def format_decision_log(pos: PositionState, decision: ManagerDecision) -> str:
    """Format a human-readable decision log line."""
    mode = "LIVE" if config.POSITION_MANAGER_ENABLED else "SHADOW"
    emoji = {
        "hold": "⏳",
        "partial_close": "📉",
        "full_close": "🔒",
        "sl_breakeven": "🛡️",
        "move_sl": "↕️",
    }.get(decision.action, "❓")

    return (
        f"[{mode}] {emoji} {pos.symbol} {pos.side} | "
        f"PnL={pos.current_pnl_pct:+.2f}% Max={pos.max_pnl_pct:.2f}% "
        f"TP={pos.tp_progress_pct:.0f}% {pos.zone_name} | "
        f"→ {decision.action.upper()}"
        f"{f' {decision.close_fraction*100:.0f}%' if decision.close_fraction > 0 else ''}"
        f" [{decision.level}] {decision.reason}"
    )
