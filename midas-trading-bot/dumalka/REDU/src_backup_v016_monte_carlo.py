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
