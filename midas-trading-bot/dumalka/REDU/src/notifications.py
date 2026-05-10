"""
Notification services for Risk Engine.
Extracted from main.py to break potential coupling and provide
a reusable alert interface for all modules.

v0.9    2026-03-22: TIER 1 migration — module extracted from main.py.
v0.19.3 2026-04-05: send_telegram_message() — BYBIT_PROXY /telegram/send first,
    then fallback to api.telegram.org (Dumalka host may be geo-blocked from Telegram).
v0.19.8 2026-04-11: API-first Bot Integration — added send_signal_report() and
    send_trade_event_report() for human-readable TG observability when bot calls
    RE directly via HTTP (source=bot_direct). Decouples TG reports from
    telegram_bridge.py, which becomes a passive observer / deprecation path.
"""
import logging
import httpx

from models import EnrichedRiskResult
from config import config

logger = logging.getLogger("risk-engine.notifications")


async def send_telegram_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Send plain text to the configured Telegram chat.

    Uses ``BYBIT_PROXY_URL/telegram/send`` when set (Tailscale / non-blocked egress),
    then falls back to direct Bot API if the proxy fails or returns an error.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    body: dict = {
        "bot_token": config.TELEGRAM_BOT_TOKEN,
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            proxy_base = (config.BYBIT_PROXY_URL or "").strip().rstrip("/")
            if proxy_base:
                try:
                    r = await client.post(f"{proxy_base}/telegram/send", json=body)
                    if r.status_code == 200:
                        try:
                            data = r.json()
                        except Exception:
                            data = {}
                        if data.get("ok") is True:
                            return True
                        desc = data.get("description", "") if isinstance(data, dict) else ""
                        if "can't parse entities" in desc:
                            logger.warning("Telegram proxy Markdown parse failed, will try direct fallback")
                        else:
                            logger.warning(
                                "Telegram proxy bad response: %s",
                                (data if data else r.text[:300]),
                            )
                    else:
                        logger.warning(
                            "Telegram proxy HTTP %s: %s",
                            r.status_code,
                            r.text[:200],
                        )
                except Exception as e:
                    logger.warning("Telegram proxy request failed: %s", e)

            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            r2 = await client.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
            if r2.status_code == 200:
                return True
            if r2.status_code == 400 and "can't parse entities" in r2.text:
                logger.warning(
                    "Telegram Markdown parse failed, retrying without parse_mode: %s",
                    r2.text[:200],
                )
                r3 = await client.post(url, json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                })
                if r3.status_code == 200:
                    return True
                logger.warning("Telegram plaintext fallback also failed %s: %s", r3.status_code, r3.text[:200])
                return False
            logger.warning(
                "Telegram direct API error %s: %s",
                r2.status_code,
                r2.text[:200],
            )
            return False
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


async def send_telegram_alert(enriched: EnrichedRiskResult, symbol: str, side: str):
    """Send risk assessment result to Telegram via proxy."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    emoji = "🟢" if enriched.approved else "🔴"
    rec_text = enriched.recommendation.upper()

    msg = (
        f"{emoji} *RISK ENGINE REPORT: {symbol} {side.upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Recommendation:* {rec_text}\n"
        f"*Score:* {enriched.signal_score:.2f} / 1.0\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Risk Metrics (Monte Carlo):*\n"
        f"• VaR (99%): {enriched.var*100:.2f}%\n"
        f"• CVaR: {enriched.cvar*100:.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Suggested Size (Kelly):*\n"
        f"• ${enriched.kelly_suggested_size_usd:,.2f}\n"
    )

    if enriched.exposure_warning:
        msg += "⚠️ *WARNING:* MAX EXPOSURE LIMIT EXCEEDED!\n"

    ok = await send_telegram_message(msg, parse_mode="Markdown")
    if not ok:
        logger.error("Failed to send Telegram risk alert")


async def send_signal_report(enriched: EnrichedRiskResult, symbol: str, side: str):
    """Human-readable signal evaluation report for TG observability.

    v0.19.8: Ported from telegram_bridge.format_and_send_report to decouple
    observability from the bridge process. Called by tv_webhook when
    source is "bot_direct" or "approval_flow".
    """
    rec = enriched.recommendation
    score = enriched.signal_score
    var_pct = enriched.var * 100
    cvar_pct = enriched.cvar * 100
    latency = enriched.latency_ms
    signal_hash = enriched.signal_hash or "n/a"

    emoji = {"approve": "🟢", "reduce": "🟡", "reject": "🔴"}.get(rec, "❓")
    rec_emoji = {"approve": "✅", "reduce": "⚠️", "reject": "❌"}.get(rec, "❓")

    if rec == "approve":
        decision_text = f"APPROVE ({score:.0%})"
    elif rec == "reduce":
        decision_text = f"REDUCE ({score:.0%})"
    else:
        decision_text = "REJECT"
        if enriched.rejection_reason:
            reason_safe = enriched.rejection_reason.replace("_", " ")
            decision_text += f" [{reason_safe}]"

    msg = (
        f"{emoji} *RE: {decision_text}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 {symbol} {side.upper()} | hash: `{signal_hash}`\n\n"
        f"{rec_emoji} *Score:* {score:.2f} / 1.0\n"
        f"📊 *VaR (99%):* {var_pct:.2f}%\n"
        f"📊 *CVaR:* {cvar_pct:.2f}%\n"
        f"⏱ *Latency:* {latency:.0f}ms\n"
    )

    if enriched.conviction_size_usd is not None:
        msg += f"💰 *Conviction:* ${enriched.conviction_size_usd:.0f}\n"

    if enriched.is_countertrend:
        msg += "🔄 *Countertrend signal*\n"
    if enriched.exposure_warning:
        msg += "⚠️ *Exposure limit exceeded*\n"

    components = enriched.score_components
    if components:
        msg += (
            f"\n*Components:*\n"
            f"  WR={components.get('wr', 0):.2f} "
            f"Prob={components.get('prob', 0):.2f} "
            f"R:R={components.get('rr', 0):.2f} "
            f"Trend={components.get('trend_align', 0):.2f} "
            f"Vol={components.get('vol_ok', 0):.2f} "
            f"Liq={components.get('liquidity_ok', 0):.2f}\n"
        )

    missing = []
    if enriched.midas_win_rate is None:
        missing.append("WinRate")
    if enriched.midas_probability is None:
        missing.append("Probability")
    if enriched.midas_risk_reward is None:
        missing.append("R:R")
    if missing:
        msg += f"⚠️ _Midas не передал: {', '.join(missing)} (score снижен)_\n"

    ok = await send_telegram_message(msg, parse_mode="Markdown")
    if not ok:
        logger.warning("Failed to send signal report to Telegram")


async def send_trade_event_report(
    event: str, symbol: str, side: str | None,
    pnl_pct: float | None, price: float | None,
    signal_hash: str | None, linked: bool,
):
    """Human-readable trade lifecycle event for TG observability.

    v0.19.8: Fires on /trade-outcome close events so admins see
    trade results in Telegram without relying on the bridge.
    """
    _CLOSE_EVENTS = {
        "sl_hit", "tp3_hit", "full_close", "timeout",
        "apollo_full_exit", "zone_full_exit", "e_pnl_full_exit",
        "dumalka_close", "manual_close", "flip_close",
    }
    if event not in _CLOSE_EVENTS:
        return

    emoji = {
        "sl_hit": "🔴", "tp3_hit": "🟢", "full_close": "🟡",
        "timeout": "⏰", "dumalka_close": "🤖", "manual_close": "👤",
        "flip_close": "🔄", "apollo_full_exit": "🚀",
        "zone_full_exit": "📊", "e_pnl_full_exit": "📉",
    }.get(event, "📋")

    event_safe = event.replace("_", " ")
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "n/a"
    pnl_emoji = "✅" if pnl_pct is not None and pnl_pct > 0 else "❌" if pnl_pct is not None else ""
    price_str = f"${price:,.4f}" if price is not None else "n/a"
    hash_short = signal_hash[:12] if signal_hash else "n/a"
    linked_str = "✓" if linked else "✗"

    msg = (
        f"{emoji} *TRADE CLOSED: {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 {(side or '?').upper()} | {event_safe}\n"
        f"{pnl_emoji} *PnL:* {pnl_str}\n"
        f"💵 *Price:* {price_str}\n"
        f"🔗 Hash: {hash_short} (linked: {linked_str})\n"
    )

    ok = await send_telegram_message(msg, parse_mode="Markdown")
    if not ok:
        logger.warning("Failed to send trade event report to Telegram")
