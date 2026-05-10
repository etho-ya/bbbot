"""
Signal Quality Scoring — LEGACY (v0.8.x)

⚠️ This file is SUPERSEDED by scoring_v2.py (Strategy Pattern).
main.py imports from scoring_v2, NOT this file.
Kept for reference and backward compatibility.

v0.8.0: Added Observability & Audit Log. Added trace_id support.
v0.7.1: Added data quality penalty — incomplete Midas metadata reduces score.
v0.7.0: Added funding_oi component (7%) and regime-aware weight adjustments.
v0.6.0: Added liquidity_ok component (8%) and multi-timeframe confluence scoring.

Combines Midas metadata (R:R, probability, win-rate, trend)
with market volatility, liquidity, funding rate, OI,
and multi-timeframe analysis to produce a 0-1 quality score
and a recommendation: 'approve', 'reduce', or 'reject'.
"""

import logging
from typing import Optional

from config import config

logger = logging.getLogger("risk-engine.scoring")


def compute_signal_score(
    side: str,
    risk_reward: Optional[float] = None,
    probability: Optional[float] = None,
    win_rate: Optional[float] = None,
    trend: Optional[str] = None,
    trend_strength: Optional[float] = None,
    volume_level: Optional[str] = None,
    market_vol: float = 0.5,
    # ── v0.6.0: Liquidity params ──
    spread_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    # ── v0.6.0: Multi-timeframe ──
    multi_tf_trends: Optional[dict] = None,
    # ── v0.7.0: Funding Rate + OI ──
    funding_rate: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
    # ── v0.7.0: Regime adjustments ──
    regime_adjustments: Optional[dict] = None,
) -> dict:
    """
    Compute a composite signal quality score (0-1).

    Parameters
    ----------
    side : str
        "long" or "short" / "sell" / "buy"
    risk_reward : float, optional
        Risk-to-reward ratio (e.g. 2.6 means 1:2.6).
    probability : float, optional
        Midas probability 0-100.
    win_rate : float, optional
        Midas monthly win-rate 0-100.
    trend : str, optional
        Trend description, e.g. "strong_bear", "moderate_bull", "sideways".
    trend_strength : float, optional
        Trend strength 0-100.
    volume_level : str, optional
        "high", "medium", "low".
    market_vol : float
        Our computed annualized volatility.
    spread_pct : float, optional (v0.6.0)
        Bid-ask spread as % of mid price.
    slippage_pct : float, optional (v0.6.0)
        Estimated slippage for our trade size, as %.
    stop_loss_pct : float, optional (v0.6.0)
        Stop-loss distance as % of entry price (for slippage comparison).
    multi_tf_trends : dict, optional (v0.6.0)
        {"15m": "bullish"|"bearish"|"neutral", "1h": ..., "4h": ...}
    funding_rate : float, optional (v0.7.0)
        Current perpetual funding rate (e.g. 0.0001 = 0.01%).
    oi_change_pct : float, optional (v0.7.0)
        Open interest 24h change (0.05 = 5% growth).
    regime_adjustments : dict, optional (v0.7.0)
        Weight multipliers from regime_detector.get_scoring_adjustments().

    Returns
    -------
    dict with keys: score, recommendation, is_countertrend, components, kelly_f
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

    # v0.7.1: Track data completeness for quality penalty
    _missing_components = []
    if not win_rate or win_rate <= 0:
        _missing_components.append("win_rate")
    if not probability or probability <= 0:
        _missing_components.append("probability")
    if not risk_reward or risk_reward <= 0:
        _missing_components.append("risk_reward")

    # Win-rate: 80% → 0.8, 50% → 0.5, unknown → 0.4 (penalize missing data)
    wr = min((win_rate or 40.0) / 100.0, 1.0)

    # Probability: direct from Midas, unknown → 0.4 (penalize missing data)
    prob = min((probability or 40.0) / 100.0, 1.0)

    # Risk:Reward: normalize to 5.0 cap. R:R 5+ → 1.0, 1:1 → 0.2
    rr_raw = risk_reward or 1.5  # conservative default
    rr = min(rr_raw / 5.0, 1.0)

    # Trend alignment (v0.6.0: multi-timeframe confluence)
    if multi_tf_trends:
        trend_align = _compute_confluence(is_long, multi_tf_trends)
    else:
        # Fallback: binary trend (backward compatible)
        trend_align = 0.0 if is_countertrend else 1.0

    # Volatility penalty: high vol → lower score
    vol_ok = max(0.2, 1.0 - market_vol / 3.0) if market_vol > 0 else 0.5

    # Liquidity (v0.6.0): penalize high spread
    if spread_pct is not None and spread_pct > 0:
        # Spread 0% → 1.0, spread >= 0.3% → 0.2 (heavy penalty)
        liquidity_ok = max(0.2, 1.0 - (spread_pct / 0.3))
    else:
        liquidity_ok = 0.7  # no data → slight penalty (unknown liquidity)

    # Funding Rate + OI (v0.7.0): detect crowded trades
    funding_oi = _compute_funding_oi(is_long, funding_rate, oi_change_pct)

    # ── Base weights (v0.7.0) ──
    weights = {
        "wr":           0.25,
        "prob":         0.22,
        "rr":           0.17,
        "trend_align":  0.14,
        "vol_ok":       0.07,
        "liquidity_ok": 0.08,
        "funding_oi":   0.07,
    }

    # ── Apply regime adjustments (v0.7.0) ──
    if regime_adjustments:
        for key in weights:
            if key in regime_adjustments:
                weights[key] *= regime_adjustments[key]
        # Re-normalize weights to sum to 1.0
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

    # ── v0.7.1: Data quality penalty ──
    # Missing critical Midas metadata → reduce score by 15% per missing component
    # This prevents inflated scores from incomplete signals
    data_quality_pct = round(100 * (3 - len(_missing_components)) / 3)
    if _missing_components:
        quality_factor = max(0.55, 1.0 - 0.15 * len(_missing_components))
        original_score = score
        score *= quality_factor
        logger.info(
            f"Data quality penalty: missing={_missing_components} "
            f"factor={quality_factor:.2f} score={original_score:.3f}→{score:.3f}"
        )

    # ── Recommendation (calibrated from 64-trade backtest) ──
    if score < 0.45:
        recommendation = "reject"
    elif score < 0.60:
        recommendation = "reduce"
    else:
        recommendation = "approve"

    # ── Extra penalty for extreme cases ──
    # Countertrend + low probability is especially dangerous
    if is_countertrend and (probability is not None) and probability < 40:
        recommendation = "reject"
        logger.info(
            f"Force-reject: countertrend + prob={probability}% (score={score:.3f})"
        )

    # Very low win-rate should also force reject
    if (win_rate is not None) and win_rate < 35:
        if recommendation != "reject":
            recommendation = "reduce"
            logger.info(f"Force-reduce: win_rate={win_rate}% (score={score:.3f})")

    # Low probability + low R:R → reject
    if (probability is not None) and probability < 30 and (risk_reward is not None) and risk_reward < 1.5:
        recommendation = "reject"
        logger.info(f"Force-reject: low prob={probability}% + low R:R={risk_reward} (score={score:.3f})")

    # ── v0.6.0: Slippage force-reject ──
    if slippage_pct is not None and stop_loss_pct is not None and stop_loss_pct > 0:
        slippage_ratio = slippage_pct / stop_loss_pct
        if slippage_ratio > config.SLIPPAGE_REJECT_RATIO:
            recommendation = "reject"
            logger.info(
                f"Force-reject: slippage={slippage_pct:.3f}% is "
                f"{slippage_ratio*100:.0f}% of SL={stop_loss_pct:.3f}% "
                f"(threshold={config.SLIPPAGE_REJECT_RATIO*100:.0f}%)"
            )

    # ── v0.7.0: Extreme funding force-reduce ──
    if funding_rate is not None and abs(funding_rate) > 0.001:
        # Extreme funding: >0.1% — crowded trade
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
    # f* = (p * b - q) / b
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


def _compute_funding_oi(
    is_long: bool,
    funding_rate: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
) -> float:
    """
    Compute funding + OI score (0-1).

    Logic:
    - Extreme positive funding + long = crowded → 0.2 (penalty)
    - Extreme negative funding + short = crowded → 0.2 (penalty)
    - Moderate funding aligned with position = 0.7 (neutral-good)
    - OI growing moderately = bonus (+0.1)
    - OI exploding (>20%) = caution (-0.1)
    - No data → 0.5 (neutral, slight penalty for unknown)
    """
    if funding_rate is None and oi_change_pct is None:
        return 0.5  # no data → neutral

    score = 0.6  # baseline: slightly positive

    # ── Funding rate component ──
    fr = funding_rate or 0.0

    if is_long:
        if fr > 0.001:
            # Very positive funding = longs are crowded → penalty
            score -= min(0.4, fr * 200)  # 0.002 → -0.4
        elif fr < -0.0005:
            # Negative funding = shorts paying → good for longs
            score += min(0.2, abs(fr) * 100)
    else:
        if fr < -0.001:
            # Very negative funding = shorts are crowded → penalty
            score -= min(0.4, abs(fr) * 200)
        elif fr > 0.0005:
            # Positive funding = longs paying → good for shorts
            score += min(0.2, fr * 100)

    # ── OI component ──
    oi = oi_change_pct or 0.0

    if 0.02 < oi < 0.15:
        # Moderate OI growth = healthy market interest
        score += 0.1
    elif oi > 0.20:
        # Explosive OI growth = potential liquidation cascade
        score -= 0.1
    elif oi < -0.10:
        # OI declining rapidly = positions closing, momentum fading
        score -= 0.05

    return max(0.0, min(1.0, round(score, 4)))


def _compute_confluence(is_long: bool, multi_tf_trends: dict) -> float:
    """
    Compute trend confluence score from multi-timeframe analysis.

    Returns 0.0-1.0 based on how many timeframes agree with signal direction:
      3/3 → 1.0 (strong confluence)
      2/3 → 0.7 (partial confluence)
      1/3 → 0.3 (weak / divergence)
      0/3 → 0.0 (full counter-trend)
    """
    target = "bullish" if is_long else "bearish"
    agreeing = 0
    total = 0

    for tf_label, direction in multi_tf_trends.items():
        if direction in ("bullish", "bearish"):
            total += 1
            if direction == target:
                agreeing += 1
        # "neutral" doesn't count as agreement or disagreement

    if total == 0:
        return 0.5  # all neutral → ambiguous

    mapping = {0: 0.0, 1: 0.3, 2: 0.7, 3: 1.0}
    return mapping.get(agreeing, agreeing / total)
