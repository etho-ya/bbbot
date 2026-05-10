"""
Deep Kline Backfill — one-time historical data fetch for Scout & ML.
v0.19.5 2026-04-07: Initial implementation.

Fetches 500 1h candles (~21 days) and 200 4h candles (~33 days) per symbol
using kline_collector's 2-tier OKX/Bybit Proxy fallback with pagination.

OKX API returns max 100 candles per request, so each target is fetched in
multiple paginated chunks (oldest-first), each shifted forward by chunk_size
candles. INSERT ON CONFLICT DO NOTHING prevents duplicates with existing data.

Usage:
    /opt/risk-engine/venv/bin/python3 src/scripts/backfill_klines_deep.py

Symbols: union of open_positions + klines_history + hardcoded high-vol list.
"""
import asyncio
import logging
import sys
import time

sys.path.insert(0, "/opt/risk-engine/src")

from db_adapter import pg_fetch_all, init_pg_pool, close_pg_pool  # noqa: E402
import httpx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_klines_deep")

HARDCODED_HIGH_VOL = [
    "SIRENUSDT", "NOMUSDT", "STOUSDT", "RIVERUSDT", "VVVUSDT",
    "ASTERUSDT", "KERNELUSDT", "CUSDT",
]

TARGETS = [
    {"tf": "1h", "okx_bar": "1H", "bybit_interval": "60", "minutes": 60, "total_candles": 500},
    {"tf": "4h", "okx_bar": "4H", "bybit_interval": "240", "minutes": 240, "total_candles": 200},
]

CHUNK_SIZE = 100
STAGGER_SEC = 0.2


async def _get_all_symbols() -> list[str]:
    symbols = set(HARDCODED_HIGH_VOL)
    try:
        rows = await pg_fetch_all("SELECT DISTINCT symbol FROM open_positions")
        for r in rows:
            symbols.add(r["symbol"])
    except Exception:
        pass
    try:
        rows = await pg_fetch_all("SELECT DISTINCT symbol FROM klines_history")
        for r in rows:
            symbols.add(r["symbol"])
    except Exception:
        pass
    return sorted(symbols)


async def main():
    await init_pg_pool()

    from kline_collector import _fetch_klines, _store_klines, _TIMEFRAMES  # noqa: E402

    symbols = await _get_all_symbols()
    logger.info(f"Deep backfill: {len(symbols)} symbols, targets: "
                + ", ".join(f"{t['tf']}x{t['total_candles']}" for t in TARGETS))

    grand_total = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for symbol in symbols:
            sym_total = 0
            for target in TARGETS:
                tf_info = next((t for t in _TIMEFRAMES if t["tf"] == target["tf"]), None)
                if not tf_info:
                    continue

                total_needed = target["total_candles"]
                now_ms = int(time.time() * 1000)
                full_span_ms = total_needed * target["minutes"] * 60 * 1000
                base_start = now_ms - full_span_ms

                fetched_total = 0
                for chunk_idx in range(0, total_needed, CHUNK_SIZE):
                    chunk_start = base_start + chunk_idx * target["minutes"] * 60 * 1000
                    chunk_limit = min(CHUNK_SIZE, total_needed - chunk_idx)

                    klines, src = await _fetch_klines(
                        client, symbol, tf_info,
                        limit=chunk_limit, start_ts=chunk_start,
                    )
                    if klines:
                        n = await _store_klines(symbol, target["tf"], klines)
                        fetched_total += n
                    await asyncio.sleep(STAGGER_SEC)

                if fetched_total > 0:
                    sym_total += fetched_total

            if sym_total > 0:
                logger.info(f"  {symbol}: +{sym_total} candles")
                grand_total += sym_total

    logger.info(f"Deep backfill complete: {grand_total} new candles across {len(symbols)} symbols")
    await close_pg_pool()


if __name__ == "__main__":
    asyncio.run(main())
