"""
Signal Quality Scoring — v0.14.2 Strategy Pattern Architecture.

Wraps the proven v0.8.x heuristic scoring logic in a Strategy pattern,
enabling future XGBoost/ML model drop-in without changing the caller.

Changelog:
  v0.18.1 (2026-04-02): Weight rebalance — RR demoted to 0.05 (was 0.17, inversely
      correlated), WR promoted to 0.30, prob to 0.25, trend_align to 0.17.
      Default win_rate/probability fallback 40%→30% to reduce false-positive bias.
  v0.16.1 (2026-03-31): Documented interaction with score correction backfill
      (main.py). When midas_win_rate or probability is NULL, data quality penalty
      (×0.85 per missing field) is applied. A bot-side regex bug caused 46% of
      signals to have NULL win_rate; corrected_score in DB reverses this penalty.
  v0.14.2: ACTIVE scorer (imported by main.py). Predictive analysis (408 trades):
      - Only `win_rate` component has predictive power (Δ=+0.088)
      - `risk_reward` is inversely correlated (Δ=-0.053)
      - Post-scoring repeat_signal_boost (+0.03) applied in main.py
  v0.9.0: Strategy Pattern (HeuristicScorer, future MLScorer)
  v0.8.0-v0.8.4: Original monolithic compute_signal_score

Usage:
    result = compute_signal_score(side="long", risk_reward=2.5, ...)
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

from config import config

logger = logging.getLogger("risk-engine.scoring")


# ─── Strategy Interface ────────────────────────────────────────

class ScoringStrategy(ABC):
    """Abstract scoring strategy. All scorers must implement this."""

    @abstractmethod
    def score(
        self,
        side: str,
        risk_reward: Optional[float] = None,
        probability: Optional[float] = None,
        win_rate: Optional[float] = None,
        trend: Optional[str] = None,
        trend_strength: Optional[float] = None,
        volume_level: Optional[str] = None,
        market_vol: float = 0.5,
        spread_pct: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        multi_tf_trends: Optional[dict] = None,
        funding_rate: Optional[float] = None,
        oi_change_pct: Optional[float] = None,
        regime_adjustments: Optional[dict] = None,
    ) -> dict:
        """Compute score and return dict with: score, recommendation, is_countertrend, components, kelly_f"""
        ...


# ─── Heuristic Scorer (v0.8.x logic, EXACT copy) ──────────────

class HeuristicScorer(ScoringStrategy):
    """
    Production-proven heuristic scoring.
    This is the production v0.14.2 Active Mode heuristic scorer.
    It evaluates market signals (Probability, WR) against live Market Data 
    (Funding, OI, Volatility, Spread) to output approve/reduce/reject decisions.
    """

    def score(
        self,
        side: str,
        risk_reward: Optional[float] = None,
        probability: Optional[float] = None,
        win_rate: Optional[float] = None,
        trend: Optional[str] = None,
        trend_strength: Optional[float] = None,
        volume_level: Optional[str] = None,
        market_vol: float = 0.5,
        spread_pct: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        multi_tf_trends: Optional[dict] = None,
        funding_rate: Optional[float] = None,
        oi_change_pct: Optional[float] = None,
        regime_adjustments: Optional[dict] = None,
    ) -> dict:
        """
        Evaluate and score a trade signal using v0.14.2 Active Mode heuristics.
        
        Returns a structured dictionary:
        - recommendation: 'approve' (>=0.60), 'reduce' (0.45-0.59), 'reject' (<0.45)
        - score: normalized float
        - is_countertrend: boolean
        """
        # ── Normalize side ──
        is_long = side.lower() in ("long", "buy")

        # ── Countertrend detection ──
        is_countertrend = False
        if trend:
            t = trend.lower()
            is_bear = "bear" in t or "медвеж" in t
            is_bull = "bull" in t or "бычий" in t or "бычь" in t
            if is_long and is_bear:
                is_countertrend = True
            elif not is_long and is_bull:
                is_countertrend = True

        # ── Component scoring (each 0-1) ──

        # Data completeness tracking
        _missing_components = []
        if not win_rate or win_rate <= 0:
            _missing_components.append("win_rate")
        if not probability or probability <= 0:
            _missing_components.append("probability")
        if not risk_reward or risk_reward <= 0:
            _missing_components.append("risk_reward")

        # Win-rate (default 30% for NULL — below force-reduce threshold of 35%)
        wr = min((win_rate or 30.0) / 100.0, 1.0)

        # Probability (default 30% for NULL — ensures reduce/reject for missing data)
        prob = min((probability or 30.0) / 100.0, 1.0)

        # Risk:Reward
        rr_raw = risk_reward or 1.5
        rr = min(rr_raw / 5.0, 1.0)

        # Trend alignment
        if multi_tf_trends:
            trend_align = self._compute_confluence(is_long, multi_tf_trends)
        else:
            trend_align = 0.0 if is_countertrend else 1.0

        # Volatility penalty
        vol_ok = max(0.2, 1.0 - market_vol / 3.0) if market_vol > 0 else 0.5

        # Liquidity
        if spread_pct is not None and spread_pct > 0:
            liquidity_ok = max(0.2, 1.0 - (spread_pct / 0.3))
        else:
            liquidity_ok = 0.7

        # Funding Rate + OI
        funding_oi = self._compute_funding_oi(is_long, funding_rate, oi_change_pct)

        # ── Base weights ──
        weights = {
            "wr":           0.30,
            "prob":         0.25,
            "rr":           0.05,
            "trend_align":  0.17,
            "vol_ok":       0.07,
            "liquidity_ok": 0.08,
            "funding_oi":   0.08,
        }

        # ── Apply regime adjustments ──
        if regime_adjustments:
            for key in weights:
                if key in regime_adjustments:
                    weights[key] *= regime_adjustments[key]
            total_w = sum(weights.values())
            if total_w > 0:
                for key in weights:
                    weights[key] /= total_w

        # ── Weighted composite ──
        component_values = {
            "wr": wr,
            "prob": prob,
            "rr": rr,
            "trend_align": trend_align,
            "vol_ok": vol_ok,
            "liquidity_ok": liquidity_ok,
            "funding_oi": funding_oi,
        }

        score = sum(weights[k] * component_values[k] for k in weights)

        # ── Data quality penalty ──
        data_quality_pct = round(100 * (3 - len(_missing_components)) / 3)
        if _missing_components:
            quality_factor = max(0.55, 1.0 - 0.15 * len(_missing_components))
            original_score = score
            score *= quality_factor
            logger.info(
                f"Data quality penalty: missing={_missing_components} "
                f"factor={quality_factor:.2f} score={original_score:.3f}->{score:.3f}"
            )

        # ── Recommendation ──
        if score < 0.45:
            recommendation = "reject"
        elif score < 0.60:
            recommendation = "reduce"
        else:
            recommendation = "approve"

        # ── Extra penalties ──
        if is_countertrend and (probability is not None) and probability < 40:
            recommendation = "reject"
            logger.info(
                f"Force-reject: countertrend + prob={probability}% (score={score:.3f})"
            )

        if (win_rate is not None) and win_rate < 35:
            if recommendation != "reject":
                recommendation = "reduce"
                logger.info(f"Force-reduce: win_rate={win_rate}% (score={score:.3f})")

        if (probability is not None) and probability < 30 and (risk_reward is not None) and risk_reward < 1.5:
            recommendation = "reject"
            logger.info(f"Force-reject: low prob={probability}% + low R:R={risk_reward} (score={score:.3f})")

        # Slippage force-reject
        if slippage_pct is not None and stop_loss_pct is not None and stop_loss_pct > 0:
            slippage_ratio = slippage_pct / stop_loss_pct
            if slippage_ratio > config.SLIPPAGE_REJECT_RATIO:
                recommendation = "reject"
                logger.info(
                    f"Force-reject: slippage={slippage_pct:.3f}% is "
                    f"{slippage_ratio*100:.0f}% of SL={stop_loss_pct:.3f}% "
                    f"(threshold={config.SLIPPAGE_REJECT_RATIO*100:.0f}%)"
                )

        # Extreme funding force-reduce
        if funding_rate is not None and abs(funding_rate) > 0.001:
            crowded_long = is_long and funding_rate > 0.001
            crowded_short = (not is_long) and funding_rate < -0.001
            if crowded_long or crowded_short:
                if recommendation == "approve":
                    recommendation = "reduce"
                    logger.info(
                        f"Force-reduce: extreme funding={funding_rate:.4f} "
                        f"({'longs crowded' if crowded_long else 'shorts crowded'}) "
                        f"(score={score:.3f})"
                    )

        components = {
            "wr": round(wr, 4),
            "prob": round(prob, 4),
            "rr": round(rr, 4),
            "trend_align": round(trend_align, 4),
            "vol_ok": round(vol_ok, 4),
            "liquidity_ok": round(liquidity_ok, 4),
            "funding_oi": round(funding_oi, 4),
        }

        # ── Kelly Sizing ──
        b = risk_reward if risk_reward and risk_reward > 0 else 1.0
        p = (win_rate or 50.0) / 100.0
        q = 1.0 - p
        f_star = (p * b - q) / b
        kelly_f = max(0.0, f_star) * config.KELLY_FRACTION

        logger.info(
            f"Signal score={score:.3f} rec={recommendation} "
            f"kelly_f={kelly_f:.3f} countertrend={is_countertrend} components={components}"
        )

        return {
            "score": round(score, 4),
            "recommendation": recommendation,
            "is_countertrend": is_countertrend,
            "components": components,
            "kelly_f": kelly_f,
            "data_quality_pct": data_quality_pct,
        }

    @staticmethod
    def _compute_funding_oi(
        is_long: bool,
        funding_rate: Optional[float] = None,
        oi_change_pct: Optional[float] = None,
    ) -> float:
        """
        Compute an alignment score (0.0 - 1.0) based on Perpetual Futures Funding Rates and Open Interest.
        Rewards trading against crowded consensus (e.g. going short when funding is extremely positive).
        """
        if funding_rate is None and oi_change_pct is None:
            return 0.5

        score = 0.6

        fr = funding_rate or 0.0
        if is_long:
            if fr > 0.001:
                score -= min(0.4, fr * 200)
            elif fr < -0.0005:
                score += min(0.2, abs(fr) * 100)
        else:
            if fr < -0.001:
                score -= min(0.4, abs(fr) * 200)
            elif fr > 0.0005:
                score += min(0.2, fr * 100)

        oi = oi_change_pct or 0.0
        if 0.02 < oi < 0.15:
            score += 0.1
        elif oi > 0.20:
            score -= 0.1
        elif oi < -0.10:
            score -= 0.05

        return max(0.0, min(1.0, round(score, 4)))

    @staticmethod
    def _compute_confluence(is_long: bool, multi_tf_trends: dict) -> float:
        """
        Score how well the intended trade direction aligns with underlying 15m/1h/4h macro trends.
        """
        target = "bullish" if is_long else "bearish"
        agreeing = 0
        total = 0
        for tf_label, direction in multi_tf_trends.items():
            if direction in ("bullish", "bearish"):
                total += 1
                if direction == target:
                    agreeing += 1
        if total == 0:
            return 0.5
        mapping = {0: 0.0, 1: 0.3, 2: 0.7, 3: 1.0}
        return mapping.get(agreeing, agreeing / total)


# ─── Active Strategy (module-level singleton) ──────────────────

_active_strategy: ScoringStrategy = HeuristicScorer()


def set_active_strategy(strategy: ScoringStrategy):
    """Switch the active scoring strategy (e.g., for A/B testing or ML deployment)."""
    global _active_strategy
    logger.info(f"Scoring strategy changed: {type(_active_strategy).__name__} -> {type(strategy).__name__}")
    _active_strategy = strategy


# ─── Public API (backward-compatible drop-in) ──────────────────

def compute_signal_score(
    side: str,
    risk_reward: Optional[float] = None,
    probability: Optional[float] = None,
    win_rate: Optional[float] = None,
    trend: Optional[str] = None,
    trend_strength: Optional[float] = None,
    volume_level: Optional[str] = None,
    market_vol: float = 0.5,
    spread_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    multi_tf_trends: Optional[dict] = None,
    funding_rate: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
    regime_adjustments: Optional[dict] = None,
) -> dict:
    """
    Public API — identical signature to v0.8.x compute_signal_score.
    Delegates to the active scoring strategy.
    """
    return _active_strategy.score(
        side=side,
        risk_reward=risk_reward,
        probability=probability,
        win_rate=win_rate,
        trend=trend,
        trend_strength=trend_strength,
        volume_level=volume_level,
        market_vol=market_vol,
        spread_pct=spread_pct,
        slippage_pct=slippage_pct,
        stop_loss_pct=stop_loss_pct,
        multi_tf_trends=multi_tf_trends,
        funding_rate=funding_rate,
        oi_change_pct=oi_change_pct,
        regime_adjustments=regime_adjustments,
    )
