"""
Tests for v0.17.0 critical fixes:
  1. MC simulate_sl_tp_probability — path-based SL/TP counting
  2. Scoring weight rebalance (rr: 0.17→0.05, wr: 0.25→0.30)
  3. NULL win_rate default (40→30)
  4. Zone 1 entry threshold (5%→3%)
  5. E_pnl exit threshold (1.5%→0.5%)
  6. Timeout conditional on PnL
  7. Apollo bail retry with cooldown
  8. ATR-adaptive Zone 1 breakeven
"""
import sys
import os
import math
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# 1. MC simulate_sl_tp_probability
# ══════════════════════════════════════════════════════════════════════════════

from core.monte_carlo import simulate_sl_tp_probability


def test_mc_sim_basic_structure():
    """simulate_sl_tp_probability returns correct dict keys."""
    result = simulate_sl_tp_probability(
        symbol="BTCUSDT", side="long",
        current_price=60000, sl_price=58000, tp_price=63000,
        volatility=0.6, n_scenarios=5000, n_steps=12,
    )
    assert "p_tp" in result
    assert "p_sl" in result
    assert "p_neither" in result
    assert "e_pnl_pct" in result
    assert "full_e_pnl_pct" in result, "v0.18.1: full_e_pnl_pct must be present"
    assert "pnl_skewness" in result, "v0.18.1: pnl_skewness must be present"
    assert "pnl_p75" in result, "v0.18.1: pnl_p75 must be present"
    assert abs(result["p_tp"] + result["p_sl"] + result["p_neither"] - 1.0) < 0.01


def test_mc_sim_probabilities_sum_to_one():
    """P_tp + P_sl + P_neither must equal 1.0."""
    result = simulate_sl_tp_probability(
        symbol="SOLUSDT", side="short",
        current_price=150, sl_price=160, tp_price=140,
        volatility=0.8, n_scenarios=10000, n_steps=24,
    )
    total = result["p_tp"] + result["p_sl"] + result["p_neither"]
    assert abs(total - 1.0) < 1e-6, f"Probabilities sum to {total}, not 1.0"


def test_mc_sim_tight_sl_high_p_sl():
    """Very tight SL relative to TP should give high P_sl."""
    result = simulate_sl_tp_probability(
        symbol="BTCUSDT", side="long",
        current_price=60000,
        sl_price=59700,   # 0.5% away
        tp_price=66000,   # 10% away
        volatility=0.6, n_scenarios=10000, n_steps=24,
    )
    assert result["p_sl"] > result["p_tp"], (
        f"Tight SL should give higher P_sl: P_sl={result['p_sl']:.3f} P_tp={result['p_tp']:.3f}"
    )


def test_mc_sim_tight_tp_high_p_tp():
    """Very close TP relative to SL should give high P_tp."""
    result = simulate_sl_tp_probability(
        symbol="BTCUSDT", side="long",
        current_price=60000,
        sl_price=54000,   # 10% away
        tp_price=60600,   # 1% away
        volatility=0.6, n_scenarios=10000, n_steps=24,
    )
    assert result["p_tp"] > result["p_sl"], (
        f"Tight TP should give higher P_tp: P_tp={result['p_tp']:.3f} P_sl={result['p_sl']:.3f}"
    )


def test_mc_sim_short_side():
    """Short side: SL above, TP below current price."""
    result = simulate_sl_tp_probability(
        symbol="ETHUSDT", side="short",
        current_price=3000,
        sl_price=3150,   # 5% above
        tp_price=2850,   # 5% below
        volatility=0.7, n_scenarios=10000, n_steps=24,
    )
    assert result["p_tp"] > 0.05, f"Short should have non-trivial P_tp: {result['p_tp']}"
    assert result["p_sl"] > 0.05, f"Short should have non-trivial P_sl: {result['p_sl']}"


def test_mc_sim_high_vol_more_hits():
    """Higher volatility should increase total barrier hits (fewer P_neither)."""
    low_vol = simulate_sl_tp_probability(
        symbol="BTCUSDT", side="long",
        current_price=60000, sl_price=57000, tp_price=63000,
        volatility=0.3, n_scenarios=10000, n_steps=24,
    )
    high_vol = simulate_sl_tp_probability(
        symbol="BTCUSDT", side="long",
        current_price=60000, sl_price=57000, tp_price=63000,
        volatility=1.5, n_scenarios=10000, n_steps=24,
    )
    assert high_vol["p_neither"] < low_vol["p_neither"], (
        f"High vol should have fewer 'neither': "
        f"high={high_vol['p_neither']:.3f} low={low_vol['p_neither']:.3f}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Scoring weight rebalance
# ══════════════════════════════════════════════════════════════════════════════

from scoring_v2 import compute_signal_score


def test_scoring_wr_weight_increased():
    """Higher win_rate should have MORE impact after rebalance."""
    low_wr = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=40, trend="bullish")
    high_wr = compute_signal_score("long", risk_reward=3.0, probability=60, win_rate=70, trend="bullish")
    delta = high_wr["score"] - low_wr["score"]
    assert delta > 0.05, f"WR delta should be significant: {delta:.4f}"


def test_scoring_rr_weight_reduced():
    """R:R should have LESS impact after rebalance (weight 0.17→0.05)."""
    low_rr = compute_signal_score("long", risk_reward=1.5, probability=60, win_rate=55, trend="bullish")
    high_rr = compute_signal_score("long", risk_reward=4.0, probability=60, win_rate=55, trend="bullish")
    delta = high_rr["score"] - low_rr["score"]
    assert delta < 0.05, f"R:R delta should be small after rebalance: {delta:.4f}"


def test_scoring_null_winrate_defaults_to_30():
    """NULL win_rate should default to 30% (was 40%), making reduce more likely."""
    result = compute_signal_score("long", risk_reward=2.5, probability=55, win_rate=None, trend="bullish")
    assert result["components"]["wr"] == 0.3, (
        f"NULL win_rate should default to 30/100=0.3, got {result['components']['wr']}"
    )


def test_scoring_null_probability_defaults_to_30():
    """NULL probability should default to 30% (was 40%)."""
    result = compute_signal_score("long", risk_reward=2.5, probability=None, win_rate=55, trend="bullish")
    assert result["components"]["prob"] == 0.3, (
        f"NULL probability should default to 30/100=0.3, got {result['components']['prob']}"
    )


def test_scoring_both_null_gets_reduce_or_reject():
    """Both win_rate and probability NULL should push toward reduce/reject."""
    result = compute_signal_score("long", risk_reward=2.5, win_rate=None, probability=None, trend="bullish")
    assert result["recommendation"] in ("reduce", "reject"), (
        f"Both NULL should give reduce/reject, got {result['recommendation']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. Zone policy: Zone 1 now starts at 3% (was 5%)
# ══════════════════════════════════════════════════════════════════════════════

from position_tracker import get_zone, DEFAULT_ZONE_POLICY


def test_zone1_starts_at_3_pct():
    """Zone 1 should start at 3% tp_progress (was 5%)."""
    zone = get_zone(3.5)
    assert zone["name"] == "Zone 1", f"3.5% should be Zone 1, got {zone['name']}"


def test_zone0_below_3_pct():
    """Below 3% should still be Zone 0."""
    zone = get_zone(2.5)
    assert zone["name"] == "Zone 0", f"2.5% should be Zone 0, got {zone['name']}"


def test_zone1_threshold_value():
    """Verify Zone 1 min_tp is 3."""
    z1 = [z for z in DEFAULT_ZONE_POLICY if z["name"] == "Zone 1"][0]
    assert z1["min_tp"] == 3, f"Zone 1 min_tp should be 3, got {z1['min_tp']}"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Position tracker constants
# ══════════════════════════════════════════════════════════════════════════════

from position_tracker import (
    POSITION_TIMEOUT_HOURS,
    YOUNG_POSITION_HOURS,
    KEEPALIVE_INTERVAL_SEC,
    APOLLO_RETRY_SEC,
)


def test_timeout_increased_to_24h():
    """Position timeout should be 24h (was 12h)."""
    assert POSITION_TIMEOUT_HOURS == 24, f"Timeout should be 24, got {POSITION_TIMEOUT_HOURS}"


def test_young_position_reduced_to_half_hour():
    """Young position grace should be 1.0h (raised from 0.5 in v0.18.2 Patience Protocol)."""
    assert YOUNG_POSITION_HOURS == 1.0, f"Grace should be 1.0, got {YOUNG_POSITION_HOURS}"


def test_keepalive_interval():
    """Keepalive should be 300s (5 min)."""
    assert KEEPALIVE_INTERVAL_SEC == 300, f"Keepalive should be 300s, got {KEEPALIVE_INTERVAL_SEC}"


def test_apollo_retry_cooldown():
    """Apollo retry cooldown should be 600s (10 min)."""
    assert APOLLO_RETRY_SEC == 600, f"Apollo retry should be 600s, got {APOLLO_RETRY_SEC}"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Adaptive factor and volume modifier (unchanged but verified)
# ══════════════════════════════════════════════════════════════════════════════

from position_tracker import compute_adaptive_factor, compute_volume_modifier


def test_adaptive_factor_trending():
    """Trending regime should allow higher cap (1.5)."""
    af = compute_adaptive_factor(0.6, 0.3, is_profit_zone=True, regime="trending")
    assert af <= 1.5, f"Trending cap exceeded: {af}"


def test_adaptive_factor_ranging():
    """Ranging regime should use tighter cap (0.9)."""
    af = compute_adaptive_factor(0.6, 0.3, is_profit_zone=True, regime="ranging")
    assert af <= 0.9, f"Ranging cap exceeded: {af}"


def test_volume_modifier_anomalous():
    """Volume > 3x should tighten (0.7x)."""
    vm = compute_volume_modifier(4.0)
    assert vm == 0.7


def test_volume_modifier_normal():
    """Normal volume should be 1.0x."""
    vm = compute_volume_modifier(1.5)
    assert vm == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# 6. Volatility classification (unchanged but verified)
# ══════════════════════════════════════════════════════════════════════════════

from position_tracker import classify_volatility


def test_volatility_classes():
    """Verify volatility classification boundaries."""
    assert classify_volatility(0.3) == "LOW"
    assert classify_volatility(0.8) == "STANDARD"
    assert classify_volatility(1.5) == "MID"
    assert classify_volatility(3.0) == "HIGH"
    assert classify_volatility(8.0) == "EXTREME"
