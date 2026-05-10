"""
Monte Carlo Risk Engine — GPU-accelerated (CuPy/Titan V) price path simulation.

Jump-diffusion model with configurable parameters for barrier-probability
estimation and full expected PnL computation.

Changelog:
  v0.18.1 (2026-04-02): "Let Winners Run" — full_e_pnl_pct (true E[PnL] across
      all paths), pnl_skewness (fat-tail detection), pnl_p75. Jump-diffusion
      params (lambda, mu, sigma) now configurable via function args.
  v0.17.0 (2026-03-26): Replaced VaR-based heuristic with path simulation.
"""

try:
    import cupy as cp
    _HAS_CUPY = True
except ImportError:
    import numpy as cp
    _HAS_CUPY = False

import logging
import numpy as np
from typing import Optional
from models import Portfolio, CandidateTrade, MarketData, RiskLimits, RiskResult

logger = logging.getLogger("risk-engine.mc")

# ── Detect GPU at module load ────────────────────────────────────────────────
_USE_GPU = False
if _HAS_CUPY:
    try:
        cp.cuda.Device(0).use()
        _gpu_name = cp.cuda.runtime.getDeviceProperties(0)["name"].decode()
        _USE_GPU = True
        logger.info(f"✅ GPU ACTIVE: {_gpu_name} — Monte Carlo will use CuPy FP64")
    except Exception as e:
        logger.warning(f"⚠️ CuPy imported but GPU init failed: {e}. Falling back to CPU (NumPy).")
else:
    logger.warning("⚠️ CuPy NOT installed. Monte Carlo running on CPU (NumPy). GPU is idle!")


def run_monte_carlo_risk(
    portfolio: Portfolio,
    candidate: Optional[CandidateTrade],
    market: MarketData,
    limits: RiskLimits,
    n_scenarios: int = 100_000,
    horizon_hours: float = 24.0,
    confidence: float = 0.99,
) -> RiskResult:
    """
    Monte Carlo VaR/CVaR engine.
    Uses GPU (CuPy FP64) if available, otherwise falls back to CPU (NumPy).
    """
    xp = cp if _USE_GPU else np

    # ── asset universe ──────────────────────────────────────────────
    symbols = list(market.prices.keys())
    symbol_to_idx = {s: i for i, s in enumerate(symbols)}
    num_assets = len(symbols)

    prices_xp = xp.array([market.prices[s] for s in symbols], dtype=xp.float64)
    vols_xp = xp.array([market.volatility[s] for s in symbols], dtype=xp.float64)

    dt = horizon_hours / 24.0 / 365.0
    sqrt_dt = xp.float64(np.sqrt(dt))

    # ── random scenarios ────────────────────────────────────────────
    Z = xp.random.randn(n_scenarios, num_assets).astype(xp.float64)

    if market.correlations:
        corr = np.eye(num_assets, dtype=np.float64)
        for s1, targets in market.correlations.items():
            for s2, val in targets.items():
                i, j = symbol_to_idx.get(s1), symbol_to_idx.get(s2)
                if i is not None and j is not None:
                    corr[i, j] = val
                    corr[j, i] = val
        L = xp.array(np.linalg.cholesky(corr), dtype=xp.float64)
        Z = Z @ L.T

    # 1. Linear GBM
    rel_changes = vols_xp * sqrt_dt * Z

    # 2. Add Poisson Jumps (Jump Diffusion model)
    lambda_jump = 2.0
    mu_jump = -0.05
    sigma_jump = 0.10

    jumps_count = xp.random.poisson(lambda_jump * dt, size=(n_scenarios, num_assets)).astype(xp.float64)
    jump_sizes = xp.random.normal(mu_jump, sigma_jump, size=(n_scenarios, num_assets)).astype(xp.float64)

    rel_changes += jumps_count * jump_sizes

    # ── portfolio P&L ───────────────────────────────────────────────
    total_pnl = xp.zeros(n_scenarios, dtype=xp.float64)

    for pos in portfolio.positions:
        idx = symbol_to_idx.get(pos.symbol)
        if idx is None: continue
        mult = 1.0 if pos.side.lower() in ("long", "buy") else -1.0
        total_pnl += mult * pos.size * pos.entry_price * rel_changes[:, idx]

    if candidate and candidate.symbol in symbol_to_idx:
        idx = symbol_to_idx[candidate.symbol]
        mult = 1.0 if candidate.side.lower() in ("long", "buy") else -1.0
        total_pnl += mult * candidate.size * market.prices[candidate.symbol] * rel_changes[:, idx]

    # ── statistics ──────────────────────────────────────────────────
    pnl = xp.asnumpy(total_pnl) if hasattr(xp, 'asnumpy') else total_pnl
    pnl.sort()

    alpha = confidence
    var_idx = int(n_scenarios * (1.0 - alpha))
    var_idx = max(0, min(var_idx, n_scenarios - 1))

    var_abs = -pnl[var_idx]
    cvar_abs = -pnl[:var_idx].mean() if var_idx > 0 else var_abs
    liq_prob = float((pnl < -portfolio.equity).sum()) / n_scenarios
    max_dd_abs = -pnl[0] if n_scenarios > 0 else 0.0

    eq = portfolio.equity
    var_rel = var_abs / eq
    cvar_rel = cvar_abs / eq
    dd_rel = max_dd_abs / eq

    approved = (
        var_rel <= limits.max_var
        and cvar_rel <= limits.max_cvar
        and liq_prob <= limits.max_liquidation_prob
    )

    return RiskResult(
        approved=approved,
        var=float(var_rel),
        cvar=float(cvar_rel),
        liquidation_prob=float(liq_prob),
        drawdown_estimate=float(dd_rel),
    )


def simulate_sl_tp_probability(
    symbol: str,
    side: str,
    current_price: float,
    sl_price: float,
    tp_price: float,
    volatility: float,
    n_scenarios: int = 50_000,
    horizon_hours: float = 24.0,
    n_steps: int = 48,
    lambda_jump: float = 2.0,
    mu_jump: float = -0.05,
    sigma_jump: float = 0.10,
) -> dict:
    """
    Run multi-step MC price paths and count which barrier (SL or TP)
    is breached first per scenario.

    v0.18.1 (2026-04-02): Full E[PnL] reform — "Let Winners Run"
      - Computes full_e_pnl_pct: true mathematical expectation across ALL paths
        (barrier-hit + terminal price for open paths). The old e_pnl_pct
        (P_tp*tp_dist - P_sl*sl_dist) ignored 40-70% of paths (p_neither).
      - Computes pnl_skewness: detects fat-tail upside (positive-skew trades).
      - Computes pnl_p75: 75th percentile PnL for tail analysis.
      - Jump-diffusion parameters now configurable via function args.

    v0.17.0 (2026-03-26): Replaced VaR-based heuristic with path simulation.

    Returns dict with p_tp, p_sl, p_neither, e_pnl_pct, full_e_pnl_pct,
    pnl_skewness, pnl_p75.
    """
    xp = cp if _USE_GPU else np

    dt = horizon_hours / 24.0 / 365.0 / n_steps
    sqrt_dt = xp.float64(np.sqrt(dt))

    is_long = side.lower() in ("long", "buy")

    prices_path = xp.full(n_scenarios, current_price, dtype=xp.float64)
    hit_sl = xp.zeros(n_scenarios, dtype=xp.bool_)
    hit_tp = xp.zeros(n_scenarios, dtype=xp.bool_)

    for step in range(n_steps):
        Z = xp.random.randn(n_scenarios).astype(xp.float64)
        rel_change = volatility * sqrt_dt * Z

        jump_count = xp.random.poisson(lambda_jump * dt, size=n_scenarios).astype(xp.float64)
        jump_size = xp.random.normal(mu_jump, sigma_jump, size=n_scenarios).astype(xp.float64)
        rel_change += jump_count * jump_size

        prices_path = prices_path * (1.0 + rel_change)

        still_open = (~hit_sl) & (~hit_tp)
        if is_long:
            hit_sl |= (prices_path <= sl_price) & still_open
            hit_tp |= (prices_path >= tp_price) & still_open
        else:
            hit_sl |= (prices_path >= sl_price) & still_open
            hit_tp |= (prices_path <= tp_price) & still_open

    # ── Convert to numpy for statistics ──
    if hasattr(xp, 'asnumpy'):
        hit_sl_np = xp.asnumpy(hit_sl)
        hit_tp_np = xp.asnumpy(hit_tp)
        final_prices_np = xp.asnumpy(prices_path)
    else:
        hit_sl_np = hit_sl
        hit_tp_np = hit_tp
        final_prices_np = prices_path

    n_sl = int(hit_sl_np.sum())
    n_tp = int(hit_tp_np.sum())
    n_neither = n_scenarios - n_sl - n_tp

    p_sl = n_sl / n_scenarios
    p_tp = n_tp / n_scenarios
    p_neither = n_neither / n_scenarios

    sl_dist_pct = abs(current_price - sl_price) / current_price * 100
    tp_dist_pct = abs(tp_price - current_price) / current_price * 100

    # Old formula (kept for backward-compat logging)
    e_pnl_pct = p_tp * tp_dist_pct - p_sl * sl_dist_pct

    # ── v0.18.1: Full E[PnL] — per-path PnL across ALL scenarios ──
    per_path_pnl = np.empty(n_scenarios, dtype=np.float64)
    if is_long:
        per_path_pnl[hit_tp_np] = tp_dist_pct
        per_path_pnl[hit_sl_np] = -sl_dist_pct
        neither_mask = (~hit_sl_np) & (~hit_tp_np)
        per_path_pnl[neither_mask] = (
            (final_prices_np[neither_mask] - current_price) / current_price * 100
        )
    else:
        per_path_pnl[hit_tp_np] = tp_dist_pct
        per_path_pnl[hit_sl_np] = -sl_dist_pct
        neither_mask = (~hit_sl_np) & (~hit_tp_np)
        per_path_pnl[neither_mask] = (
            (current_price - final_prices_np[neither_mask]) / current_price * 100
        )

    full_e_pnl_pct = float(np.mean(per_path_pnl))
    pnl_p75 = float(np.percentile(per_path_pnl, 75))

    std = float(np.std(per_path_pnl))
    if std > 1e-9:
        pnl_skewness = float(np.mean(((per_path_pnl - full_e_pnl_pct) / std) ** 3))
    else:
        pnl_skewness = 0.0

    logger.info(
        f"[MC SL/TP Sim] {symbol}: {n_scenarios} paths, {n_steps} steps, "
        f"P_tp={p_tp:.3f} P_sl={p_sl:.3f} P_neither={p_neither:.3f} "
        f"E_pnl={e_pnl_pct:+.2f}% Full_E={full_e_pnl_pct:+.2f}% "
        f"Skew={pnl_skewness:+.2f} P75={pnl_p75:+.2f}%"
    )

    return {
        "p_tp": float(p_tp),
        "p_sl": float(p_sl),
        "p_neither": float(p_neither),
        "e_pnl_pct": float(e_pnl_pct),
        "full_e_pnl_pct": full_e_pnl_pct,
        "pnl_skewness": pnl_skewness,
        "pnl_p75": pnl_p75,
        "n_sl": n_sl,
        "n_tp": n_tp,
        "n_neither": n_neither,
    }
