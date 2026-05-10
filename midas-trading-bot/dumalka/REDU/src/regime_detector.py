"""
Regime Detector — Rule-based market regime classifier (v0.7.0).

Detects 4 market regimes from BTC + altcoin data:
  - TRENDING:       EMA separation > 1%, expanding ATR
  - RANGING:        EMA convergence < 0.5%, stable ATR
  - VOLATILE:       ATR > 2× 20-period average
  - LOW_LIQUIDITY:  Volume ratio < 0.3

Provides parameter adjustments for scoring weights and zone thresholds.
Cached for REGIME_CACHE_SECONDS (default 15 min).
"""

import time
import logging
from typing import Optional

logger = logging.getLogger("risk-engine.regime")

# ── Regime definitions ────────────────────────────────────────────────────────

REGIMES = {
    "trending":      {"label": "📈 Trending",       "dd_sensitivity": 0.85, "trend_weight_mult": 1.4, "vol_weight_mult": 0.7},
    "ranging":       {"label": "↔️ Ranging",         "dd_sensitivity": 1.2,  "trend_weight_mult": 0.6, "vol_weight_mult": 1.0},
    "volatile":      {"label": "🌊 High Volatility", "dd_sensitivity": 0.65, "trend_weight_mult": 0.8, "vol_weight_mult": 1.5},
    "low_liquidity": {"label": "🏜️ Low Liquidity",   "dd_sensitivity": 0.7,  "trend_weight_mult": 0.5, "vol_weight_mult": 1.3},
    "normal":        {"label": "⚡ Normal",          "dd_sensitivity": 1.0,  "trend_weight_mult": 1.0, "vol_weight_mult": 1.0},
}

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {
    "regime": "normal",
    "confidence": 0.5,
    "details": {},
    "updated_at": 0.0,
}

CACHE_TTL = 900  # 15 min default


def get_cached_regime() -> dict:
    """Get current cached regime without network call."""
    regime_name = _cache["regime"]
    regime_info = REGIMES.get(regime_name, REGIMES["normal"])
    return {
        "regime": regime_name,
        "confidence": _cache["confidence"],
        "label": regime_info["label"],
        "dd_sensitivity": regime_info["dd_sensitivity"],
        "trend_weight_mult": regime_info["trend_weight_mult"],
        "vol_weight_mult": regime_info["vol_weight_mult"],
        "details": _cache.get("details", {}),
        "age_seconds": time.time() - _cache["updated_at"],
    }


def get_scoring_adjustments() -> dict:
    """
    Get weight multipliers for scoring.py based on current regime.

    Returns dict with multipliers for each scoring component weight.
    All multipliers default to 1.0 (no change) for normal regime.
    """
    regime_name = _cache["regime"]
    info = REGIMES.get(regime_name, REGIMES["normal"])

    return {
        "trend_align": info["trend_weight_mult"],
        "vol_ok": info["vol_weight_mult"],
        # Weights for other components stay at 1.0
        "wr": 1.0,
        "prob": 1.0,
        "rr": 1.0,
        "liquidity_ok": 1.0,
        "funding_oi": 1.0,
    }


def get_zone_dd_sensitivity() -> float:
    """
    Get drawdown sensitivity multiplier for position_tracker zone thresholds.

    < 1.0 = tighter (close earlier), > 1.0 = looser (hold longer).
    """
    regime_name = _cache["regime"]
    return REGIMES.get(regime_name, REGIMES["normal"])["dd_sensitivity"]


# ── Core detection logic ──────────────────────────────────────────────────────

def detect_regime(
    klines: list,
    volume_ratio: float = 1.0,
    current_vol: float = 0.5,
) -> dict:
    """
    Classify market regime from kline data.

    Parameters
    ----------
    klines : list
        Hourly klines (chronological): [[ts, o, h, l, c, vol, turnover], ...]
        Need at least 60 candles for reliable detection.
    volume_ratio : float
        Current volume / 24h average volume.
    current_vol : float
        Annualized volatility from fetch_market_data.

    Returns
    -------
    dict with regime, confidence, details
    """
    if not klines or len(klines) < 30:
        return {"regime": "normal", "confidence": 0.3, "details": {"reason": "insufficient_data"}}

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]

    # ── ATR (Average True Range) ──────────────────────────────────────────
    atr_values = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        atr_values.append(tr)

    if len(atr_values) < 20:
        return {"regime": "normal", "confidence": 0.3, "details": {"reason": "insufficient_atr"}}

    recent_atr = sum(atr_values[-5:]) / 5  # last 5 candles
    avg_atr = sum(atr_values[-20:]) / 20   # 20-candle average
    mid_price = closes[-1] if closes[-1] > 0 else 1.0
    atr_ratio = recent_atr / avg_atr if avg_atr > 0 else 1.0
    atr_pct = (avg_atr / mid_price) * 100  # ATR as % of price

    # ── EMA separation ────────────────────────────────────────────────────
    ema20 = _simple_ema(closes, 20)
    ema50 = _simple_ema(closes, 50)

    if ema20 is not None and ema50 is not None and mid_price > 0:
        ema_separation_pct = abs(ema20 - ema50) / mid_price * 100
    else:
        ema_separation_pct = 0.5  # unknown → assume ranging

    # ── Classification rules ──────────────────────────────────────────────
    regime = "normal"
    confidence = 0.5
    details = {
        "atr_ratio": round(atr_ratio, 3),
        "atr_pct": round(atr_pct, 4),
        "ema_sep_pct": round(ema_separation_pct, 4),
        "volume_ratio": round(volume_ratio, 3),
        "annual_vol": round(current_vol, 4),
    }

    # Priority 1: Low liquidity (most dangerous)
    if volume_ratio < 0.3:
        regime = "low_liquidity"
        confidence = 0.8
        details["trigger"] = "volume_ratio < 0.3"

    # Priority 2: High volatility
    elif atr_ratio > 2.0 or current_vol > 2.0:
        regime = "volatile"
        confidence = min(0.95, 0.6 + (atr_ratio - 2.0) * 0.2)
        details["trigger"] = f"atr_ratio={atr_ratio:.2f} or annual_vol={current_vol:.2f}"

    # Priority 3: Trending (clear direction)
    elif ema_separation_pct > 1.0 and atr_ratio > 0.8:
        regime = "trending"
        confidence = min(0.95, 0.5 + ema_separation_pct * 0.15)
        trend_dir = "up" if ema20 > ema50 else "down"
        details["trigger"] = f"ema_sep={ema_separation_pct:.2f}% {trend_dir}"
        details["trend_direction"] = trend_dir

    # Priority 4: Ranging (consolidation)
    elif ema_separation_pct < 0.5 and atr_ratio < 1.2:
        regime = "ranging"
        confidence = min(0.9, 0.5 + (0.5 - ema_separation_pct) * 0.5)
        details["trigger"] = f"ema_sep={ema_separation_pct:.2f}% < 0.5"

    # Fallback: normal
    else:
        details["trigger"] = "no clear regime pattern"

    details["regime"] = regime

    return {"regime": regime, "confidence": round(confidence, 3), "details": details}


async def update_regime(
    klines: list = None,
    volume_ratio: float = 1.0,
    current_vol: float = 0.5,
    force: bool = False,
) -> dict:
    """
    Update cached regime. Called periodically from position_tracker or webhook.

    Uses BTC klines for global regime detection.
    Skips update if cache is fresh (< CACHE_TTL seconds old).
    """
    global _cache

    if not force and (time.time() - _cache["updated_at"]) < CACHE_TTL:
        return get_cached_regime()

    if klines:
        result = detect_regime(klines, volume_ratio, current_vol)
    else:
        result = {"regime": "normal", "confidence": 0.3, "details": {"reason": "no_klines"}}

    old_regime = _cache["regime"]
    _cache["regime"] = result["regime"]
    _cache["confidence"] = result["confidence"]
    _cache["details"] = result.get("details", {})
    _cache["updated_at"] = time.time()

    if result["regime"] != old_regime:
        logger.info(
            f"🔄 Regime changed: {old_regime} → {result['regime']} "
            f"(confidence={result['confidence']:.2f})"
        )
    else:
        logger.debug(
            f"Regime unchanged: {result['regime']} "
            f"(confidence={result['confidence']:.2f})"
        )

    return get_cached_regime()


def _simple_ema(values: list[float], period: int) -> float | None:
    """Calculate EMA, return last value."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema
