"""
Scout — Autonomous Shadow Signal Generator.
v0.19.6 2026-04-09: Bugfix — accumulation feature used wrong column name `composite_score`
  (column is `accumulation_score` in accumulation_snapshots). Silent exception hid the error;
  scout feature was always NULL for accumulation_score. Fixed to use correct column name.
  Tuning: _SPIKE_ATR_MULT 3.0 → 2.5 based on backtest (34 symbols, 548+ candles).
  At 3.0: 32 signals, WR 34%, avg PnL -0.5%. At 2.5: 47 signals, WR 36%, avg PnL +2.1%.
  SIREN rocket (04-04, +107% in 4h, spike=2.76x ATR) missed at 3.0 but caught at 2.5.
v0.19.2 2026-04-04: Extended symbol tracking (48h post-close) + spike_consolidation_breakout signal.
v0.19.1 2026-04-04: Enhanced signal types + ML features + derivatives time-series.
  - 7 signal types (3 new: funding_extreme_reversal, volume_breakout, rsi_divergence)
  - 12 ML features per signal (4 new: long_short_ratio, atr_pct, ema_distance_pct, price_change_4h)
  - derivatives_snapshots time-series (funding/OI/LSR per symbol, every 15m)
  - Fixes: API stagger (150ms), independent feature fetches, always-log cycle completion
v0.19.0 2026-04-04: Initial implementation.

Signal Types (8):
  EMA crossover:
    - ema_cross_golden  (EMA20 crosses above EMA50, RSI<70, 4h confirms)
    - ema_cross_death   (EMA20 crosses below EMA50, RSI>30, 4h confirms)
  RSI threshold:
    - rsi_oversold_bounce     (RSI crosses below 25)
    - rsi_overbought_reversal (RSI crosses above 75)
  Contrarian / momentum (v0.19.1):
    - funding_extreme_reversal (|funding|>0.03%, contrarian, RSI 40-60 zone)
    - volume_breakout          (vol>2.5x avg + directional candle >0.5%)
    - rsi_divergence           (price/RSI divergence, 14-candle lookback)
  Re-entry (v0.19.2):
    - spike_consolidation_breakout (spike > 2.5x ATR above EMA20, consolidation, breakout)

ML Feature Snapshot (12 features per signal):
  funding_rate, oi_change_pct, spread_pct, rsi_14, volume_ratio,
  btc_change_1h, multi_tf_trends, accumulation_score,
  long_short_ratio (OKX L/S account ratio),
  atr_pct (ATR/price normalized volatility),
  ema_distance_pct (momentum: distance from EMA50),
  price_change_4h (from klines_history)

Derivatives Time-Series (v0.19.1):
  Every Scout cycle stores per-symbol snapshot to derivatives_snapshots:
  funding_rate, oi_change_pct, long_short_ratio, price.
  Enables future funding curve regime detection and OI Z-Score.

All signals are SHADOW ONLY — stored in scout_signals table with
shadow PnL tracking (1h/4h/24h). No commands are sent to the bot.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from config import config
from db_adapter import pg_execute, pg_fetch_all, pg_fetch_one

logger = logging.getLogger("risk-engine.scout")

_MIN_CANDLES = 55
_MIN_SIGNAL_GAP_SEC = 3600
_SL_ATR_MULT = 1.5
_TP1_RR = 1.0
_TP3_RR = 3.0

_FUNDING_EXTREME = 0.0003  # 0.03% — contrarian threshold
_VOLUME_BREAKOUT_MULT = 2.5
_RSI_DIVERGENCE_LOOKBACK = 14  # candles to search for divergence

# v0.19.2: Spike consolidation breakout parameters
# v0.19.6 tuning: 3.0 → 2.5 (backtest: +2.1% avg PnL vs -0.5% at 3.0, 34 symbols)
_SPIKE_ATR_MULT = 2.5       # spike = high > EMA20 + 2.5x ATR
_SPIKE_LOOKBACK = 6         # candles to search for spike
_CONSOL_CANDLES = 3         # candles for consolidation check
_CONSOL_ATR_MULT = 1.5      # consolidation range < 1.5x ATR
_BREAKOUT_VOL_MULT = 1.5    # breakout volume > 1.5x avg

_last_signal_time: dict[str, float] = {}


# ─── Technical Indicator Calculations ───────────────────────────

def _calc_ema(values: list[float], period: int) -> list[float]:
    emas = [float('nan')] * len(values)
    if len(values) < period:
        return emas
    multiplier = 2.0 / (period + 1)
    emas[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        emas[i] = (values[i] - emas[i - 1]) * multiplier + emas[i - 1]
    return emas


def _calc_rsi(closes: list[float], period: int = 14) -> list[float]:
    rsi = [50.0] * len(closes)
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


def _calc_atr(highs: list[float], lows: list[float], closes: list[float],
              period: int = 14) -> list[float]:
    atr = [0.0] * len(closes)
    if len(closes) < period + 1:
        return atr
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) >= period:
        atr[period] = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr[i + 1] = (atr[i] * (period - 1) + trs[i]) / period
    return atr


# ─── Data Access ────────────────────────────────────────────────

async def _get_klines(symbol: str, tf: str, limit: int = 100) -> list[dict]:
    rows = await pg_fetch_all("""
        SELECT open_time, open, high, low, close, volume, turnover
        FROM klines_history
        WHERE symbol = ? AND timeframe = ?
        ORDER BY open_time ASC
        LIMIT ?
    """, (symbol, tf, limit))
    return rows or []


async def _get_4h_trend(symbol: str) -> str | None:
    klines = await _get_klines(symbol, "4h", 55)
    if len(klines) < 51:
        return None
    closes = [float(k["close"]) for k in klines]
    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    last = len(closes) - 1
    if math.isnan(ema20[last]) or math.isnan(ema50[last]):
        return None
    if ema20[last] > ema50[last]:
        return "bullish"
    elif ema20[last] < ema50[last]:
        return "bearish"
    return "neutral"


# ─── Divergence Detection ──────────────────────────────────────

def _detect_rsi_divergence(closes: list[float], rsi: list[float],
                           lookback: int = 14) -> str | None:
    """Detect bullish or bearish RSI divergence on last `lookback` candles.
    Bullish: price makes lower low, RSI makes higher low.
    Bearish: price makes higher high, RSI makes lower high.
    Returns 'bullish', 'bearish', or None.
    """
    n = len(closes)
    if n < lookback + 2:
        return None

    window_c = closes[-(lookback + 1):]
    window_r = rsi[-(lookback + 1):]

    price_min_idx = 0
    price_max_idx = 0
    for j in range(1, len(window_c) - 1):
        if window_c[j] < window_c[price_min_idx]:
            price_min_idx = j
        if window_c[j] > window_c[price_max_idx]:
            price_max_idx = j

    curr = len(window_c) - 1

    if window_c[curr] < window_c[price_min_idx] and price_min_idx > 0:
        if window_r[curr] > window_r[price_min_idx]:
            return "bullish"

    if window_c[curr] > window_c[price_max_idx] and price_max_idx > 0:
        if window_r[curr] < window_r[price_max_idx]:
            return "bearish"

    return None


def _detect_spike_consolidation_breakout(
    closes: list[float], highs: list[float], lows: list[float],
    volumes: list[float], ema20: list[float], atr: list[float],
    avg_vol_24h: float,
) -> str | None:
    """Detect spike → consolidation → breakout pattern for second-wave entry.

    v0.19.2 2026-04-04: Initial implementation (shadow mode only).

    Pattern:
      1. Recent spike: max(high) in last SPIKE_LOOKBACK candles > EMA20 + SPIKE_ATR_MULT * ATR
      2. Consolidation: last CONSOL_CANDLES range < CONSOL_ATR_MULT * ATR
      3. Breakout: current candle close > consolidation high
      4. Volume: current volume > BREAKOUT_VOL_MULT * avg_vol_24h

    Returns 'long', 'short', or None.
    Currently only generates LONG signals (riding second wave up).
    """
    n = len(closes)
    if n < _SPIKE_LOOKBACK + _CONSOL_CANDLES + 2:
        return None

    i = n - 1
    if math.isnan(ema20[i]) or atr[i] <= 0 or avg_vol_24h <= 0:
        return None

    # Step 1: Check for a recent spike in the lookback window (before consolidation)
    spike_start = i - _CONSOL_CANDLES - _SPIKE_LOOKBACK
    spike_end = i - _CONSOL_CANDLES
    if spike_start < 0:
        return None

    spike_threshold = ema20[spike_end] + _SPIKE_ATR_MULT * atr[spike_end]
    max_high_in_spike = max(highs[spike_start:spike_end + 1])
    if max_high_in_spike < spike_threshold:
        return None

    # Step 2: Consolidation — last CONSOL_CANDLES (excluding current) have tight range
    consol_start = i - _CONSOL_CANDLES
    consol_end = i - 1
    if consol_start < 0:
        return None

    consol_highs = highs[consol_start:consol_end + 1]
    consol_lows = lows[consol_start:consol_end + 1]
    consol_range = max(consol_highs) - min(consol_lows)
    consol_high = max(consol_highs)

    if consol_range > _CONSOL_ATR_MULT * atr[i]:
        return None

    # Step 3: Breakout — current close above consolidation high
    if closes[i] <= consol_high:
        return None

    # Step 4: Volume confirmation
    if volumes[i] < avg_vol_24h * _BREAKOUT_VOL_MULT:
        return None

    return "long"


# ─── Feature Snapshot ───────────────────────────────────────────

async def _snapshot_features(symbol: str, closes: list[float],
                             ema50: list[float], atr: list[float]) -> dict:
    """Capture current market features for ML training dataset.
    v0.19.1: Added long_short_ratio, atr_pct, ema_distance_pct, price_change_4h.
    """
    features = {
        "funding_rate": None, "oi_change_pct": None, "spread_pct": None,
        "rsi_14": None, "volume_ratio": None, "btc_change_1h": None,
        "multi_tf_trends": None, "accumulation_score": None,
        "long_short_ratio": None, "atr_pct": None,
        "ema_distance_pct": None, "price_change_4h": None,
    }

    i = len(closes) - 1
    price = closes[i] if closes else 0

    if price > 0 and atr[i] > 0:
        features["atr_pct"] = round((atr[i] / price) * 100, 4)

    if price > 0 and not math.isnan(ema50[i]) and ema50[i] > 0:
        features["ema_distance_pct"] = round(((price - ema50[i]) / ema50[i]) * 100, 4)

    try:
        klines_4h = await _get_klines(symbol, "4h", 2)
        if len(klines_4h) >= 2:
            old_c = float(klines_4h[0]["close"])
            new_c = float(klines_4h[-1]["close"])
            if old_c > 0:
                features["price_change_4h"] = round(((new_c - old_c) / old_c) * 100, 4)
    except Exception:
        pass

    try:
        from bybit import (fetch_funding_and_oi, fetch_orderbook_depth,
                           fetch_volume_info, fetch_btc_change_1h,
                           fetch_long_short_ratio)
    except ImportError:
        return features

    for fetch_name, fetch_fn in [
        ("funding_oi", lambda: fetch_funding_and_oi(symbol)),
        ("spread", lambda: fetch_orderbook_depth(symbol)),
        ("volume", lambda: fetch_volume_info(symbol)),
        ("btc", lambda: fetch_btc_change_1h()),
        ("lsr", lambda: fetch_long_short_ratio(symbol)),
    ]:
        try:
            result = await fetch_fn()
            if fetch_name == "funding_oi":
                features["funding_rate"], features["oi_change_pct"] = result
            elif fetch_name == "spread":
                features["spread_pct"] = result[0]
            elif fetch_name == "volume":
                features["volume_ratio"] = result
            elif fetch_name == "btc":
                features["btc_change_1h"] = result
            elif fetch_name == "lsr":
                features["long_short_ratio"] = result
        except Exception:
            pass

    try:
        from regime_detector import get_cached_regime
        regime_info = get_cached_regime()
        if regime_info:
            features["regime"] = regime_info.get("regime", "normal")
    except Exception:
        pass

    try:
        # v0.19.6 fix (2026-04-09): column is `accumulation_score`, not `composite_score`
        acc = await pg_fetch_one("""
            SELECT accumulation_score FROM accumulation_snapshots
            WHERE symbol = ? ORDER BY id DESC LIMIT 1
        """, (symbol,))
        if acc:
            features["accumulation_score"] = float(acc["accumulation_score"])
    except Exception:
        pass

    return features


# ─── Signal Generation (per symbol) ────────────────────────────

async def _generate_signals_for_symbol(symbol: str) -> list[dict]:
    """Analyze 1h klines and generate signals from all 8 signal types."""
    now = time.time()
    last = _last_signal_time.get(symbol, 0)
    if now - last < _MIN_SIGNAL_GAP_SEC:
        return []

    klines_1h = await _get_klines(symbol, "1h", 100)
    if len(klines_1h) < _MIN_CANDLES:
        return []

    closes = [float(k["close"]) for k in klines_1h]
    highs = [float(k["high"]) for k in klines_1h]
    lows = [float(k["low"]) for k in klines_1h]
    volumes = [float(k["volume"]) for k in klines_1h]

    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    rsi = _calc_rsi(closes, 14)
    atr = _calc_atr(highs, lows, closes, 14)

    avg_vol_24h = sum(volumes[-24:]) / 24 if len(volumes) >= 24 else 0

    i = len(closes) - 1
    if math.isnan(ema20[i]) or math.isnan(ema50[i]) or atr[i] <= 0:
        return []

    from regime_detector import get_cached_regime
    regime_info = get_cached_regime()
    regime = regime_info.get("regime", "normal") if regime_info else "normal"

    trend_4h = await _get_4h_trend(symbol)

    candidates = []

    # --- Type 1: EMA Crossover ---
    prev_above = ema20[i - 1] > ema50[i - 1]
    curr_above = ema20[i] > ema50[i]

    if curr_above and not prev_above and rsi[i] < 70:
        candidates.append(("long", "ema_cross_golden"))
    elif not curr_above and prev_above and rsi[i] > 30:
        candidates.append(("short", "ema_cross_death"))

    # --- Type 2: RSI Threshold ---
    if rsi[i] < 25 and (i < 1 or rsi[i - 1] >= 25):
        candidates.append(("long", "rsi_oversold_bounce"))
    elif rsi[i] > 75 and (i < 1 or rsi[i - 1] <= 75):
        candidates.append(("short", "rsi_overbought_reversal"))

    # --- Type 3: RSI Divergence (v0.19.1) ---
    div = _detect_rsi_divergence(closes, rsi, _RSI_DIVERGENCE_LOOKBACK)
    if div == "bullish" and rsi[i] < 45:
        candidates.append(("long", "rsi_divergence"))
    elif div == "bearish" and rsi[i] > 55:
        candidates.append(("short", "rsi_divergence"))

    # --- Type 4: Volume Breakout (v0.19.1) ---
    if avg_vol_24h > 0 and volumes[i] > avg_vol_24h * _VOLUME_BREAKOUT_MULT:
        candle_body = closes[i] - closes[i - 1] if i > 0 else 0
        body_pct = abs(candle_body / closes[i - 1]) * 100 if i > 0 and closes[i - 1] > 0 else 0
        if body_pct > 0.5:  # meaningful directional move
            vb_side = "long" if candle_body > 0 else "short"
            candidates.append((vb_side, "volume_breakout"))

    # --- Type 5: Funding Extreme Reversal (v0.19.1) ---
    # Fetched lazily — only if no other signal found, to save API calls
    if not candidates:
        try:
            from bybit import fetch_funding_and_oi
            funding, _ = await fetch_funding_and_oi(symbol)
            if funding is not None and abs(funding) > _FUNDING_EXTREME:
                fr_side = "short" if funding > _FUNDING_EXTREME else "long"
                if rsi[i] > 40 and rsi[i] < 60:  # avoid overbought/sold zones
                    candidates.append((fr_side, "funding_extreme_reversal"))
        except Exception:
            pass

    # --- Type 6: Spike Consolidation Breakout (v0.19.2) ---
    scb_side = _detect_spike_consolidation_breakout(
        closes, highs, lows, volumes, ema20, atr, avg_vol_24h
    )
    if scb_side:
        candidates.append((scb_side, "spike_consolidation_breakout"))

    if not candidates:
        return []

    # --- Filters ---
    signals = []
    for side, signal_type in candidates:
        # Volume filter (skip for volume_breakout and spike_consolidation — already volume-confirmed)
        if signal_type not in ("volume_breakout", "spike_consolidation_breakout"):
            if avg_vol_24h > 0 and volumes[i] < avg_vol_24h * 0.8:
                continue

        # Regime filter (skip low_liquidity, ranging except for funding/spike_consolidation)
        if signal_type not in ("funding_extreme_reversal", "spike_consolidation_breakout"):
            if regime in ("low_liquidity", "ranging"):
                continue

        # 4h trend confluence (skip for divergence/funding/spike — contrarian or momentum-agnostic)
        if signal_type not in ("rsi_divergence", "funding_extreme_reversal", "spike_consolidation_breakout"):
            if trend_4h:
                if side == "long" and trend_4h == "bearish":
                    continue
                if side == "short" and trend_4h == "bullish":
                    continue

        entry = closes[i]
        sl_dist = atr[i] * _SL_ATR_MULT
        if side == "long":
            sl = entry - sl_dist
            tp1 = entry + sl_dist * _TP1_RR
            tp3 = entry + sl_dist * _TP3_RR
        else:
            sl = entry + sl_dist
            tp1 = entry - sl_dist * _TP1_RR
            tp3 = entry - sl_dist * _TP3_RR

        confidence = _calc_confidence(side, signal_type, rsi[i], volumes[i],
                                      avg_vol_24h, trend_4h)

        features = await _snapshot_features(symbol, closes, ema50, atr)
        features["rsi_14"] = rsi[i]

        _last_signal_time[symbol] = now

        signals.append({
            "symbol": symbol,
            "side": side,
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp3": tp3,
            "signal_type": signal_type,
            "regime": regime,
            "confidence": round(confidence, 3),
            **features,
        })
        break  # one signal per symbol per cycle

    return signals


def _calc_confidence(side: str, signal_type: str, rsi: float,
                     cur_vol: float, avg_vol: float,
                     trend_4h: str | None) -> float:
    """Multi-factor confidence score (0.0-1.0)."""
    confidence = 0.0

    # Base from signal type
    type_scores = {
        "ema_cross_golden": 0.25, "ema_cross_death": 0.25,
        "rsi_oversold_bounce": 0.20, "rsi_overbought_reversal": 0.20,
        "rsi_divergence": 0.30,
        "volume_breakout": 0.25,
        "funding_extreme_reversal": 0.20,
        "spike_consolidation_breakout": 0.35,
    }
    confidence += type_scores.get(signal_type, 0.15)

    # RSI in favorable zone
    if (side == "long" and 30 < rsi < 60) or (side == "short" and 40 < rsi < 70):
        confidence += 0.2

    # Volume strength
    if avg_vol > 0 and cur_vol > avg_vol * 1.2:
        confidence += 0.15
    if avg_vol > 0 and cur_vol > avg_vol * 2.0:
        confidence += 0.10

    # 4h trend alignment
    if trend_4h == ("bullish" if side == "long" else "bearish"):
        confidence += 0.25

    return min(confidence, 1.0)


# ─── Derivatives Time-Series Collection (v0.19.1) ──────────────

async def _collect_derivatives_snapshots(symbols: list[str]):
    """Store funding_rate + OI + L/S ratio per symbol for time-series analysis.
    Enables future funding curve regime detection and OI Z-Score computation.
    """
    stored = 0
    try:
        from bybit import (fetch_funding_and_oi, fetch_long_short_ratio,
                           fetch_market_data)
    except ImportError:
        return 0

    for symbol in symbols:
        try:
            funding, oi_change = await fetch_funding_and_oi(symbol)
            lsr = await fetch_long_short_ratio(symbol)
            price, _ = await fetch_market_data(symbol)

            if funding is None and oi_change is None and lsr == 1.0:
                continue

            await pg_execute("""
                INSERT INTO derivatives_snapshots
                (symbol, funding_rate, oi_value, long_short_ratio, price, oi_change_pct)
                VALUES (?, ?, NULL, ?, ?, ?)
            """, (symbol, funding, lsr, price, oi_change))
            stored += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.debug(f"Derivatives snapshot fail {symbol}: {e}")

    if stored > 0:
        logger.info(f"Derivatives snapshots: {stored}/{len(symbols)} symbols stored")
    return stored


# ─── Shadow PnL Resolution ─────────────────────────────────────

async def _resolve_shadow_signals():
    """Check unresolved scout signals and fill shadow PnL from stored klines."""
    unresolved = await pg_fetch_all("""
        SELECT id, symbol, side, entry_price, stop_loss, tp1, tp3,
               created_at, shadow_pnl_1h, shadow_pnl_4h, shadow_pnl_24h, shadow_outcome
        FROM scout_signals
        WHERE shadow_outcome IS NULL OR shadow_outcome = 'open'
        ORDER BY id
        LIMIT 20
    """)
    if not unresolved:
        return

    now = time.time()
    resolved = 0

    for sig in unresolved:
        try:
            created = sig["created_at"]
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace('Z', '+00:00'))
            age_sec = now - created.timestamp()

            if age_sec < 3900:
                continue

            entry = float(sig["entry_price"])
            sl = float(sig["stop_loss"])
            tp = float(sig["tp3"]) if sig["tp3"] else float(sig["tp1"])
            side = sig["side"]

            if entry <= 0 or sl <= 0 or tp <= 0:
                await pg_execute(
                    "UPDATE scout_signals SET shadow_outcome = 'no_data' WHERE id = ?",
                    (sig["id"],)
                )
                continue

            created_ms = int(created.timestamp() * 1000)
            klines = await pg_fetch_all("""
                SELECT open_time, high, low, close
                FROM klines_history
                WHERE symbol = ? AND timeframe = '15m' AND open_time >= ?
                ORDER BY open_time ASC LIMIT 400
            """, (sig["symbol"], created_ms))

            if not klines or len(klines) < 4:
                continue

            outcome = "open"
            pnl_1h = None
            pnl_4h = None
            pnl_24h = None

            for j, k in enumerate(klines):
                candle_age_min = (int(k["open_time"]) - created_ms) / 60000
                h = float(k["high"])
                l = float(k["low"])
                c = float(k["close"])

                if side == "long":
                    if l <= sl:
                        outcome = "sl_hit"
                        break
                    if h >= tp:
                        outcome = "tp_hit"
                        break
                    cur_pnl = ((c - entry) / entry) * 100
                else:
                    if h >= sl:
                        outcome = "sl_hit"
                        break
                    if l <= tp:
                        outcome = "tp_hit"
                        break
                    cur_pnl = ((entry - c) / entry) * 100

                if pnl_1h is None and candle_age_min >= 60:
                    pnl_1h = cur_pnl
                if pnl_4h is None and candle_age_min >= 240:
                    pnl_4h = cur_pnl
                if pnl_24h is None and candle_age_min >= 1440:
                    pnl_24h = cur_pnl

            if outcome == "open" and age_sec > 86400:
                outcome = "timeout"

            if outcome == "open" and pnl_1h is None:
                continue

            updates = []
            params = []
            if pnl_1h is not None:
                updates.append("shadow_pnl_1h = ?")
                params.append(round(pnl_1h, 4))
            if pnl_4h is not None:
                updates.append("shadow_pnl_4h = ?")
                params.append(round(pnl_4h, 4))
            if pnl_24h is not None:
                updates.append("shadow_pnl_24h = ?")
                params.append(round(pnl_24h, 4))
            if outcome != "open":
                updates.append("shadow_outcome = ?")
                params.append(outcome)
                updates.append("resolved_at = NOW()")

            if updates:
                sql = f"UPDATE scout_signals SET {', '.join(updates)} WHERE id = ?"
                params.append(sig["id"])
                await pg_execute(sql, tuple(params))
                resolved += 1

        except Exception as e:
            logger.debug(f"Scout shadow resolve error id={sig['id']}: {e}")

    if resolved > 0:
        logger.info(f"Scout shadow: resolved {resolved} signals")


# ─── Main Loop ──────────────────────────────────────────────────

async def scout_loop():
    """Main Scout background loop.
    v0.19.2 2026-04-04: Extended symbols (48h post-close) + spike_consolidation_breakout signal.
    v0.19.1 2026-04-04: Enhanced — 7 signal types, 12 ML features, derivatives time-series.
    v0.19.0 2026-04-04: Initial implementation — shadow mode only.
    """
    from main import _task_heartbeats

    await asyncio.sleep(30)
    logger.info("Scout Signal Generator started (v0.19.2, SHADOW MODE, 8 signal types)")

    while True:
        cycle_start = time.time()
        try:
            kline_count = await pg_fetch_one(
                "SELECT COUNT(DISTINCT symbol) as n FROM klines_history WHERE timeframe = '1h'"
            )
            if not kline_count or kline_count["n"] < 3:
                logger.info("Scout: waiting for kline_collector to populate data...")
                _task_heartbeats["scout"] = {
                    "last_success": time.time(),
                    "status": "waiting_for_klines",
                }
                await asyncio.sleep(config.SCOUT_INTERVAL_SEC)
                continue

            # v0.19.2: include recently-closed symbols (48h) for post-close signal generation
            symbols = sorted(set(config.WATCHLIST_SYMBOLS))
            try:
                traded = await pg_fetch_all(
                    """SELECT DISTINCT symbol FROM open_positions
                       WHERE status = 'open'
                          OR (status = 'closed' AND closed_at > datetime('now', '-48 hours'))"""
                )
                for r in traded:
                    symbols.append(r["symbol"])
                symbols = sorted(set(symbols))
            except Exception:
                pass

            # Phase 1: Generate signals
            total_signals = 0
            for symbol in symbols:
                try:
                    signals = await _generate_signals_for_symbol(symbol)
                    for sig in signals:
                        await pg_execute("""
                            INSERT INTO scout_signals
                            (symbol, side, entry_price, stop_loss, tp1, tp3,
                             signal_type, regime, confidence,
                             funding_rate, oi_change_pct, spread_pct, rsi_14,
                             volume_ratio, btc_change_1h, multi_tf_trends,
                             accumulation_score,
                             long_short_ratio, atr_pct, ema_distance_pct,
                             price_change_4h)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            sig["symbol"], sig["side"], sig["entry_price"],
                            sig["stop_loss"], sig["tp1"], sig["tp3"],
                            sig["signal_type"], sig.get("regime"),
                            sig["confidence"],
                            sig.get("funding_rate"), sig.get("oi_change_pct"),
                            sig.get("spread_pct"), sig.get("rsi_14"),
                            sig.get("volume_ratio"), sig.get("btc_change_1h"),
                            sig.get("multi_tf_trends"),
                            sig.get("accumulation_score"),
                            sig.get("long_short_ratio"), sig.get("atr_pct"),
                            sig.get("ema_distance_pct"),
                            sig.get("price_change_4h"),
                        ))
                        total_signals += 1
                        logger.info(
                            f"Scout signal: {sig['side'].upper()} {symbol} "
                            f"@ {sig['entry_price']:.6f} "
                            f"[{sig['signal_type']}] conf={sig['confidence']}"
                        )
                except Exception as e:
                    logger.debug(f"Scout error for {symbol}: {e}")

            # Phase 2: Resolve shadow PnL
            await _resolve_shadow_signals()

            # Phase 3: Collect derivatives time-series (v0.19.1)
            deriv_count = await _collect_derivatives_snapshots(symbols)

            elapsed = round(time.time() - cycle_start, 1)
            _task_heartbeats["scout"] = {
                "last_success": time.time(),
                "symbols_scanned": len(symbols),
                "signals_generated": total_signals,
                "derivatives_stored": deriv_count,
                "elapsed_s": elapsed,
            }

            logger.info(
                f"Scout cycle: {total_signals} signals, "
                f"{len(symbols)} symbols scanned, "
                f"{deriv_count} deriv snapshots ({elapsed}s)"
            )

        except Exception as e:
            logger.error(f"Scout cycle error: {e}")
            _task_heartbeats["scout"] = {
                "last_error": time.time(),
                "error": str(e)[:200],
            }

        await asyncio.sleep(config.SCOUT_INTERVAL_SEC)
