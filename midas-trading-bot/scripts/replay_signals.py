"""Replay the last 5 trade signals to @uebot_report for RE testing."""
import asyncio
import json
import sys
import os

sys.path.insert(0, "/app")
os.chdir("/app")

from app.core.database import async_session
from app.core.config import settings
from app.models.db_models import SignalDB
from app.services.telegram_notifier import notifier
from app.services.signal_parser import parse_trade_signal
from sqlalchemy import select, desc


async def replay():
    # Start Telegram client for notifier
    from app.services.telegram_listener import client
    await client.start(phone=settings.TELEGRAM_PHONE)
    notifier.set_client(client)
    print("Telegram client connected.")

    async with async_session() as session:
        result = await session.execute(
            select(SignalDB)
            .where(SignalDB.signal_type == "trade")
            .order_by(desc(SignalDB.created_at))
            .limit(5)
        )
        signals = result.scalars().all()

    if not signals:
        print("No trade signals found in DB.")
        return

    print(f"Found {len(signals)} signals to replay.\n")

    for sig in reversed(signals):  # oldest first
        parsed = json.loads(sig.parsed_data) if sig.parsed_data else None
        if not parsed:
            # Re-parse from raw text
            parsed = parse_trade_signal(sig.raw_text)

        if not parsed:
            print(f"[SKIP] Signal #{sig.id}: could not parse")
            continue

        symbol = parsed.get("symbol", "???")
        side = parsed.get("side", "???")
        entry = parsed.get("entry_price", 0)
        tp1 = parsed.get("tp1", 0)
        tp2 = parsed.get("tp2", 0)
        tp3 = parsed.get("tp3", 0)
        sl = parsed.get("sl", 0)

        metadata = parsed.get("metadata", {})
        risk_reward = metadata.get("risk_reward")
        probability = metadata.get("probability")
        win_rate = metadata.get("win_rate")
        trend = "bullish" if side == "Buy" else "bearish"

        print(f"[REPLAY] Signal #{sig.id}: {symbol} {side} @ {entry}")
        print(f"         TP1={tp1} TP2={tp2} TP3={tp3} SL={sl}")
        print(f"         R:R={risk_reward} Prob={probability} WR={win_rate} Trend={trend}")

        await notifier.notify_re_request(
            chat_id=settings.RE_REPORT_CHAT_ID,
            symbol=symbol, side=side, entry=entry,
            tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
            sig_hash=sig.signal_hash or "replay-test",
            raw_text=sig.raw_text or "",
            risk_reward=risk_reward,
            probability=probability,
            win_rate=win_rate,
            trend=trend,
        )
        print(f"         ✅ Sent to {settings.RE_REPORT_CHAT_ID}")
        await asyncio.sleep(2)  # avoid flood

    print("\nDone! Check @uebot_report for test messages.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(replay())
