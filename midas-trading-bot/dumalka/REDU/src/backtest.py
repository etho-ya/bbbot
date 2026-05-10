"""
Backtest Module — Historical data generator for RL training.

Fetches historical klines from Bybit Proxy, generates synthetic signals
(EMA crossover + RSI + volume), scores them through the real scoring pipeline,
simulates position lifecycle (SL/TP/timeout), and stores results in DB.

Usage:
    python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 30
    python backtest.py --all --days 90
"""

import argparse
import asyncio
import hashlib
import logging
import random
import sys
import time
from datetime import datetime, timezone, timedelta

import httpx
import math
import numpy as np
import numpy as xp  # alias for compatibility
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, get_db_pool

GPU_AVAILABLE = False  # GPU не даёт ускорения для бэктеста (массивы слишком малы)

from config import config
from scoring import compute_signal_score

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest")

# ── Configuration ─────────────────────────────────────────────────────────────
POSITION_TIMEOUT_BARS = 12       # 12 hours (12 × 1h candles)
SL_ATR_MULT = 1.5                # SL = 1.5 × ATR-14
TP_RR_RATIOS = [1.0, 2.0, 3.0]  # TP1=1:1, TP2=1:2, TP3=1:3 of SL distance
MIN_SIGNAL_GAP_BARS = 3          # Minimum bars between signals (avoid clustering)
SIMULATED_SIZE_USDT = 100.0      # Simulated position size for USDT calculations

# All symbols actually traded by the live Bybit bot + major coins
DEFAULT_SYMBOLS = [
    # Real traded symbols (from live bot)
    "RENDERUSDT", "FARTCOINUSDT", "WIFUSDT", "AEROUSDT", "SYRUPUSDT",
    "GRASSUSDT", "RIVERUSDT", "ASTERUSDT", "HIPPOUSDT",
    # Major coins for breadth
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AAVEUSDT", "TAOUSDT",
    "NEARUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
]


# ══════════════════════════════════════════════════════════════════════════════
# 1. FETCH HISTORICAL KLINES
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_historical_klines(symbol: str, days: int = 30) -> list[list]:
    """
    Fetch 1h klines from Bybit Proxy.
    Each kline: [timestamp, open, high, low, close, volume, turnover]
    Returns klines in chronological order (oldest first).
    """
    limit = min(days * 24, 1000)  # Bybit max 1000
    url = f"{config.BYBIT_PROXY_URL}/klines/{symbol}?interval=60&limit={limit}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    klines = data.get("klines", data) if isinstance(data, dict) else data
    if not isinstance(klines, list) or len(klines) < 50:
        logger.warning(f"[{symbol}] Only {len(klines) if isinstance(klines, list) else 0} klines, need 50+")
        return []

    # Bybit returns newest-first, reverse to chronological
    klines.reverse()
    logger.info(f"[{symbol}] Fetched {len(klines)} klines ({days} days)")
    return klines


# ══════════════════════════════════════════════════════════════════════════════
# 2. TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def calc_ema(values: list[float], period: int) -> list[float]:
    """Calculate EMA series. Returns list of same length (NaN-padded at start)."""
    emas = [float("nan")] * len(values)
    if len(values) < period:
        return emas
    multiplier = 2.0 / (period + 1)
    emas[period - 1] = sum(values[:period]) / period  # SMA seed
    for i in range(period, len(values)):
        emas[i] = (values[i] - emas[i - 1]) * multiplier + emas[i - 1]
    return emas


def calc_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Calculate RSI series."""
    rsi = [50.0] * len(closes)  # neutral default
    if len(closes) < period + 1:
        return rsi

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def calc_atr(klines: list[list], period: int = 14) -> list[float]:
    """Calculate ATR series from klines [ts, o, h, l, c, v, t]."""
    atr = [0.0] * len(klines)
    if len(klines) < period + 1:
        return atr

    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2])
        l = float(klines[i][3])
        prev_c = float(klines[i - 1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    # Initial ATR = SMA of first `period` TRs
    if len(trs) >= period:
        atr[period] = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr[i + 1] = (atr[i] * (period - 1) + trs[i]) / period

    return atr


# ══════════════════════════════════════════════════════════════════════════════
# 3. SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_signals(klines: list[list]) -> list[dict]:
    """
    Generate synthetic Midas-like signals from historical klines.
    Uses EMA20/EMA50 crossover + RSI confirmation + volume filter.
    """
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    rsi = calc_rsi(closes, 14)
    atr = calc_atr(klines, 14)

    # Average volume (rolling 24h)
    avg_vol_24 = [0.0] * len(volumes)
    for i in range(24, len(volumes)):
        avg_vol_24[i] = sum(volumes[i - 24:i]) / 24

    signals = []
    last_signal_bar = -MIN_SIGNAL_GAP_BARS

    for i in range(51, len(klines) - POSITION_TIMEOUT_BARS - 1):
        # Skip if too close to last signal
        if i - last_signal_bar < MIN_SIGNAL_GAP_BARS:
            continue

        # Skip if indicators not ready
        if math.isnan(ema20[i]) or math.isnan(ema50[i]) or atr[i] == 0:
            continue

        side = None

        # ── EMA Crossover Detection ──
        prev_above = ema20[i - 1] > ema50[i - 1]
        curr_above = ema20[i] > ema50[i]

        if curr_above and not prev_above:
            # Golden cross → long candidate
            if rsi[i] < 70:  # Not overbought
                side = "long"
        elif not curr_above and prev_above:
            # Death cross → short candidate
            if rsi[i] > 30:  # Not oversold
                side = "short"

        # ── RSI Extreme Reversal Signals ──
        if side is None:
            if rsi[i] < 25 and rsi[i - 1] >= 25:
                side = "long"  # Oversold bounce
            elif rsi[i] > 75 and rsi[i - 1] <= 75:
                side = "short"  # Overbought reversal

        if side is None:
            continue

        # ── Volume Confirmation ──
        if avg_vol_24[i] > 0 and volumes[i] < avg_vol_24[i] * 0.8:
            continue  # Low volume, skip

        # ── Build Signal ──
        entry_price = closes[i]
        sl_distance = atr[i] * SL_ATR_MULT

        if side == "long":
            sl = entry_price - sl_distance
            tp1 = entry_price + sl_distance * TP_RR_RATIOS[0]
            tp2 = entry_price + sl_distance * TP_RR_RATIOS[1]
            tp3 = entry_price + sl_distance * TP_RR_RATIOS[2]
            trend = "moderate_bull" if ema20[i] > ema50[i] else "sideways"
        else:
            sl = entry_price + sl_distance
            tp1 = entry_price - sl_distance * TP_RR_RATIOS[0]
            tp2 = entry_price - sl_distance * TP_RR_RATIOS[1]
            tp3 = entry_price - sl_distance * TP_RR_RATIOS[2]
            trend = "moderate_bear" if ema20[i] < ema50[i] else "sideways"

        # Synthetic Midas metadata (realistic random ranges)
        rr = sl_distance * 2.0 / max(sl_distance, 0.0001)  # ~2.0 R:R
        prob = random.uniform(45, 75)
        wr = random.uniform(50, 70)
        trend_strength = random.uniform(40, 80)

        # Compute volatility from recent closes
        if len(closes[:i + 1]) >= 24:
            recent = closes[max(0, i - 167):i + 1]
            arr = xp.array(recent, dtype=xp.float64)
            log_rets = xp.diff(xp.log(arr))
            annual_vol = float(xp.std(log_rets) * float(xp.sqrt(xp.float64(8760))))
        else:
            annual_vol = 0.5

        signal_hash = hashlib.md5(
            f"bt_{klines[i][0]}_{side}_{entry_price}".encode()
        ).hexdigest()

        signals.append({
            "bar_index": i,
            "timestamp": int(klines[i][0]),
            "side": side,
            "entry_price": entry_price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr": rr,
            "probability": prob,
            "win_rate": wr,
            "trend": trend,
            "trend_strength": trend_strength,
            "annual_vol": annual_vol,
            "volume_level": "high" if volumes[i] > avg_vol_24[i] * 1.5 else "medium",
            "signal_hash": signal_hash,
            "rsi": rsi[i],
        })
        last_signal_bar = i

    logger.info(f"Generated {len(signals)} signals from {len(klines)} klines")
    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 4. SCORING (uses real pipeline)
# ══════════════════════════════════════════════════════════════════════════════

def score_signal(signal: dict) -> dict:
    """Run signal through the real compute_signal_score() pipeline."""
    result = compute_signal_score(
        side=signal["side"],
        risk_reward=signal["rr"],
        probability=signal["probability"],
        win_rate=signal["win_rate"],
        trend=signal["trend"],
        trend_strength=signal["trend_strength"],
        volume_level=signal["volume_level"],
        market_vol=signal["annual_vol"],
    )
    signal["score"] = result["score"]
    signal["recommendation"] = result["recommendation"]
    signal["score_components"] = result.get("components", {})
    signal["is_countertrend"] = result.get("is_countertrend", False)
    return signal


# ══════════════════════════════════════════════════════════════════════════════
# 5. POSITION SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def simulate_position(signal: dict, klines: list[list]) -> dict:
    """
    Simulate position lifecycle using future klines.
    Checks each bar's high/low for SL/TP hits, tracks max PnL and drawdown.
    """
    bar_start = signal["bar_index"] + 1
    bar_end = min(bar_start + POSITION_TIMEOUT_BARS, len(klines))
    entry = signal["entry_price"]
    is_long = signal["side"] == "long"

    max_pnl_pct = 0.0
    min_pnl_pct = 0.0
    close_reason = "timeout"
    close_price = entry
    close_bar = bar_end - 1
    tp_progress_pct = 0.0

    for i in range(bar_start, bar_end):
        high = float(klines[i][2])
        low = float(klines[i][3])
        bar_close = float(klines[i][4])

        if is_long:
            pnl_at_high = (high - entry) / entry * 100
            pnl_at_low = (low - entry) / entry * 100
            pnl_at_close = (bar_close - entry) / entry * 100

            # Check SL hit (low touches SL)
            if low <= signal["sl"]:
                sl_pnl = (signal["sl"] - entry) / entry * 100
                close_reason = "sl_hit"
                close_price = signal["sl"]
                close_bar = i
                max_pnl_pct = max(max_pnl_pct, pnl_at_high)
                break

            # Check TP hits
            if high >= signal["tp3"]:
                close_reason = "tp3_hit"
                close_price = signal["tp3"]
                close_bar = i
                tp_progress_pct = 100.0
                max_pnl_pct = max(max_pnl_pct, pnl_at_high)
                break
            elif high >= signal["tp2"]:
                tp_progress_pct = max(tp_progress_pct, 66.0)
            elif high >= signal["tp1"]:
                tp_progress_pct = max(tp_progress_pct, 33.0)

            max_pnl_pct = max(max_pnl_pct, pnl_at_high)
            min_pnl_pct = min(min_pnl_pct, pnl_at_low)

        else:  # short
            pnl_at_high = (entry - high) / entry * 100
            pnl_at_low = (entry - low) / entry * 100
            pnl_at_close = (entry - bar_close) / entry * 100

            # Check SL hit (high touches SL)
            if high >= signal["sl"]:
                close_reason = "sl_hit"
                close_price = signal["sl"]
                close_bar = i
                max_pnl_pct = max(max_pnl_pct, pnl_at_low)
                break

            # Check TP hits
            if low <= signal["tp3"]:
                close_reason = "tp3_hit"
                close_price = signal["tp3"]
                close_bar = i
                tp_progress_pct = 100.0
                max_pnl_pct = max(max_pnl_pct, pnl_at_low)
                break
            elif low <= signal["tp2"]:
                tp_progress_pct = max(tp_progress_pct, 66.0)
            elif low <= signal["tp1"]:
                tp_progress_pct = max(tp_progress_pct, 33.0)

            max_pnl_pct = max(max_pnl_pct, pnl_at_low)
            min_pnl_pct = min(min_pnl_pct, pnl_at_high)

    # Final PnL
    if close_reason == "timeout":
        close_price = float(klines[close_bar][4])

    if is_long:
        realized_pnl_pct = (close_price - entry) / entry * 100
    else:
        realized_pnl_pct = (entry - close_price) / entry * 100

    drawdown_from_peak = max_pnl_pct - realized_pnl_pct if max_pnl_pct > 0 else abs(min_pnl_pct)

    # Compute size in units
    size = SIMULATED_SIZE_USDT / entry if entry > 0 else 1.0

    return {
        "close_reason": close_reason,
        "close_price": close_price,
        "realized_pnl_pct": round(realized_pnl_pct, 4),
        "max_pnl_pct": round(max_pnl_pct, 4),
        "drawdown_from_peak_pct": round(drawdown_from_peak, 4),
        "tp_progress_pct": round(tp_progress_pct, 2),
        "opened_at": datetime.fromtimestamp(signal["timestamp"] / 1000, tz=timezone.utc).isoformat(),
        "closed_at": datetime.fromtimestamp(int(klines[close_bar][0]) / 1000, tz=timezone.utc).isoformat(),
        "size": size,
        "hold_bars": close_bar - signal["bar_index"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. SAVE TO DATABASE
# ══════════════════════════════════════════════════════════════════════════════

async def ensure_tables(db_path: str):
    """Create required tables if they don't exist (for separate backtest DB)."""
    await pg_execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            size REAL,
            signal_hash TEXT,
            price_at_signal REAL,
            volatility_used REAL,
            payload_raw TEXT NOT NULL,
            risk_request TEXT,
            risk_result TEXT,
            approved INTEGER,
            var REAL,
            cvar REAL,
            liquidation_prob REAL,
            latency_ms REAL,
            risk_reward REAL,
            midas_probability REAL,
            midas_win_rate REAL,
            midas_trend TEXT,
            is_countertrend INTEGER,
            re_signal_score REAL,
            re_recommendation TEXT,
            re_annual_vol REAL,
            score_components TEXT,
            setup_master_text TEXT
        )
    """)
    await pg_execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            opened_at TEXT,
            symbol TEXT,
            side TEXT,
            size REAL,
            original_size REAL,
            entry_price REAL,
            current_sl REAL,
            current_tp1 REAL,
            current_tp2 REAL,
            current_tp3 REAL,
            current_price REAL,
            current_pnl_pct REAL,
            max_pnl_pct REAL,
            max_price_favorable REAL,
            drawdown_from_peak_pct REAL,
            tp_progress_pct REAL,
            initial_signal_score REAL,
            initial_recommendation TEXT,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            close_reason TEXT,
            realized_pnl_pct REAL,
            signal_hash TEXT,
            close_reason_detailed TEXT
        )
    """)


async def save_results(symbol: str, results: list[dict]):
    """Save backtest results to signals + open_positions tables."""
    for r in results:
        sig = r["signal"]
        pos = r["position"]

        # Insert into signals
        import json
        payload_raw = json.dumps({
            "symbol": symbol, "side": sig["side"], "source": "backtest_v2",
            "entry_price": sig["entry_price"], "sl": sig["sl"],
            "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig["tp3"],
            "rsi": sig.get("rsi", 50),
        })
        risk_request = json.dumps({"backtest": True})
        risk_result = json.dumps({
            "score": sig["score"],
            "recommendation": sig["recommendation"],
            "components": sig.get("score_components", {}),
        })

        await pg_execute("""
            INSERT INTO signals (
                created_at, source, symbol, side, size,
                signal_hash, price_at_signal, volatility_used,
                risk_reward, midas_probability, midas_win_rate,
                midas_trend, is_countertrend,
                re_signal_score, re_recommendation, re_annual_vol,
                payload_raw, risk_request, risk_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos["opened_at"], "backtest_v2", symbol, sig["side"], pos["size"],
            sig["signal_hash"], sig["entry_price"], sig["annual_vol"],
            sig["rr"], sig["probability"], sig["win_rate"],
            sig["trend"], sig["is_countertrend"],
            sig["score"], sig["recommendation"], sig["annual_vol"],
            payload_raw, risk_request, risk_result,
        ))

        # Insert into open_positions
        await pg_execute("""
            INSERT INTO open_positions (
                signal_id, opened_at, symbol, side, size, original_size,
                entry_price, current_sl, current_tp1, current_tp2, current_tp3,
                current_price, current_pnl_pct, max_pnl_pct,
                drawdown_from_peak_pct, tp_progress_pct,
                initial_signal_score, initial_recommendation,
                status, closed_at, close_reason, realized_pnl_pct,
                signal_hash
            ) VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?)
        """, (
            pos["opened_at"], symbol, sig["side"], pos["size"], pos["size"],
            sig["entry_price"], sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"],
            pos["close_price"], pos["realized_pnl_pct"], pos["max_pnl_pct"],
            pos["drawdown_from_peak_pct"], pos["tp_progress_pct"],
            sig["score"], sig["recommendation"],
            pos["closed_at"], pos["close_reason"], pos["realized_pnl_pct"],
            sig["signal_hash"],
        ))

    logger.info(f"[{symbol}] Saved {len(results)} positions to DB")


# ══════════════════════════════════════════════════════════════════════════════
# 7. ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

async def run_backtest(symbols: list[str], days: int = 30) -> dict:
    """Run full backtest pipeline for given symbols."""
    start_time = time.time()
    total_results = {}

    # Create tables if using separate DB
    await ensure_tables(config.DB_PATH)

    for symbol in symbols:
        try:
            # 1. Fetch klines
            klines = await fetch_historical_klines(symbol, days)
            if not klines:
                logger.warning(f"[{symbol}] No klines, skipping")
                continue

            # 2. Generate signals
            signals = generate_signals(klines)
            if not signals:
                logger.warning(f"[{symbol}] No signals generated, skipping")
                continue

            # 3. Score each signal
            scored = [score_signal(s) for s in signals]

            # 4. Simulate positions
            results = []
            for sig in scored:
                pos = simulate_position(sig, klines)
                results.append({"signal": sig, "position": pos})

            # 5. Save to DB
            await save_results(symbol, results)

            # Stats
            wins = sum(1 for r in results if r["position"]["realized_pnl_pct"] > 0)
            avg_pnl = sum(r["position"]["realized_pnl_pct"] for r in results) / len(results)
            total_results[symbol] = {
                "total": len(results),
                "wins": wins,
                "losses": len(results) - wins,
                "win_rate": round(wins / len(results) * 100, 1),
                "avg_pnl": round(avg_pnl, 2),
            }

            logger.info(
                f"[{symbol}] {len(results)} trades | "
                f"WR: {total_results[symbol]['win_rate']}% | "
                f"Avg PnL: {total_results[symbol]['avg_pnl']}%"
            )

        except Exception as e:
            logger.error(f"[{symbol}] Error: {e}")
            total_results[symbol] = {"error": str(e)}

    elapsed = time.time() - start_time
    summary = {
        "duration_sec": round(elapsed, 1),
        "symbols": total_results,
        "total_positions": sum(
            v.get("total", 0) for v in total_results.values()
        ),
    }

    logger.info(
        f"══ Backtest complete: {summary['total_positions']} positions "
        f"in {summary['duration_sec']}s ══"
    )
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Risk Engine Backtest Module")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to backtest (e.g. BTCUSDT ETHUSDT)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Use all default symbols"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Number of days of history (max ~41 due to 1000 kline limit)"
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to save results (default: main signals.db)"
    )
    args = parser.parse_args()

    if args.all:
        symbols = DEFAULT_SYMBOLS
    elif args.symbols:
        symbols = args.symbols
    else:
        symbols = DEFAULT_SYMBOLS[:3]

    # Override DB path if specified
    if args.db:
        config.DB_PATH = args.db

    print(f"═══ Risk Engine Backtest v0.7.0 ═══")
    print(f"GPU: {'🟢 Titan V (CuPy)' if GPU_AVAILABLE else '🔴 CPU only (NumPy)'}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Days: {args.days}")
    print(f"DB: {config.DB_PATH}")
    print()

    result = asyncio.run(run_backtest(symbols, args.days))

    # Print summary
    print(f"\n═══ RESULTS ═══")
    for sym, stats in result["symbols"].items():
        if "error" in stats:
            print(f"  {sym}: ERROR — {stats['error']}")
        else:
            print(
                f"  {sym}: {stats['total']} trades | "
                f"WR {stats['win_rate']}% | Avg PnL {stats['avg_pnl']}%"
            )
    print(f"\n  Total: {result['total_positions']} positions in {result['duration_sec']}s")


if __name__ == "__main__":
    main()
