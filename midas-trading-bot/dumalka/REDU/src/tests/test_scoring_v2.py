"""
pytest tests for v0.6.0 features: scoring updates, portfolio allocator, liquidity.

Pure unit tests — no GPU, no network required.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import compute_signal_score, _compute_confluence
from portfolio_allocator import check_portfolio_limits, classify_sector
from bybit import _ema


# ══════════════════════════════════════════════════════════════════════════════
# SCORING: Liquidity component
# ══════════════════════════════════════════════════════════════════════════════

def test_liquidity_penalty_reduces_score():
    """High spread should reduce the overall score."""
    base = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish")
    with_spread = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        spread_pct=0.25,  # high spread
    )
    assert with_spread["score"] < base["score"], (
        f"Spread penalty not applied: base={base['score']} vs spread={with_spread['score']}"
    )
    assert "liquidity_ok" in with_spread["components"]


def test_slippage_force_reject():
    """Slippage > 50% of SL distance should force reject."""
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        slippage_pct=0.3,     # 0.3% slippage
        stop_loss_pct=0.5,    # SL is 0.5% from entry → slippage is 60% of SL
    )
    assert result["recommendation"] == "reject", (
        f"Expected reject for high slippage ratio, got {result['recommendation']}"
    )


def test_no_slippage_no_penalty():
    """Zero spread/slippage should not cause rejection."""
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=65, win_rate=60, trend="bullish",
        spread_pct=0.0, slippage_pct=0.0, stop_loss_pct=0.5,
    )
    assert result["recommendation"] == "approve", (
        f"Expected approve with no slippage, got {result['recommendation']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCORING: Multi-timeframe confluence
# ══════════════════════════════════════════════════════════════════════════════

def test_confluence_all_agree():
    """3/3 timeframes matching signal → trend_align = 1.0."""
    trends = {"15m": "bullish", "1h": "bullish", "4h": "bullish"}
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        multi_tf_trends=trends,
    )
    assert result["components"]["trend_align"] == 1.0, (
        f"Expected 1.0 for full confluence, got {result['components']['trend_align']}"
    )


def test_confluence_partial():
    """1/3 timeframes matching → trend_align = 0.3."""
    trends = {"15m": "bearish", "1h": "bearish", "4h": "bullish"}
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        multi_tf_trends=trends,
    )
    assert result["components"]["trend_align"] == 0.3, (
        f"Expected 0.3 for 1/3 confluence, got {result['components']['trend_align']}"
    )


def test_confluence_none_agree():
    """0/3 timeframes matching → trend_align = 0.0."""
    trends = {"15m": "bearish", "1h": "bearish", "4h": "bearish"}
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
        multi_tf_trends=trends,
    )
    assert result["components"]["trend_align"] == 0.0, (
        f"Expected 0.0 for counter-trend, got {result['components']['trend_align']}"
    )


def test_confluence_function_direct():
    """Test _compute_confluence directly."""
    assert _compute_confluence(True, {"15m": "bullish", "1h": "bullish", "4h": "bullish"}) == 1.0
    assert _compute_confluence(True, {"15m": "bullish", "1h": "bullish", "4h": "bearish"}) == 0.7
    assert _compute_confluence(True, {"15m": "bearish", "1h": "bearish", "4h": "bullish"}) == 0.3
    assert _compute_confluence(True, {"15m": "bearish", "1h": "bearish", "4h": "bearish"}) == 0.0
    assert _compute_confluence(False, {"15m": "bearish", "1h": "bearish", "4h": "bearish"}) == 1.0


def test_backward_compatible_no_multitf():
    """Without multi_tf_trends, scoring should work the same as before."""
    result = compute_signal_score(
        "long", risk_reward=3.0, probability=60, win_rate=55, trend="bullish",
    )
    # trend_align should be 1.0 (binary: bullish + long = aligned)
    assert result["components"]["trend_align"] == 1.0
    assert result["recommendation"] in ("approve", "reduce", "reject")


# ══════════════════════════════════════════════════════════════════════════════
# PORTFOLIO ALLOCATOR
# ══════════════════════════════════════════════════════════════════════════════

def test_sector_classification():
    """Test symbol → sector mapping."""
    assert classify_sector("BTCUSDT") == "BTC_LIKE"
    assert classify_sector("ETHUSDT") == "ETH_LIKE"
    assert classify_sector("SOLUSDT") == "MAJOR_ALT"
    assert classify_sector("LINKUSDT") == "MAJOR_ALT"
    assert classify_sector("RIVERUSDT") == "ALT"
    assert classify_sector("ARCUSDT") == "ALT"


def test_portfolio_sector_limit():
    """Adding alt when sector at 50%+ of equity → blocked."""
    existing = [
        {"symbol": "RIVERUSDT", "side": "long", "size": 100, "entry_price": 50.0},   # $5000 (ALT)
        {"symbol": "ARCUSDT", "side": "long", "size": 200, "entry_price": 5.0},       # $1000 (ALT)
    ]
    result = check_portfolio_limits(
        candidate_symbol="PEPEUSDT",
        candidate_side="long",
        candidate_size=1000,
        candidate_price=0.01,   # $10 (ALT)
        equity=10000.0,
        existing_positions=existing,
    )
    # ALT sector already at $6000 = 60% → adding another should still be rejected
    # because new total would be $6010 > 50% ($5000)
    assert result["allowed"] == False or result["suggested_size_mult"] < 1.0, (
        f"Expected block/reduce for sector overexposure, got {result}"
    )


def test_portfolio_no_limits_empty():
    """Empty portfolio → always allowed."""
    result = check_portfolio_limits(
        candidate_symbol="BTCUSDT",
        candidate_side="long",
        candidate_size=0.01,
        candidate_price=70000.0,
        equity=10000.0,
    )
    assert result["allowed"] is True
    assert result["suggested_size_mult"] == 1.0


def test_portfolio_different_sectors():
    """Positions in different sectors should not trigger limits."""
    existing = [
        {"symbol": "BTCUSDT", "side": "long", "size": 0.05, "entry_price": 70000.0},  # $3500 BTC_LIKE
    ]
    result = check_portfolio_limits(
        candidate_symbol="ETHUSDT",
        candidate_side="long",
        candidate_size=0.5,
        candidate_price=3000.0,   # $1500 ETH_LIKE (15% of $10k equity)
        equity=10000.0,
        existing_positions=existing,
    )
    assert result["allowed"] is True
    assert result["suggested_size_mult"] == 1.0
    assert result["sector"] == "ETH_LIKE"


# ══════════════════════════════════════════════════════════════════════════════
# EMA helper
# ══════════════════════════════════════════════════════════════════════════════

def test_ema_basic():
    """EMA returns a float when enough data, None otherwise."""
    data = list(range(1, 101))  # 100 values
    assert _ema(data, 20) is not None
    assert _ema(data, 50) is not None
    assert _ema(data, 200) is None  # not enough data
    # EMA of rising values should be close to the end
    ema20 = _ema(data, 20)
    assert ema20 > 80  # should be biased towards recent values
