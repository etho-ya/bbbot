"""
Daily Risk Engine Report — sends summary to Telegram.
Run via cron: 0 9 * * * cd /opt/risk-engine/src && /opt/risk-engine/venv/bin/python daily_report.py

v0.9.4: Migrated from SQLite to PostgreSQL (P0 observability fix).
"""
import asyncio
import httpx
import os
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily-report")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


async def generate_report() -> str:
    """Generate daily summary from PostgreSQL."""
    from db_adapter import init_pg_pool, pg_fetch_all, pg_fetch_val

    await init_pg_pool()
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Signals today
    total = await pg_fetch_val(
        "SELECT COUNT(*) FROM signals WHERE created_at > ? AND COALESCE(source, '') != 'backtest_v2'",
        (yesterday,)
    ) or 0

    recs = await pg_fetch_all(
        "SELECT re_recommendation, COUNT(*) as cnt FROM signals WHERE created_at > ? AND COALESCE(source, '') != 'backtest_v2' GROUP BY re_recommendation",
        (yesterday,)
    )
    rec_str = ", ".join(f"{r['cnt']} {r['re_recommendation']}" for r in recs) if recs else "none"

    # Countertrend
    ct = await pg_fetch_val(
        "SELECT COUNT(*) FROM signals WHERE created_at > ? AND is_countertrend = true",
        (yesterday,)
    ) or 0

    # Open positions
    opens = await pg_fetch_val("SELECT COUNT(*) FROM open_positions WHERE status = 'open'") or 0
    avg_pnl = await pg_fetch_val(
        "SELECT ROUND(AVG(current_pnl_pct)::numeric, 2) FROM open_positions WHERE status = 'open'"
    ) or 0

    # Trade outcomes today
    outcomes = await pg_fetch_all(
        "SELECT event_type, COUNT(*) as cnt, ROUND(AVG(pnl_pct)::numeric, 2) as avg_pnl FROM trade_outcomes WHERE event_at > ? GROUP BY event_type",
        (yesterday,)
    )
    outcome_str = "\n".join(
        f"  {r['event_type']}: {r['cnt']} (avg {r['avg_pnl']}%)" for r in outcomes
    ) if outcomes else "  none"

    # Closed today
    closed_count = await pg_fetch_val(
        "SELECT COUNT(*) FROM open_positions WHERE closed_at > ?", (yesterday,)
    ) or 0
    closed_pnl = await pg_fetch_val(
        "SELECT ROUND(AVG(realized_pnl_pct)::numeric, 2) FROM open_positions WHERE closed_at > ?",
        (yesterday,)
    ) or 0

    # RE unavailable
    try:
        unavail = await pg_fetch_val(
            "SELECT COUNT(*) FROM re_unavailable_events WHERE event_at > ?", (yesterday,)
        ) or 0
    except Exception:
        unavail = "N/A"

    # Latency
    lat = await pg_fetch_val(
        "SELECT ROUND(AVG(latency_ms)) FROM signals WHERE created_at > ? AND latency_ms > 0",
        (yesterday,)
    ) or 0

    # Snapshots collected
    snaps = await pg_fetch_val(
        "SELECT COUNT(*) FROM position_snapshots WHERE snapshot_at > ?", (yesterday,)
    ) or 0

    # Думалка audit summary
    audit_total = await pg_fetch_val(
        "SELECT COUNT(*) FROM dumalka_audit_log WHERE timestamp > ?", (yesterday,)
    ) or 0
    audit_actions = await pg_fetch_all(
        "SELECT action, COUNT(*) as cnt FROM dumalka_audit_log WHERE timestamp > ? GROUP BY action ORDER BY cnt DESC LIMIT 5",
        (yesterday,)
    )
    audit_str = ", ".join(f"{r['cnt']}× {r['action']}" for r in audit_actions) if audit_actions else "none"

    # Wallet balance
    wallet_str = ""
    try:
        from bybit import fetch_wallet_balance
        wallet = await fetch_wallet_balance()
        if wallet and wallet.get("equity"):
            wallet_str = f"\n💰 <b>Wallet</b>: ${float(wallet['equity']):.2f}"
    except Exception:
        pass

    report = (
        f"📊 <b>Daily Risk Report</b> ({datetime.now().strftime('%Y-%m-%d')})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Signals</b>: {total} ({rec_str})\n"
        f"🔄 Countertrend: {ct}/{total}\n"
        f"📈 <b>Open positions</b>: {opens} (avg PnL: {avg_pnl}%)\n"
        f"🔒 <b>Closed today</b>: {closed_count} (avg PnL: {closed_pnl}%)\n"
        f"\n<b>Trade events:</b>\n{outcome_str}\n"
        f"\n🧠 <b>Думалка</b>: {audit_total} actions ({audit_str})\n"
        f"⚠️ RE unavailable: {unavail}\n"
        f"⏱ Avg latency: {lat}ms\n"
        f"🧠 ML snapshots: {snaps}"
        f"{wallet_str}\n"
    )
    return report


async def send_report():
    """Send report to Telegram."""
    try:
        report = await generate_report()
        logger.info(report)

        if not BOT_TOKEN or not CHAT_ID:
            logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping send")
            return

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={
                "chat_id": CHAT_ID,
                "text": report,
                "parse_mode": "HTML",
            })
            if resp.status_code == 200:
                logger.info("✅ Report sent to Telegram")
            else:
                logger.error(f"❌ Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Failed to generate/send report: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(send_report())
