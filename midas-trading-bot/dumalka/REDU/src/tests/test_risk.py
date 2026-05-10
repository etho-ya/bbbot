"""
pytest tests for the Risk Engine Monte Carlo core.

Uses small n_scenarios (10_000) with artificial data to verify:
  1. VaR/CVaR move in the right direction when position size grows
  2. Larger volatility → larger VaR
  3. Empty portfolio → near-zero risk
  4. Opposing positions partially cancel
  5. API models serialise correctly
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Portfolio, Position, CandidateTrade, MarketData, RiskLimits
from core.monte_carlo import run_monte_carlo_risk

N = 10_000  # small for speed in CI, still statistically meaningful


def _make_request(positions, candidate=None, vol=0.8, equity=10_000.0):
    symbols = set(p.symbol for p in positions)
    if candidate:
        symbols.add(candidate.symbol)
    prices = {s: 60_000.0 if "BTC" in s else 3_000.0 for s in symbols}
    vols = {s: vol for s in symbols}
    return (
        Portfolio(equity=equity, positions=positions),
        candidate,
        MarketData(prices=prices, volatility=vols),
        RiskLimits(),
    )


def test_empty_portfolio_zero_risk():
    """No positions → VaR ≈ 0."""
    port, cand, market, limits = _make_request(positions=[])
    r = run_monte_carlo_risk(port, cand, market, limits, n_scenarios=N)
    assert abs(r.var) < 1e-6
    assert abs(r.cvar) < 1e-6
    assert r.approved is True


def test_var_increases_with_size():
    """Doubling position size should roughly double VaR."""
    small = [Position(symbol="BTCUSDT", side="long", size=0.1, entry_price=60_000)]
    big = [Position(symbol="BTCUSDT", side="long", size=0.5, entry_price=60_000)]

    r_small = run_monte_carlo_risk(*_make_request(small), n_scenarios=N)
    r_big = run_monte_carlo_risk(*_make_request(big), n_scenarios=N)

    assert r_big.var > r_small.var * 1.5, f"big={r_big.var:.4f} small={r_small.var:.4f}"


def test_var_increases_with_volatility():
    """Higher σ → higher VaR."""
    pos = [Position(symbol="BTCUSDT", side="long", size=0.3, entry_price=60_000)]

    r_low = run_monte_carlo_risk(*_make_request(pos, vol=0.3), n_scenarios=N)
    r_high = run_monte_carlo_risk(*_make_request(pos, vol=1.2), n_scenarios=N)

    assert r_high.var > r_low.var * 2.0, f"high={r_high.var:.4f} low={r_low.var:.4f}"


def test_opposing_positions_reduce_risk():
    """Long + Short on the same asset should partially cancel."""
    one_side = [Position(symbol="BTCUSDT", side="long", size=0.3, entry_price=60_000)]
    hedged = [
        Position(symbol="BTCUSDT", side="long", size=0.3, entry_price=60_000),
        Position(symbol="BTCUSDT", side="short", size=0.2, entry_price=60_000),
    ]

    r_one = run_monte_carlo_risk(*_make_request(one_side), n_scenarios=N)
    r_hedged = run_monte_carlo_risk(*_make_request(hedged), n_scenarios=N)

    assert r_hedged.var < r_one.var, f"hedged={r_hedged.var:.4f} one={r_one.var:.4f}"


def test_candidate_adds_risk():
    """Adding a candidate trade should increase risk."""
    pos = [Position(symbol="BTCUSDT", side="long", size=0.2, entry_price=60_000)]
    cand = CandidateTrade(symbol="BTCUSDT", side="long", size=0.3)

    r_no = run_monte_carlo_risk(*_make_request(pos), n_scenarios=N)
    r_yes = run_monte_carlo_risk(*_make_request(pos, candidate=cand), n_scenarios=N)

    assert r_yes.var > r_no.var, f"with_cand={r_yes.var:.4f} without={r_no.var:.4f}"


def test_cvar_ge_var():
    """CVaR (Expected Shortfall) should always be ≥ VaR."""
    pos = [Position(symbol="BTCUSDT", side="long", size=0.5, entry_price=60_000)]
    r = run_monte_carlo_risk(*_make_request(pos), n_scenarios=N)
    assert r.cvar >= r.var - 1e-6, f"cvar={r.cvar:.4f} var={r.var:.4f}"


def test_approval_respects_max_cvar():
    """If max_cvar is very tight, trade should be rejected even if max_var is loose."""
    pos = [Position(symbol="BTCUSDT", side="long", size=1.0, entry_price=60_000)]
    port, cand, market, _ = _make_request(pos, vol=1.0)
    tight_limits = RiskLimits(max_var=1.0, max_cvar=0.001, max_liquidation_prob=1.0)

    r = run_monte_carlo_risk(port, cand, market, tight_limits, n_scenarios=N)
    assert r.approved is False, f"Should be rejected: cvar={r.cvar:.4f}"
