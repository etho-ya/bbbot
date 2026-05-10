"""
pytest tests for v0.7.0 features: Funding Rate/OI scoring, Regime Detector.

Pure unit tests — no GPU, no network required.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import compute_signal_score, _compute_funding_oi
from regime_detector import detect_regime, get_scoring_adjustments, get_zone_dd_sensitivity, REGIMES, _cache


# ══════════════════════════════════════════════════════════════════════════════
# SCORING: Funding + OI component
# ══════════════════════════════════════════════════════════════════════════════

def test_funding_extreme_long_penalizes():
    """Extreme positive funding on long position should reduce score."""
    base = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish")
    with_funding = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        funding_rate=0.002,  # extreme positive = crowded longs
    )
    assert with_funding["score"] < base["score"], (
        f"Extreme funding penalty not applied: base={base['score']} vs funded={with_funding['score']}"
    )
    assert "funding_oi" in with_funding["components"]


def test_funding_extreme_short_penalizes():
    """Extreme negative funding on short position should reduce score."""
    base = compute_signal_score("short", risk_reward=3.0, probability=60, win_rate=55, trend="bearish")
    with_funding = compute_signal_score(
        "short", risk_reward=3.0, probability=60, win_rate=55, trend="bearish",
        funding_rate=-0.002,  # extreme negative = crowded shorts
    )
    assert with_funding["score"] < base["score"]


def test_funding_favorable_helps():
    """Negative funding for long (shorts paying) should slightly help."""
    base = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish")
    favorable = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        funding_rate=-0.0008,  # shorts paying → good for longs
    )
    assert favorable["score"] >= base["score"], (
        f"Favorable funding should help: base={base['score']} vs favorable={favorable['score']}"
    )


def test_funding_neutral_no_penalty():
    """Near-zero funding should give neutral score."""
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        funding_rate=0.0001,  # normal, not extreme
    )
    fi = result["components"]["funding_oi"]
    assert 0.4 < fi < 0.8, f"Neutral funding should be ~0.6, got {fi}"


def test_funding_extreme_force_reduce():
    """Extreme funding should force-reduce an approve recommendation."""
    result = compute_signal_score(
        "long", risk_reward=4.0, probability=70, win_rate=65, trend="bullish",
        funding_rate=0.002,  # very crowded longs
    )
    assert result["recommendation"] in ("reduce", "reject"), (
        f"Expected force-reduce for extreme funding, got {result['recommendation']}"
    )


def test_funding_oi_function_direct():
    """Test _compute_funding_oi directly."""
    # No data → 0.5
    assert _compute_funding_oi(True, None, None) == 0.5

    # Crowded long → low
    score = _compute_funding_oi(True, 0.002, 0.0)
    assert score < 0.4, f"Crowded long should be < 0.4, got {score}"

    # Favorable for long → higher
    score = _compute_funding_oi(True, -0.001, 0.05)
    assert score > 0.6, f"Favorable long should be > 0.6, got {score}"


def test_oi_moderate_growth_bonus():
    """Moderate OI growth should give small bonus."""
    base = _compute_funding_oi(True, 0.0, 0.0)
    with_oi = _compute_funding_oi(True, 0.0, 0.05)
    assert with_oi > base, f"Moderate OI growth should increase score: {base} vs {with_oi}"


def test_oi_explosive_penalty():
    """Explosive OI > 20% should penalize."""
    base = _compute_funding_oi(True, 0.0, 0.05)
    explosive = _compute_funding_oi(True, 0.0, 0.25)
    assert explosive < base, f"Explosive OI should reduce score: {base} vs {explosive}"


# ══════════════════════════════════════════════════════════════════════════════
# SCORING: Backward compatibility
# ══════════════════════════════════════════════════════════════════════════════

def test_scoring_backward_compatible():
    """Without new params, scoring should produce reasonable results."""
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
    )
    assert 0.0 < result["score"] < 1.0
    assert result["recommendation"] in ("approve", "reduce", "reject")
    assert "funding_oi" in result["components"]
    # funding_oi should default to 0.5 (no data)
    assert result["components"]["funding_oi"] == 0.5


# ══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

def _make_klines(n=60, base_price=80000, trend="flat", volatility=0.01):
    """Generate synthetic klines for testing."""
    import random
    random.seed(42)
    klines = []
    price = base_price
    for i in range(n):
        if trend == "up":
            price *= (1 + volatility * 0.5)
        elif trend == "down":
            price *= (1 - volatility * 0.5)
        noise = random.gauss(0, volatility)
        o = price * (1 + noise)
        h = o * (1 + abs(noise) + volatility)
        l = o * (1 - abs(noise) - volatility)
        c = o * (1 + noise * 0.5)
        vol = random.uniform(100, 200)
        klines.append([i * 3600000, o, h, l, c, vol, vol * c])
    return klines


def test_regime_trending():
    """Strong uptrend should be detected as trending."""
    klines = _make_klines(60, trend="up", volatility=0.005)
    result = detect_regime(klines, volume_ratio=1.0, current_vol=0.5)
    assert result["regime"] == "trending", f"Expected trending, got {result}"


def test_regime_volatile():
    """High volatility should be detected."""
    klines = _make_klines(60, trend="flat", volatility=0.05)
    result = detect_regime(klines, volume_ratio=1.0, current_vol=2.5)
    assert result["regime"] == "volatile", f"Expected volatile, got {result}"


def test_regime_low_liquidity():
    """Very low volume should detect low_liquidity."""
    klines = _make_klines(60, trend="flat", volatility=0.01)
    result = detect_regime(klines, volume_ratio=0.1, current_vol=0.5)
    assert result["regime"] == "low_liquidity", f"Expected low_liquidity, got {result}"


def test_regime_ranging():
    """Flat market should detect ranging."""
    klines = _make_klines(60, trend="flat", volatility=0.001)
    result = detect_regime(klines, volume_ratio=1.0, current_vol=0.3)
    # Ranging: EMA separation small, ATR calm
    assert result["regime"] in ("ranging", "normal"), f"Expected ranging/normal, got {result}"


def test_regime_insufficient_data():
    """Short klines should return normal with low confidence."""
    result = detect_regime([], volume_ratio=1.0)
    assert result["regime"] == "normal"
    assert result["confidence"] < 0.5


def test_regime_dd_sensitivity():
    """Regime adjustments should produce valid dd_sensitivity values."""
    for regime_name, info in REGIMES.items():
        dd = info["dd_sensitivity"]
        assert 0.1 < dd < 5.0, f"Invalid dd_sensitivity for {regime_name}: {dd}"


def test_regime_scoring_adjustments():
    """get_scoring_adjustments should return valid multipliers."""
    adj = get_scoring_adjustments()
    assert isinstance(adj, dict)
    assert "trend_align" in adj
    assert "vol_ok" in adj
    # All values should be reasonable multipliers
    for k, v in adj.items():
        assert 0.1 < v < 5.0, f"Invalid adjustment for {k}: {v}"


def test_regime_scoring_integration():
    """Regime adjustments should actually change scores."""
    # Trending regime: boost trend_align
    trending_adj = {"trend_align": 1.4, "vol_ok": 0.7, "wr": 1.0, "prob": 1.0,
                    "rr": 1.0, "liquidity_ok": 1.0, "funding_oi": 1.0}

    base = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish")
    trending = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        regime_adjustments=trending_adj,
    )
    # In trending regime with bullish trend, trend_align is already 1.0
    # boosting its weight should slightly change the score
    assert trending["score"] != base["score"], (
        f"Regime should change score: base={base['score']} vs trending={trending['score']}"
    )
