"""
Kline Historical Collector — Background candle storage for Scout & ML.
v0.19.2 2026-04-04: Extended symbol tracking — includes recently-closed positions (48h)
  so klines are available for what_if_outcomes, Scout shadow PnL, and ML labeling post-close.
v0.19.0 2026-04-04: Initial implementation.

Stores 15m/1h/4h candles in PostgreSQL `klines_history` table.
Uses 2-tier fetch with adaptive source cache:
  1. OKX (geo-free, covers ~60% of portfolio symbols)
  2. Bybit Proxy (100% coverage, api.bybit.com is geo-blocked)

Adaptive source cache: after first successful fetch per symbol,
remembers which source worked and skips failed ones on subsequent cycles.
Cache resets on service restart (re-learns in 1 cycle).

Network constraints:
  - api.bybit.com is geo-blocked from this machine
  - Bybit Proxy VPS at config.BYBIT_PROXY_URL (Tailscale)
  - OKX public API is geo-free
  - Many altcoins (STO, VVV, NOM, SYRUP, AVAAI) are Bybit-exclusive
"""
import asyncio
import logging
import time

import httpx

from config import config
from db_adapter import pg_execute, pg_fetch_one, pg_fetch_all

logger = logging.getLogger("risk-engine.kline_collector")

_TIMEFRAMES = [
    {"tf": "15m", "okx_bar": "15m",  "bybit_interval": "15",  "minutes": 15},
    {"tf": "1h",  "okx_bar": "1H",   "bybit_interval": "60",  "minutes": 60},
    {"tf": "4h",  "okx_bar": "4H",   "bybit_interval": "240", "minutes": 240},
]

_FETCH_TIMEOUT = 8.0
_STAGGER_MS = 150

# Adaptive source cache: symbol -> "okx" | "proxy"
_symbol_source: dict[str, str] = {}


async def _fetch_from_okx(client: httpx.AsyncClient, symbol: str,
                           okx_bar: str, limit: int, start_ts: int,
                           end_ts: int) -> list | None:
    """Fetch klines from OKX. Returns list of candles or None."""
    okx_base = (symbol[:-4] + "-USDT") if symbol.endswith("USDT") else symbol
    for suffix in ("-SWAP", ""):
        try:
            inst = okx_base + suffix
            url = (
                f"https://www.okx.com/api/v5/market/history-candles"
                f"?instId={inst}&bar={okx_bar}"
                f"&after={end_ts}&before={start_ts}&limit={min(100, limit)}"
            )
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    data.reverse()
                    return data
        except Exception:
            pass
    return None


async def _fetch_from_proxy(client: httpx.AsyncClient, symbol: str,
                             bybit_interval: str, limit: int,
                             start_ts: int) -> list | None:
    """Fetch klines from Bybit Proxy. Returns list of candles or None."""
    try:
        url = (
            f"{config.BYBIT_PROXY_URL}/klines/{symbol}"
            f"?interval={bybit_interval}&limit={limit}&start={start_ts}"
        )
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            klines = data.get("result", {}).get("list", [])
            if not klines:
                klines = data.get("klines", data) if isinstance(data, dict) else data
            if isinstance(klines, list) and klines:
                klines.reverse()
                return klines
    except Exception:
        pass
    return None


def _parse_kline(row: list) -> tuple:
    """Normalize a kline row to (open_time, open, high, low, close, volume, turnover).
    Works for both OKX and Bybit formats (both are list-of-strings, same column order).
    OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    Bybit: [ts, o, h, l, c, volume, turnover]
    """
    return (
        int(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
        float(row[6]) if len(row) > 6 else 0.0,
    )


async def _fetch_klines(client: httpx.AsyncClient, symbol: str,
                          tf_info: dict, limit: int = 2,
                          start_ts: int | None = None) -> tuple[list, str]:
    """Fetch klines using 2-tier fallback with adaptive source cache.
    Returns (parsed_klines, source_name).
    """
    if start_ts is None:
        now_ms = int(time.time() * 1000)
        start_ts = now_ms - (limit * tf_info["minutes"] * 60 * 1000)

    end_ts = start_ts + (limit * tf_info["minutes"] * 60 * 1000)
    cached = _symbol_source.get(symbol)

    sources = [
        ("okx", lambda: _fetch_from_okx(client, symbol, tf_info["okx_bar"], limit, start_ts, end_ts)),
        ("proxy", lambda: _fetch_from_proxy(client, symbol, tf_info["bybit_interval"], limit, start_ts)),
    ]

    if cached == "proxy":
        sources = [sources[1], sources[0]]

    for src_name, fetch_fn in sources:
        raw = await fetch_fn()
        if raw:
            parsed = []
            for row in raw:
                try:
                    parsed.append(_parse_kline(row))
                except (ValueError, IndexError):
                    continue
            if parsed:
                _symbol_source[symbol] = src_name
                return parsed, src_name

    return [], "none"


async def _store_klines(symbol: str, tf: str, klines: list[tuple]) -> int:
    """Insert klines into DB. Returns count of newly inserted rows.
    pg_execute returns status string like 'INSERT 0 1' (1 row) or 'INSERT 0 0' (conflict).
    """
    inserted = 0
    for k in klines:
        open_time, o, h, l, c, vol, turnover = k
        try:
            status = await pg_execute("""
                INSERT INTO klines_history (symbol, timeframe, open_time, open, high, low, close, volume, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, timeframe, open_time) DO NOTHING
            """, (symbol, tf, open_time, o, h, l, c, vol, turnover))
            if isinstance(status, str) and status.endswith(" 1"):
                inserted += 1
        except Exception:
            pass
    return inserted


async def _get_symbols() -> list[str]:
    """Get all symbols to collect: WATCHLIST + open + recently-closed (48h).

    v0.19.2 2026-04-04: Extended to include symbols closed in last 48h.
    Ensures kline data continues after position close for:
      - what_if_outcomes post-close analysis (needs 24h of klines)
      - Scout shadow PnL resolution (needs 15m klines post-signal)
      - ML labeler backfill (future_pnl_12h, future_pnl_max_24h)
    """
    symbols = set(config.WATCHLIST_SYMBOLS)
    try:
        rows = await pg_fetch_all(
            """SELECT DISTINCT symbol FROM open_positions
               WHERE status = 'open'
                  OR (status = 'closed' AND closed_at > datetime('now', '-48 hours'))"""
        )
        for r in rows:
            symbols.add(r["symbol"])
    except Exception:
        pass
    return sorted(symbols)


async def _needs_backfill() -> bool:
    """Check if klines_history is empty (first run)."""
    try:
        row = await pg_fetch_one("SELECT COUNT(*) as n FROM klines_history")
        return row and row["n"] == 0
    except Exception:
        return True


async def _backfill(client: httpx.AsyncClient, symbols: list[str]):
    """One-time backfill: fetch 100 candles per symbol/TF."""
    logger.info(f"Kline backfill starting: {len(symbols)} symbols x {len(_TIMEFRAMES)} TF")
    total = 0
    for symbol in symbols:
        for tf_info in _TIMEFRAMES:
            now_ms = int(time.time() * 1000)
            start_ts = now_ms - (100 * tf_info["minutes"] * 60 * 1000)
            klines, src = await _fetch_klines(client, symbol, tf_info, limit=100, start_ts=start_ts)
            if klines:
                n = await _store_klines(symbol, tf_info["tf"], klines)
                total += n
            await asyncio.sleep(_STAGGER_MS / 1000)
    logger.info(f"Kline backfill complete: {total} candles stored")


async def kline_collector_loop():
    """Main background loop for kline collection.
    v0.19.0 2026-04-04: Initial implementation.
    """
    from main import _task_heartbeats

    await asyncio.sleep(10)
    logger.info("Kline Collector started (v0.19.0)")

    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
        symbols = await _get_symbols()

        if await _needs_backfill():
            try:
                await _backfill(client, symbols)
            except Exception as e:
                logger.error(f"Kline backfill error: {e}")

        while True:
            cycle_start = time.time()
            try:
                symbols = await _get_symbols()
                inserted_total = 0
                from_okx = 0
                from_proxy = 0
                failed = 0

                for symbol in symbols:
                    for tf_info in _TIMEFRAMES:
                        klines, src = await _fetch_klines(client, symbol, tf_info, limit=2)
                        if klines:
                            n = await _store_klines(symbol, tf_info["tf"], klines)
                            inserted_total += n
                            if src == "okx":
                                from_okx += 1
                            elif src == "proxy":
                                from_proxy += 1
                        else:
                            failed += 1
                        await asyncio.sleep(_STAGGER_MS / 1000)

                elapsed = round(time.time() - cycle_start, 1)
                _task_heartbeats["kline_collector"] = {
                    "last_success": time.time(),
                    "symbols": len(symbols),
                    "inserted": inserted_total,
                    "from_okx": from_okx,
                    "from_proxy": from_proxy,
                    "failed": failed,
                    "elapsed_s": elapsed,
                }

                if inserted_total > 0 or failed > 0:
                    logger.info(
                        f"Kline cycle: +{inserted_total} rows, "
                        f"okx={from_okx} proxy={from_proxy} fail={failed}, "
                        f"{elapsed}s"
                    )

            except Exception as e:
                logger.error(f"Kline collector cycle error: {e}")
                _task_heartbeats["kline_collector"] = {
                    "last_error": time.time(),
                    "error": str(e)[:200],
                }

            await asyncio.sleep(config.KLINE_COLLECT_INTERVAL_SEC)
