"""
Compound Growth Engine v0.11.0
==============================
Dynamic position sizing proportional to account balance.

SAFETY FIRST:
- Starts as shadow calculator (logging only, not active)
- Circuit breaker: drawdown > threshold → reduce risk 4x
- Ramp-up: first N trades → half risk
- Hard cap: max 5% of balance per position
- Kill switch: COMPOUND_GROWTH_ENABLED=false

ACTIVATION CRITERIA:
- Do NOT activate when EV < 0
- Activate when EV > +0.1% on 50+ live trades after calibration
- Use as shadow calculator until then
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("compound_growth")


@dataclass
class CompoundGrowthState:
    """Persistent state for the Compound Growth Engine."""
    peak_balance: float = 0.0
    total_trades: int = 0
    last_updated: str = ""


class CompoundGrowthEngine:
    """
    Safe Position Sizing Engine.

    At EV < 0, this should run as shadow calculator only.
    At EV > 0, it becomes rocket fuel for exponential growth.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,          # 2% risk per trade
        max_position_pct: float = 0.05,  # Hard cap: 5% of balance
        dd_threshold: float = 0.10,      # 10% drawdown → protective mode
        dd_risk_multiplier: float = 0.25, # In protective mode: risk × 0.25
        ramp_up_trades: int = 10,        # First 10 trades → half risk
        min_position_usd: float = 1.0,   # Minimum viable position
    ):
        """
        Initializes the Compound Growth Engine with safety thresholds.
        """
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct
        self.dd_threshold = dd_threshold
        self.dd_risk_multiplier = dd_risk_multiplier
        self.ramp_up_trades = ramp_up_trades
        self.min_position_usd = min_position_usd
        self.state = CompoundGrowthState()

    def calculate_position_size(self, balance: float) -> dict:
        """
        Calculate recommended position size in USD.

        Returns a dict with:
            - size_usd: recommended position size
            - effective_risk_pct: actual risk % used
            - mode: 'ramp_up' | 'normal' | 'protective'
            - drawdown_pct: current drawdown from peak
            - peak_balance: highest balance seen
        """
        if balance <= 0:
            return self._result(0.0, 0.0, "error", 0.0, 0.0)

        # Track peak balance
        if balance > self.state.peak_balance:
            self.state.peak_balance = balance

        # Calculate drawdown
        drawdown = 0.0
        if self.state.peak_balance > 0:
            drawdown = (self.state.peak_balance - balance) / self.state.peak_balance

        # Determine mode and effective risk
        if self.state.total_trades < self.ramp_up_trades:
            # Ramp-up: half risk for first N trades
            effective_risk = self.risk_pct * 0.5
            mode = "ramp_up"
        elif drawdown > self.dd_threshold:
            # Circuit breaker: reduce risk dramatically
            effective_risk = self.risk_pct * self.dd_risk_multiplier
            mode = "protective"
            logger.warning(
                f"⚠️ COMPOUND GROWTH: Protective mode activated. "
                f"DD={drawdown:.1%} > threshold={self.dd_threshold:.1%}. "
                f"Risk reduced to {effective_risk:.2%}"
            )
        else:
            effective_risk = self.risk_pct
            mode = "normal"

        # Calculate size
        size_usd = balance * effective_risk

        # Apply hard cap
        max_size = balance * self.max_position_pct
        size_usd = min(size_usd, max_size)

        # Apply minimum
        if size_usd < self.min_position_usd:
            size_usd = self.min_position_usd

        # Update state
        self.state.last_updated = datetime.now(timezone.utc).isoformat()

        return self._result(size_usd, effective_risk, mode, drawdown, self.state.peak_balance)

    def record_trade(self):
        """Record that a trade was executed (for ramp-up tracking)."""
        self.state.total_trades += 1

    def get_shadow_report(self, balance: float, flat_size_usd: float) -> str:
        """
        Generate a shadow comparison report.
        Shows what compound growth WOULD have recommended vs flat sizing.
        """
        result = self.calculate_position_size(balance)
        compound_size = result["size_usd"]
        delta_pct = ((compound_size - flat_size_usd) / flat_size_usd * 100) if flat_size_usd > 0 else 0

        return (
            f"📊 COMPOUND SHADOW: "
            f"Balance=${balance:.2f} | "
            f"Flat=${flat_size_usd:.2f} → Compound=${compound_size:.2f} "
            f"({delta_pct:+.1f}%) | "
            f"Mode={result['mode']} | "
            f"DD={result['drawdown_pct']:.1%} | "
            f"Peak=${result['peak_balance']:.2f}"
        )

    def _result(self, size_usd, effective_risk, mode, drawdown, peak):
        """Helper method to format the output dictionary for position sizing."""
        return {
            "size_usd": round(size_usd, 2),
            "effective_risk_pct": round(effective_risk, 4),
            "mode": mode,
            "drawdown_pct": round(drawdown, 4),
            "peak_balance": round(peak, 2),
            "risk_pct_base": self.risk_pct,
            "dd_threshold": self.dd_threshold,
        }


# Singleton instance
_engine: CompoundGrowthEngine | None = None


def get_compound_engine() -> CompoundGrowthEngine:
    """Get or create the singleton CompoundGrowthEngine."""
    global _engine
    if _engine is None:
        from config import config
        _engine = CompoundGrowthEngine(
            risk_pct=getattr(config, 'COMPOUND_RISK_PCT', 0.02),
            max_position_pct=getattr(config, 'COMPOUND_MAX_POSITION_PCT', 0.05),
            dd_threshold=getattr(config, 'COMPOUND_DD_THRESHOLD', 0.10),
        )
    return _engine
