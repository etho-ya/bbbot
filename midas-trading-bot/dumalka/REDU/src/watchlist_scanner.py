"""
Watchlist Scanner — Autonomous Dump/Pump Detector (v0.8.2)

Monitors a configurable list of trading pairs every N minutes,
detects significant price moves (dumps/pumps), scores opportunities
through the existing RE pipeline, and sends alerts to Telegram.

This is ALERTING ONLY — Midas remains the primary signal source.
All detections are logged for post-mortem analysis and ML training.

Flow:
  1. Every SCAN_INTERVAL_SEC, fetch klines + market data for each watchlist symbol
  2. Compute price change over 1h, 4h windows
  3. If |change| > threshold → score via compute_signal_score()
  4. If score >= MIN_ALERT_SCORE → send TG alert + save to DB
  5. Background: backfill price_after_1h/4h for post-mortem learning
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, get_db_pool
import httpx

from config import config
from bybit import fetch_market_data, fetch_multi_timeframe, fetch_funding_and_oi, fetch_orderbook_depth
from scoring import compute_signal_score
from regime_detector import get_cached_regime

logger = logging.getLogger("risk-engine.scanner")

# ── Cooldown: don't spam alerts for same symbol ──────────────────────────────
_alert_cooldown: dict[str, float] = {}  # symbol → last alert timestamp
COOLDOWN_SECONDS = 1800  # 30 min between alerts for same symbol

# v0.9.4 FIX-4: Heartbeat for background task monitoring
_heartbeat: dict[str, float] = {}

# ── Price history cache (in-memory ring buffer per symbol) ───────────────────
_price_history: dict[str, list[tuple[float, float]]] = {}  # symbol → [(ts, price), ...]
MAX_HISTORY_POINTS = 50  # ~4h at 5min intervals


def _record_price(symbol: str, price: float):
    """Add price snapshot to in-memory history."""
    now = time.time()
    if symbol not in _price_history:
        _price_history[symbol] = []
    _price_history[symbol].append((now, price))
    # Trim old entries
    if len(_price_history[symbol]) > MAX_HISTORY_POINTS:
        _price_history[symbol] = _price_history[symbol][-MAX_HISTORY_POINTS:]


def compute_price_changes(symbol: str, current_price: float) -> dict:
    """
    Compute price change % over 1h and 4h windows from cached history.
    Returns: {"change_1h": float|None, "change_4h": float|None, "change_since_last": float|None}
    """
    history = _price_history.get(symbol, [])
    now = time.time()
    result = {"change_1h": None, "change_4h": None, "change_since_last": None}

    if not history:
        return result

    # Change since last scan
    last_price = history[-1][1] if history else current_price
    if last_price > 0:
        result["change_since_last"] = ((current_price - last_price) / last_price) * 100

    # 1h change (~12 points back at 5min intervals)
    target_1h = now - 3600
    for ts, price in reversed(history):
        if ts <= target_1h and price > 0:
            result["change_1h"] = ((current_price - price) / price) * 100
            break

    # 4h change (~48 points back)
    target_4h = now - 14400
    for ts, price in reversed(history):
        if ts <= target_4h and price > 0:
            result["change_4h"] = ((current_price - price) / price) * 100
            break

    return result


def detect_opportunity(
    symbol: str,
    price_changes: dict,
    volatility: float,
    funding_rate: float,
) -> Optional[dict]:
    """
    Detect if price movement qualifies as a dump or pump.

    Returns dict with alert info or None.
    Criteria:
      - |price_change_1h| > DUMP/PUMP_THRESHOLD_PCT, OR
      - |price_change_4h| > DUMP/PUMP_THRESHOLD_PCT * 1.5
      - Not in cooldown
    """
    change_1h = price_changes.get("change_1h")
    change_4h = price_changes.get("change_4h")

    # Check cooldown
    last_alert = _alert_cooldown.get(symbol, 0)
    if time.time() - last_alert < COOLDOWN_SECONDS:
        return None

    alert = None

    # 1h window check
    if change_1h is not None:
        if change_1h <= -config.DUMP_THRESHOLD_PCT:
            alert = {
                "type": "dump",
                "side": "short",
                "trigger": f"1h drop {change_1h:.2f}%",
                "change_pct": change_1h,
                "window": "1h",
            }
        elif change_1h >= config.PUMP_THRESHOLD_PCT:
            alert = {
                "type": "pump",
                "side": "long",
                "trigger": f"1h rise +{change_1h:.2f}%",
                "change_pct": change_1h,
                "window": "1h",
            }

    # 4h window check (higher threshold, lower priority if 1h already detected)
    if alert is None and change_4h is not None:
        threshold_4h = config.DUMP_THRESHOLD_PCT * 1.5
        if change_4h <= -threshold_4h:
            alert = {
                "type": "dump",
                "side": "short",
                "trigger": f"4h drop {change_4h:.2f}%",
                "change_pct": change_4h,
                "window": "4h",
            }
        elif change_4h >= threshold_4h:
            alert = {
                "type": "pump",
                "side": "long",
                "trigger": f"4h rise +{change_4h:.2f}%",
                "change_pct": change_4h,
                "window": "4h",
            }

    if alert:
        alert["symbol"] = symbol
        alert["change_1h"] = change_1h
        alert["change_4h"] = change_4h

    return alert


async def score_opportunity(
    symbol: str,
    side: str,
    volatility: float,
    funding_rate: float,
    oi_change_pct: float,
    multi_tf_trends: Optional[dict],
    spread_pct: float = 0.0,
) -> dict:
    """Score detected opportunity through existing RE pipeline."""
    # We don't have Midas metadata (that's the whole point — we're detecting independently)
    # So we pass None for probability/win_rate and let the data quality penalty apply
    result = compute_signal_score(
        side=side,
        risk_reward=None,        # unknown — let scoring use conservative default
        probability=None,        # unknown
        win_rate=None,           # unknown
        trend=None,              # use multi_tf instead
        market_vol=volatility,
        spread_pct=spread_pct,
        multi_tf_trends=multi_tf_trends,
        funding_rate=funding_rate,
        oi_change_pct=oi_change_pct,
    )
    return result


async def send_alert_telegram(alert: dict, score_result: dict, price: float, volatility: float):
    """Send formatted alert to Telegram channel."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    symbol = alert["symbol"]
    side = alert["side"].upper()
    alert_type = alert["type"].upper()
    score = score_result["score"]
    rec = score_result["recommendation"]
    trigger = alert["trigger"]

    emoji = "\U0001f4c9" if alert["type"] == "dump" else "\U0001f4c8"
    score_emoji = "\U0001f7e2" if rec == "approve" else "\U0001f7e1" if rec == "reduce" else "\U0001f534"

    msg = (
        f"{emoji} *WATCHLIST ALERT: {symbol}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*{alert_type}* detected: {trigger}\n"
        f"\U0001f4b0 Price: {price}\n"
        f"\U0001f4ca Vol: {volatility:.2f}\n"
        f"\n"
        f"{score_emoji} *RE Score:* {score:.3f} \u2192 {rec.upper()}\n"
    )

    change_1h = alert.get("change_1h")
    change_4h = alert.get("change_4h")
    if change_1h is not None:
        msg += f"\U0001f4c9 1h: {change_1h:+.2f}%\n"
    if change_4h is not None:
        msg += f"\U0001f4c9 4h: {change_4h:+.2f}%\n"

    components = score_result.get("components", {})
    if components:
        msg += (
            f"\n*Components:*\n"
            f"  Trend={components.get('trend_align', 0):.2f} "
            f"Vol={components.get('vol_ok', 0):.2f} "
            f"Liq={components.get('liquidity_ok', 0):.2f} "
            f"Fund={components.get('funding_oi', 0):.2f}\n"
        )

    if score_result.get("is_countertrend"):
        msg += "\U0001f504 *Countertrend*\n"

    msg += f"\n\u26a0\ufe0f _Alert only \u2014 Midas remains primary_"

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            })
            if resp.status_code == 200:
                logger.info(f"\U0001f4e8 Alert sent: {symbol} {alert_type} score={score:.3f} rec={rec}")
            else:
                logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Failed to send scanner alert: {e}")


# ── Contra-Trend Opportunity Detection (v0.9.2) ─────────────────────────
#
# Event Lifecycle approach:
#   1. Pump detected in bearish trend → event created + tracked
#   2. Subsequent scans update the event data but DON'T re-alert
#   3. Event cleared when price change drops below threshold
#   4. New event can only fire after previous one cleared
#
# Multi-factor confidence prevents false alerts:
#   - TF alignment (2/3 = 0.5, 3/3 = 1.0)
#   - Volume context (low vol pump = weak, more likely to revert)
#   - OI direction (declining OI on pump = no real buyers)
#   - Funding alignment (positive funding + SHORT opportunity = edge)

# Active events: symbol → {type, direction, first_price, max_change, alerted, ts}
_active_events: dict[str, dict] = {}


def detect_contra_trend(
    alert: dict,
    multi_tf: Optional[dict],
    volatility: float = 0.0,
    funding_rate: float = 0.0,
    oi_change: float = 0.0,
) -> Optional[dict]:
    """
    Detect contra-trend opportunities with event lifecycle tracking.

    Pattern: GRASSUSDT (22 Mar 2026) — pump +5% while all 3 TF bearish
    → high probability of reversion.

    Uses multi-factor confidence scoring to filter out weak signals.
    Returns opportunity dict (with 'confidence' field) or None.
    """
    if not multi_tf or not alert:
        return None

    symbol = alert.get("symbol", "")
    alert_type = alert["type"]  # 'pump' or 'dump'
    change_pct = alert.get("change_pct", 0)
    abs_change = abs(change_pct)

    trend_15m = multi_tf.get("15m", "unknown")
    trend_1h = multi_tf.get("1h", "unknown")
    trend_4h = multi_tf.get("4h", "unknown")
    trends = [trend_15m, trend_1h, trend_4h]

    # ── Event lifecycle: check if this is a NEW event or continuation ──
    existing = _active_events.get(symbol)
    if existing:
        # Same type of move still active → update data, DON'T re-alert
        if existing["type"] == alert_type:
            if abs_change > abs(existing.get("max_change", 0)):
                existing["max_change"] = change_pct
            return None  # Already alerted for this event
        else:
            # Direction changed (pump → dump or vice versa) → clear old event
            del _active_events[symbol]

    # ── Minimum threshold: abs change must be >= 3.0% (строже чем раньше) ──
    if abs_change < 3.0:
        return None

    # ── Multi-factor confidence score ──────────────────────────────
    confidence = 0.0
    confidence_factors = []

    if alert_type == "pump":
        bearish_count = sum(1 for t in trends if t == "bearish")
        if bearish_count < 2:
            return None
        direction = "SHORT"
        opp_type = "contra_short"

        # Factor 1: TF alignment (0.3 for 2/3, 0.5 for 3/3)
        tf_score = 0.5 if bearish_count == 3 else 0.3
        confidence += tf_score
        confidence_factors.append(f"TF={bearish_count}/3({tf_score:.1f})")

        # Factor 2: Volume context (low vol pump = weak)
        if volatility < 1.0:  # Low vol
            confidence += 0.15
            confidence_factors.append("LowVol(+0.15)")
        elif volatility > 2.0:  # High vol = risky
            confidence -= 0.1
            confidence_factors.append("HighVol(-0.1)")

        # Factor 3: Funding alignment (positive funding = longs overcrowded)
        if funding_rate > 0.0001:
            confidence += 0.15
            confidence_factors.append("Fund+(+0.15)")

        # Factor 4: OI declining = no new buyers (bearish)
        if oi_change < -0.005:  # OI dropped > 0.5%
            confidence += 0.1
            confidence_factors.append("OI\u2193(+0.1)")

        # Factor 5: Pump magnitude (bigger pump = more likely to revert)
        if abs_change >= 5.0:
            confidence += 0.15
            confidence_factors.append(f"Big({abs_change:.0f}%+0.15)")
        elif abs_change >= 4.0:
            confidence += 0.05
            confidence_factors.append(f"Med({abs_change:.0f}%+0.05)")

        reason = f"Pump +{abs_change:.1f}% vs {'ALL' if bearish_count == 3 else '2/3'} TF bearish"
        alignment = bearish_count

    elif alert_type == "dump":
        bullish_count = sum(1 for t in trends if t == "bullish")
        if bullish_count < 2:
            return None
        direction = "LONG"
        opp_type = "contra_long"

        tf_score = 0.5 if bullish_count == 3 else 0.3
        confidence += tf_score
        confidence_factors.append(f"TF={bullish_count}/3({tf_score:.1f})")

        if volatility < 1.0:
            confidence += 0.15
            confidence_factors.append("LowVol(+0.15)")
        elif volatility > 2.0:
            confidence -= 0.1
            confidence_factors.append("HighVol(-0.1)")

        if funding_rate < -0.0001:
            confidence += 0.15
            confidence_factors.append("Fund-(+0.15)")

        if oi_change < -0.005:
            confidence += 0.1
            confidence_factors.append("OI\u2193(+0.1)")

        if abs_change >= 5.0:
            confidence += 0.15
            confidence_factors.append(f"Big({abs_change:.0f}%+0.15)")
        elif abs_change >= 4.0:
            confidence += 0.05
            confidence_factors.append(f"Med({abs_change:.0f}%+0.05)")

        reason = f"Dump {change_pct:.1f}% vs {'ALL' if bullish_count == 3 else '2/3'} TF bullish"
        alignment = bullish_count

    else:
        return None

    confidence = round(min(confidence, 1.0), 2)

    # ── Register event (even if below alert threshold — prevents re-detection) ──
    _active_events[symbol] = {
        "type": alert_type,
        "direction": direction,
        "max_change": change_pct,
        "confidence": confidence,
        "ts": time.time(),
    }

    # Confidence label for display
    if confidence >= 0.7:
        conf_label = "HIGH"
    elif confidence >= 0.5:
        conf_label = "MED"
    else:
        conf_label = "LOW"

    return {
        "opportunity_type": opp_type,
        "direction": direction,
        "trigger_type": alert_type,
        "trigger_pct": change_pct,
        "trigger_window": alert.get("window", "1h"),
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "trend_alignment": alignment,
        "confidence": confidence,
        "confidence_label": conf_label,
        "confidence_factors": confidence_factors,
        "reason": reason,
    }


async def send_opportunity_alert(
    symbol: str, opportunity: dict, price: float,
    score_result: dict, volatility: float, funding_rate: float,
    spread_pct: float = 0.0, slippage_pct: float = 0.0,
    open_pos_count: int = 0, recent_trades: list = None
):
    """Send high-priority contra-trend opportunity alert to Telegram (Russian)."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    direction = opportunity["direction"]
    reason = opportunity["reason"]
    alignment = opportunity["trend_alignment"]
    score = score_result["score"]
    confidence = opportunity.get("confidence", 0)
    conf_label = opportunity.get("confidence_label", "LOW")
    trigger_window = opportunity.get("trigger_window", "1h")
    factors = opportunity.get("confidence_factors", [])

    # Funding context line
    funding_edge = ""
    if direction == "SHORT" and funding_rate > 0.0001:
        funding_edge = "\nFunding > 0 (лонги платят шортам)"
    elif direction == "LONG" and funding_rate < -0.0001:
        funding_edge = "\nFunding < 0 (шорты платят лонгам)"

    factors_str = " | ".join(factors) if factors else ""

    msg = (
        f"\u26a1 *{direction} {symbol}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{reason}\n"
        f"\n"
        f"Price: {price}\n"
        f"Trend: 15m={opportunity['trend_15m']} | 1h={opportunity['trend_1h']} | 4h={opportunity['trend_4h']}\n"
        f"Confidence: *{conf_label}* ({confidence:.0%})\n"
        f"\n"
        f"RE Score: {score:.3f} | Vol: {volatility:.2f}\n"
        f"Funding: {funding_rate:.6f}{funding_edge}\n"
    )

    if factors_str:
        msg += f"\n{factors_str}\n"

    # RE Market Context Formatting
    recent_trades = recent_trades or []
    if not recent_trades:
        hist_str = "None"
    else:
        sl_hits = sum(1 for t in recent_trades if t.get('event_type') == 'sl_hit')
        avg_pnl = sum(t.get('pnl_pct', 0) for t in recent_trades) / len(recent_trades)
        if sl_hits >= 3:
            hist_str = f"{sl_hits} SL hits in a row (avg {avg_pnl:.1f}%)"
        else:
            hist_str = f"{len(recent_trades)} trades (avg {avg_pnl:.1f}%)"

    liq_label = "TOXIC 🔴" if slippage_pct >= 1.0 else "POOR 🟠" if slippage_pct >= 0.5 else "OK 🟢"

    msg += (
        f"\n⚠️ *RE MARKET CONTEXT:*\n"
        f"• Liquidity: {liq_label}\n"
        f"• Spread: {spread_pct:.2f}% | Slippage ($1k): {slippage_pct:.2f}%\n"
        f"• Recent history: {hist_str}\n"
        f"• Open positions: {open_pos_count}\n"
    )

    if slippage_pct >= 1.0:
        msg += "\n🛑 _Осторожно: экстремальное проскальзывание!_\n"

    msg += "\n"
    if direction == "SHORT":
        msg += f"\u041f\u0430\u043c\u043f \u043f\u0440\u043e\u0442\u0438\u0432 \u0442\u0440\u0435\u043d\u0434\u0430 ({trigger_window}) = \u0432\u043e\u0437\u043c\u043e\u0436\u0435\u043d \u043e\u0442\u043a\u0430\u0442 \u0432\u043d\u0438\u0437"
    else:
        msg += f"\u0414\u0430\u043c\u043f \u043f\u0440\u043e\u0442\u0438\u0432 \u0442\u0440\u0435\u043d\u0434\u0430 ({trigger_window}) = \u0432\u043e\u0437\u043c\u043e\u0436\u0435\u043d \u043e\u0442\u0441\u043a\u043e\u043a \u0432\u0432\u0435\u0440\u0445"

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            })
            if resp.status_code == 200:
                logger.info(f"Opportunity alert sent: {direction} {symbol} ({conf_label} {confidence:.0%})")
            else:
                logger.warning(f"Telegram opportunity alert error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Failed to send opportunity alert: {e}")


async def save_opportunity_to_db(
    symbol: str, opportunity: dict, price: float,
    score_result: dict, volatility: float,
    funding_rate: float, oi_change: float, alert_sent: bool,
):
    """Save detected opportunity to trade_opportunities table for ML training."""
    try:
        await pg_execute("""
            INSERT INTO trade_opportunities (
                symbol, opportunity_type, trigger_type, trigger_pct, trigger_window,
                price, trend_15m, trend_1h, trend_4h, trend_alignment,
                re_score, re_recommendation, funding_rate, oi_change_pct,
                volatility, alert_sent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            opportunity["opportunity_type"],
            opportunity["trigger_type"],
            opportunity["trigger_pct"],
            opportunity["trigger_window"],
            price,
            opportunity["trend_15m"],
            opportunity["trend_1h"],
            opportunity["trend_4h"],
            opportunity["trend_alignment"],
            score_result["score"],
            score_result["recommendation"],
            funding_rate,
            oi_change,
            volatility,
            int(bool(alert_sent)),
        ))
        logger.info(f"\U0001f4be Opportunity saved: {opportunity['opportunity_type']} {symbol}")
    except Exception as e:
        logger.error(f"Failed to save opportunity to DB: {e}")


async def save_alert_to_db(
    alert: dict, score_result: dict, price: float,
    volatility: float, funding_rate: float,
    multi_tf_trends: Optional[dict], alert_sent: bool,
):
    """Persist alert to watchlist_alerts table for post-mortem analysis."""
    try:
        await pg_execute("""
            INSERT INTO watchlist_alerts (
                created_at, symbol, alert_type, side,
                price, price_change_1h, price_change_4h,
                volatility, funding_rate,
                re_score, re_recommendation, multi_tf_trends,
                alert_sent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            alert["symbol"],
            alert["type"],
            alert["side"],
            price,
            alert.get("change_1h"),
            alert.get("change_4h"),
            volatility,
            funding_rate,
            score_result["score"],
            score_result["recommendation"],
            json.dumps(multi_tf_trends) if multi_tf_trends else None,
            int(bool(alert_sent)),
        ))
    except Exception as e:
        logger.error(f"Failed to save alert to DB: {e}")


# ── Post-mortem: backfill future price data ──────────────────────────────────

async def backfill_post_mortem():
    """
    Periodically fill in price_after_1h and price_after_4h for past alerts.
    This creates the ML training dataset: "what happened after the alert?"
    """
    try:
        # Find alerts older than 1h that don't have price_after_1h yet
        alerts_1h = await pg_fetch_all("""
            SELECT id, symbol, side, price, created_at
            FROM watchlist_alerts
            WHERE price_after_1h IS NULL
              AND created_at < NOW() - INTERVAL '1 hour'
            ORDER BY id DESC LIMIT 20
        """)

        # Find alerts older than 4h that don't have price_after_4h yet
        alerts_4h = await pg_fetch_all("""
            SELECT id, symbol, side, price, created_at
            FROM watchlist_alerts
            WHERE price_after_4h IS NULL
              AND created_at < NOW() - INTERVAL '4 hours'
            ORDER BY id DESC LIMIT 20
        """)

        # Fetch current prices and backfill
        symbols_needed = set()
        for a in alerts_1h + alerts_4h:
            symbols_needed.add(a["symbol"])

        price_cache = {}
        for sym in symbols_needed:
            p, _ = await fetch_market_data(sym)
            if p > 0:
                price_cache[sym] = p

        for a in alerts_1h:
            current_price = price_cache.get(a["symbol"])
            try:
                entry = float(a["price"]) if a["price"] else 0.0
            except ValueError:
                entry = 0.0
            
            if current_price and entry > 0:
                side = a["side"]
                if side == "short":
                    pnl_pct = ((entry - current_price) / entry) * 100
                else:
                    pnl_pct = ((current_price - entry) / entry) * 100
                await pg_execute("""
                    UPDATE watchlist_alerts
                    SET price_after_1h = ?, would_have_pnl_pct = ?
                    WHERE id = ?
                """, (current_price, round(pnl_pct, 4), a["id"]))

        for a in alerts_4h:
            current_price = price_cache.get(a["symbol"])
            try:
                entry = float(a["price"]) if a["price"] else 0.0
            except ValueError:
                entry = 0.0

            if current_price and entry > 0:
                side = a["side"]
                if side == "short":
                    pnl_pct = ((entry - current_price) / entry) * 100
                else:
                    pnl_pct = ((current_price - entry) / entry) * 100
                await pg_execute("""
                    UPDATE watchlist_alerts
                    SET price_after_4h = ?
                    WHERE id = ? AND would_have_pnl_pct IS NULL
                """, (current_price, a["id"]))
                # Update would_have_pnl if not already set (prefer 4h if 1h wasn't available)
                if a.get("price_after_1h") is None:
                    await pg_execute("""
                        UPDATE watchlist_alerts
                        SET would_have_pnl_pct = ?
                        WHERE id = ? AND would_have_pnl_pct IS NULL
                    """, (round(pnl_pct, 4), a["id"]))


        filled = len([a for a in alerts_1h if a["symbol"] in price_cache]) + \
                 len([a for a in alerts_4h if a["symbol"] in price_cache])
        if filled > 0:
            logger.info(f"\U0001f4da Post-mortem: backfilled {filled} alert outcomes")

        # ── Post-mortem for trade_opportunities ──────────────────────────
        opps_1h = await pg_fetch_all("""
            SELECT id, symbol, opportunity_type, price, created_at
            FROM trade_opportunities
            WHERE price_after_1h IS NULL
              AND created_at < NOW() - INTERVAL '1 hour'
            ORDER BY id DESC LIMIT 20
        """)
        for opp in opps_1h:
            cp = price_cache.get(opp["symbol"])
            if not cp:
                p, _ = await fetch_market_data(opp["symbol"])
                if p > 0:
                    cp = p
                    price_cache[opp["symbol"]] = p
            if cp and opp["price"] > 0:
                if "short" in opp["opportunity_type"]:
                    pnl = ((opp["price"] - cp) / opp["price"]) * 100
                else:
                    pnl = ((cp - opp["price"]) / opp["price"]) * 100
                outcome = "win" if pnl > 0 else "loss"
                await pg_execute("""
                    UPDATE trade_opportunities
                    SET price_after_1h = ?, would_have_pnl_1h = ?, outcome = ?
                    WHERE id = ?
                """, (cp, round(pnl, 4), outcome, opp["id"]))

        opps_filled = len([o for o in opps_1h if o["symbol"] in price_cache])
        if opps_filled > 0:
            logger.info(f"\U0001f4da Post-mortem: backfilled {opps_filled} opportunity outcomes")

    except Exception as e:
        logger.error(f"Post-mortem backfill error: {e}")


# ── Fetch klines for short-term price change detection ───────────────────────

async def fetch_recent_price_change(symbol: str) -> dict:
    """
    Fetch 1h klines and compute price changes directly from Bybit,
    to supplement in-memory cache (useful on first start when cache is empty).
    Returns: {"change_1h": float|None, "change_4h": float|None}
    """
    result = {"change_1h": None, "change_4h": None}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 15-min klines, last 20 candles = 5 hours
            url = f"{config.BYBIT_PROXY_URL}/klines/{symbol}?interval=15&limit=20"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                klines = data.get("klines", data) if isinstance(data, dict) else data
                if isinstance(klines, list) and len(klines) >= 4:
                    # Bybit klines: newest first → [0]=latest, [-1]=oldest
                    closes = [float(k[4]) for k in klines]
                    current = closes[0]

                    # ~1h ago = 4 candles back (4 × 15min = 60min)
                    if len(closes) > 4 and closes[4] > 0:
                        result["change_1h"] = ((current - closes[4]) / closes[4]) * 100

                    # ~4h ago = 16 candles back
                    if len(closes) > 16 and closes[16] > 0:
                        result["change_4h"] = ((current - closes[16]) / closes[16]) * 100

    except Exception as e:
        logger.debug(f"fetch_recent_price_change failed for {symbol}: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCANNER LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def scan_watchlist():
    """Background loop: scan watchlist pairs for dump/pump opportunities."""
    logger.info(
        f"🔍 Watchlist Scanner started | "
        f"{len(config.WATCHLIST_SYMBOLS)} symbols | "
        f"interval={config.SCAN_INTERVAL_SEC}s | "
        f"dump_thresh={config.DUMP_THRESHOLD_PCT}% | "
        f"pump_thresh={config.PUMP_THRESHOLD_PCT}%"
    )

    # Initial delay to let other services start
    await asyncio.sleep(15)

    scan_count = 0
    POST_MORTEM_EVERY = 12  # Run post-mortem every ~1h (12 × 5min)

    while True:
        try:
            scan_count += 1
            t0 = time.time()
            alerts_generated = 0

            for symbol in config.WATCHLIST_SYMBOLS:
                try:
                    # 1. Fetch market data (price + vol)
                    price, volatility = await fetch_market_data(symbol)
                    if price <= 0:
                        continue

                    # 2. Record price for in-memory history
                    _record_price(symbol, price)

                    # 3. Compute price changes (from cache + klines fallback)
                    changes = compute_price_changes(symbol, price)

                    # On first few scans, use klines-based detection since cache is empty
                    if changes["change_1h"] is None:
                        kline_changes = await fetch_recent_price_change(symbol)
                        if kline_changes["change_1h"] is not None:
                            changes["change_1h"] = kline_changes["change_1h"]
                        if kline_changes["change_4h"] is not None:
                            changes["change_4h"] = kline_changes["change_4h"]

                    # 4. Fetch funding + OI
                    funding_rate, oi_change = await fetch_funding_and_oi(symbol)

                    # 5. Detect opportunity
                    opportunity = detect_opportunity(symbol, changes, volatility, funding_rate)

                    if opportunity:
                        # v0.8.4: Skip ranging markets
                        regime_info = get_cached_regime()
                        is_ranging = regime_info and regime_info.get("regime") == "ranging"
                        if is_ranging:
                            logger.debug(f"🔍 Scanner: {symbol} skipped — ranging market")
                            continue

                        # 6. Fetch multi-TF trends for scoring
                        multi_tf = await fetch_multi_timeframe(symbol)

                        # 7. Score the opportunity
                        score_result = await score_opportunity(
                            symbol=symbol,
                            side=opportunity["side"],
                            volatility=volatility,
                            funding_rate=funding_rate,
                            oi_change_pct=oi_change,
                            multi_tf_trends=multi_tf,
                        )

                        logger.info(
                            f"🔍 Scanner: {symbol} {opportunity['type'].upper()} "
                            f"| {opportunity['trigger']} "
                            f"| score={score_result['score']:.3f} rec={score_result['recommendation']} "
                            f"| price={price} vol={volatility:.2f}"
                        )

                        # 8. Send TG alert if score is meaningful
                        MIN_ALERT_SCORE = 0.30  # Low threshold — we want to capture data
                        alert_sent = False
                        if score_result["score"] >= MIN_ALERT_SCORE:
                            await send_alert_telegram(opportunity, score_result, price, volatility)
                            _alert_cooldown[symbol] = time.time()
                            alert_sent = True
                            alerts_generated += 1

                        # 9. Always save to DB for post-mortem (even if not alerted)
                        await save_alert_to_db(
                            opportunity, score_result, price,
                            volatility, funding_rate, multi_tf, alert_sent,
                        )

                        # 10. Contra-trend opportunity detection (v0.9.2)
                        contra = detect_contra_trend(
                            opportunity, multi_tf,
                            volatility=volatility,
                            funding_rate=funding_rate,
                            oi_change=oi_change,
                        )
                        if contra:
                            conf = contra.get('confidence', 0)
                            conf_label = contra.get('confidence_label', 'LOW')
                            logger.info(
                                f"\u26a1 CONTRA-TREND: {contra['direction']} {symbol} | "
                                f"{contra['reason']} | conf={conf_label}({conf:.0%}) | "
                                f"factors={contra.get('confidence_factors', [])}"
                            )
                            # Only send TG alert for MED+ confidence
                            should_alert = conf >= 0.5
                            if should_alert:
                                # Fetch Context for Telegram
                                spread_pct, slippage_pct = await fetch_orderbook_depth(symbol, 1000.0)
                                open_pos_count = await pg_fetch_val(
                                    "SELECT count(*) FROM open_positions WHERE symbol=%s AND status='open'",
                                    (symbol,)
                                )
                                recent_trades = await pg_fetch_all("""
                                    SELECT event_type, pnl_pct FROM trade_outcomes 
                                    WHERE symbol = %s AND event_type IN ('full_close', 'sl_hit', 'tp3_hit', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close')
                                    ORDER BY id DESC LIMIT 5
                                """, (symbol,))
                                
                                await send_opportunity_alert(
                                    symbol, contra, price, score_result,
                                    volatility, funding_rate,
                                    spread_pct=spread_pct, slippage_pct=slippage_pct,
                                    open_pos_count=open_pos_count, recent_trades=recent_trades
                                )
                            # Always save to DB for ML training
                            await save_opportunity_to_db(
                                symbol, contra, price, score_result,
                                volatility, funding_rate, oi_change,
                                alert_sent=should_alert,
                            )

                except Exception as e:
                    logger.warning(f"Scanner error for {symbol}: {e}")

            elapsed = time.time() - t0
            if alerts_generated > 0 or scan_count % 12 == 0:
                logger.info(
                    f"🔍 Scan #{scan_count} complete: "
                    f"{len(config.WATCHLIST_SYMBOLS)} symbols, "
                    f"{alerts_generated} alerts, {elapsed:.1f}s"
                )

            # Periodic post-mortem backfill
            if scan_count % POST_MORTEM_EVERY == 0:
                await backfill_post_mortem()

        except Exception as e:
            logger.error(f"Scanner loop error: {e}", exc_info=True)
            _heartbeat["last_error"] = time.time()
            _heartbeat["error"] = str(e)[:100]

        # v0.9.4 FIX-4: Report heartbeat after each cycle
        _heartbeat["last_success"] = time.time()
        _heartbeat["scan_count"] = scan_count

        await asyncio.sleep(config.SCAN_INTERVAL_SEC)
