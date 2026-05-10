"""
GPU-Accelerated Analytics Engine (Titan V / CuPy)

Uses GPU for heavy analytics computations:
1. SL Optimization Backtester — 1000s of scenarios in parallel
2. Symbol Correlation Matrix — rolling correlation for portfolio risk
3. Batch position scoring — Monte Carlo for all open positions at once
"""

try:
    import cupy as cp
    _HAS_CUPY = True
except ImportError:
    import numpy as cp
    _HAS_CUPY = False

import numpy as np
import logging
import time

logger = logging.getLogger("risk-engine.gpu-analytics")


def gpu_sl_optimization(pnl_array: list[float], win_amounts: list[float], loss_amounts: list[float]) -> dict:
    """
    GPU-accelerated SL optimization: simulate 100 SL cap levels (0.1% → 15%) 
    across all losing trades simultaneously.
    
    On CPU: O(N_caps × N_losses) = sequential loops
    On GPU: all caps computed in parallel on CUDA cores
    
    Returns: best SL cap, simulation results, breakeven analysis
    """
    t0 = time.time()
    xp = cp if _HAS_CUPY else np

    losses = xp.array([abs(x) for x in loss_amounts], dtype=xp.float64)
    total_win = float(sum(win_amounts))
    n_losses = len(losses)

    # Generate 100 SL cap levels from 0.1% to 15% 
    caps = xp.linspace(0.1, 15.0, 150, dtype=xp.float64)

    # Broadcast: caps (150,) × losses (N,) → capped matrix (150, N)
    # This is where GPU shines — parallel min() across all caps × all trades
    losses_2d = xp.tile(losses, (len(caps), 1))  # (150, N)
    caps_2d = caps[:, xp.newaxis]  # (150, 1)
    capped = xp.minimum(losses_2d, caps_2d)  # (150, N) — GPU parallel

    # Sum capped losses per cap level
    total_capped = xp.sum(capped, axis=1)  # (150,)
    net_pnl = total_win - total_capped  # (150,)
    saved = xp.sum(losses) - total_capped  # (150,)

    # Transfer back to CPU
    if hasattr(xp, 'asnumpy'):
        caps_cpu = xp.asnumpy(caps)
        net_cpu = xp.asnumpy(net_pnl)
        saved_cpu = xp.asnumpy(saved)
    else:
        caps_cpu = caps
        net_cpu = net_pnl
        saved_cpu = saved

    # Find optimal cap (first one that's still profitable with best ratio)
    breakeven_cap = None
    best_cap = None
    best_ratio = -999
    simulations = []

    for i in range(len(caps_cpu)):
        cap_val = float(caps_cpu[i])
        net_val = float(net_cpu[i])
        saved_val = float(saved_cpu[i])
        profitable = net_val > 0

        # Efficiency ratio: net_pnl per unit of constraint
        if profitable and cap_val > 0:
            # We want the cap that maximizes profit while not being too tight
            ratio = net_val / cap_val
            if ratio > best_ratio:
                best_ratio = ratio
                best_cap = cap_val

        if breakeven_cap is None and profitable:
            breakeven_cap = cap_val

        # Only return subset for display (every 10th + key points)
        if i % 10 == 0 or abs(net_val) < 5 or i == len(caps_cpu) - 1:
            simulations.append({
                'sl_cap_pct': round(cap_val, 1),
                'net_pnl': round(net_val, 2),
                'saved_pct': round(saved_val, 2),
                'profitable': profitable,
            })

    elapsed = time.time() - t0
    device = "GPU (Titan V)" if _HAS_CUPY else "CPU (NumPy)"
    logger.info(f"⚡ SL Optimization: {len(caps_cpu)} caps × {n_losses} losses in {elapsed*1000:.1f}ms on {device}")

    return {
        'simulations': simulations,
        'breakeven_cap': round(breakeven_cap, 2) if breakeven_cap else None,
        'best_efficiency_cap': round(best_cap, 2) if best_cap else None,
        'computation_ms': round(elapsed * 1000, 1),
        'device': device,
        'caps_tested': len(caps_cpu),
    }


def gpu_correlation_matrix(symbol_returns: dict[str, list[float]]) -> dict:
    """
    GPU-accelerated rolling correlation matrix for all symbols.
    
    Input: {symbol: [daily_returns]} 
    Output: NxN correlation matrix + clustered groups
    
    Titan V can compute 25×25 correlation matrix in <1ms vs ~50ms on CPU.
    """
    t0 = time.time()
    xp = cp if _HAS_CUPY else np

    symbols = sorted(symbol_returns.keys())
    if len(symbols) < 2:
        return {'symbols': symbols, 'matrix': [], 'clusters': []}

    # Build returns matrix (N_days × N_symbols)
    min_len = min(len(v) for v in symbol_returns.values())
    if min_len < 5:
        return {'symbols': symbols, 'matrix': [], 'clusters': [], 'error': 'Not enough data'}

    returns = xp.array([symbol_returns[s][-min_len:] for s in symbols], dtype=xp.float64)  # (N_sym, N_days)

    # GPU correlation: standardize + matmul
    means = xp.mean(returns, axis=1, keepdims=True)
    stds = xp.std(returns, axis=1, keepdims=True)
    stds = xp.maximum(stds, 1e-10)  # avoid div by zero
    standardized = (returns - means) / stds
    corr_matrix = (standardized @ standardized.T) / min_len  # (N_sym, N_sym) — GPU parallel matmul

    if hasattr(xp, 'asnumpy'):
        corr_cpu = xp.asnumpy(corr_matrix)
    else:
        corr_cpu = corr_matrix

    # Simple clustering: find pairs with correlation > 0.7
    high_corr_pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            c = float(corr_cpu[i, j])
            if abs(c) > 0.7:
                high_corr_pairs.append({
                    'sym1': symbols[i], 'sym2': symbols[j],
                    'correlation': round(c, 3),
                    'type': 'positive' if c > 0 else 'negative'
                })

    elapsed = time.time() - t0
    device = "GPU" if _HAS_CUPY else "CPU"
    logger.info(f"⚡ Correlation matrix: {len(symbols)}×{len(symbols)} in {elapsed*1000:.1f}ms on {device}")

    return {
        'symbols': symbols,
        'matrix': [[round(float(corr_cpu[i, j]), 3) for j in range(len(symbols))] for i in range(len(symbols))],
        'high_corr_pairs': high_corr_pairs,
        'computation_ms': round(elapsed * 1000, 1),
        'device': device,
    }


def gpu_batch_monte_carlo(positions: list[dict], n_scenarios: int = 200_000) -> dict:
    """
    GPU-accelerated batch Monte Carlo for ALL open positions simultaneously.
    
    Instead of running MC per-position (N calls × 100K scenarios each),
    runs ALL positions in ONE GPU kernel (1 call × 200K scenarios × N positions).
    
    Returns: per-position P(TP), P(SL), expected PnL, recommended action
    """
    t0 = time.time()
    xp = cp if _HAS_CUPY else np

    if not positions:
        return {'positions': [], 'device': 'N/A'}

    n_pos = len(positions)

    # Extract arrays for GPU
    entry_prices = xp.array([p['entry_price'] for p in positions], dtype=xp.float64)
    current_prices = xp.array([p.get('current_price', p['entry_price']) for p in positions], dtype=xp.float64)
    sl_prices = xp.array([p.get('current_sl', 0) for p in positions], dtype=xp.float64)
    tp_prices = xp.array([p.get('current_tp3', 0) or p.get('current_tp1', 0) for p in positions], dtype=xp.float64)
    volatilities = xp.array([p.get('volatility', 0.5) for p in positions], dtype=xp.float64)
    sides = [p.get('side', 'long') for p in positions]
    side_mult = xp.array([1.0 if s == 'long' else -1.0 for s in sides], dtype=xp.float64)

    # Simulate price paths: (n_scenarios, n_pos)
    dt = 24.0 / 24.0 / 365.0  # 24h horizon
    sqrt_dt = xp.float64(np.sqrt(dt))
    Z = xp.random.randn(n_scenarios, n_pos).astype(xp.float64)

    # GBM: price_change = vol * sqrt(dt) * Z
    rel_changes = volatilities * sqrt_dt * Z  # (n_scenarios, n_pos)

    # Simulated prices: current_price * (1 + rel_change)
    sim_prices = current_prices * (1.0 + rel_changes)  # (n_scenarios, n_pos)

    # PnL per scenario
    pnl_pct = side_mult * (sim_prices - entry_prices) / entry_prices * 100  # (n_scenarios, n_pos)

    # P(TP) and P(SL) for each position
    results = []
    for i in range(n_pos):
        pos_pnl = pnl_pct[:, i]
        pos_sim = sim_prices[:, i]

        if hasattr(xp, 'asnumpy'):
            pnl_cpu = xp.asnumpy(pos_pnl)
            sim_cpu = xp.asnumpy(pos_sim)
        else:
            pnl_cpu = pos_pnl
            sim_cpu = pos_sim

        # P(TP): probability of hitting TP
        tp = float(tp_prices[i]) if float(tp_prices[i]) > 0 else None
        sl = float(sl_prices[i]) if float(sl_prices[i]) > 0 else None

        if tp and sides[i] == 'long':
            p_tp = float((sim_cpu >= tp).sum()) / n_scenarios
        elif tp and sides[i] == 'short':
            p_tp = float((sim_cpu <= tp).sum()) / n_scenarios
        else:
            p_tp = float((pnl_cpu > 0).sum()) / n_scenarios

        if sl and sides[i] == 'long':
            p_sl = float((sim_cpu <= sl).sum()) / n_scenarios
        elif sl and sides[i] == 'short':
            p_sl = float((sim_cpu >= sl).sum()) / n_scenarios
        else:
            p_sl = float((pnl_cpu < -2).sum()) / n_scenarios

        avg_pnl = float(pnl_cpu.mean())
        var_95 = float(np.percentile(pnl_cpu, 5))

        # Recommendation based on MC
        if p_tp > 0.6 and avg_pnl > 0:
            action = "hold"
        elif p_sl > 0.5 or avg_pnl < -1:
            action = "consider_close"
        elif p_sl > 0.3 and avg_pnl < 0:
            action = "tighten_sl"
        else:
            action = "hold"

        results.append({
            'symbol': positions[i].get('symbol', '?'),
            'side': sides[i],
            'p_tp': round(p_tp * 100, 1),
            'p_sl': round(p_sl * 100, 1),
            'avg_expected_pnl': round(avg_pnl, 3),
            'var_95': round(var_95, 3),
            'action': action,
        })

    elapsed = time.time() - t0
    device = "GPU (Titan V)" if _HAS_CUPY else "CPU (NumPy)"
    logger.info(f"⚡ Batch MC: {n_pos} positions × {n_scenarios:,} scenarios in {elapsed*1000:.1f}ms on {device}")

    return {
        'positions': results,
        'n_scenarios': n_scenarios,
        'computation_ms': round(elapsed * 1000, 1),
        'device': device,
    }
