"""
Kline (candlestick) fetcher with multi-exchange fallback.
v0.18.0 2026-04-01: Extracted from main.py to resolve circular import
                     (exit_quality.py needs klines but cannot import main.py).
v0.18.5 2026-04-03: OKX tries -USDT-SWAP (perpetual) before -USDT (spot);
                     fixes 6/16 symbols (AERO, FARTCOIN, GRASS, RIVER, SYRUP, TAO).
v0.19.0 2026-04-04: Added 4h interval support (bybit_interval="240", okx="4H").
                     OKX end_ms calculation now handles all standard intervals.

Fallback chain:
  1. OKX       — geo-free; tries SWAP then spot instId
  2. Binance   — geo-free, limited pairs
  3. Bybit     — geo-restricted (may 403)
  4. Local Proxy — last resort (recent data only, ~24 h window)

All returned klines are ordered Oldest → Newest.
"""
import logging

import httpx

from config import config

logger = logging.getLogger("risk-engine.kline_fetcher")


async def fetch_klines_with_fallbacks(
    symbol: str, start_ts: int, limit: int,
    bybit_interval: str, okx_interval: str,
) -> list:
    """
    Fetch chronologically ordered historical klines with 4-tier fallback.

    Args:
        symbol:         e.g. "BTCUSDT"
        start_ts:       start timestamp in milliseconds
        limit:          number of candles requested
        bybit_interval: Bybit interval string ("15", "60", etc.)
        okx_interval:   OKX interval string ("15m", "1H", etc.)

    Returns:
        List of klines [ts, open, high, low, close, ...] ordered Oldest→Newest,
        or [] if all sources fail.

    v0.14.2 2026-03-29: initial implementation inside main.py.
    v0.18.0 2026-04-01: extracted to standalone module.
    v0.18.5 2026-04-03: OKX SWAP-first (perpetual before spot).
    """
    async with httpx.AsyncClient(timeout=8.0) as client:
        # 1. OKX — try perpetual SWAP first, then spot
        okx_base = (symbol[:-4] + "-USDT") if symbol.endswith("USDT") else symbol
        _interval_minutes = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30,
                             "60": 60, "120": 120, "240": 240, "360": 360, "720": 720}
        mins = _interval_minutes.get(bybit_interval, 5)
        end_ms = start_ts + (limit * mins * 60 * 1000)

        for okx_suffix in ("-SWAP", ""):
            try:
                okx_inst = okx_base + okx_suffix
                okx_url = (
                    f"https://www.okx.com/api/v5/market/history-candles"
                    f"?instId={okx_inst}&bar={okx_interval}"
                    f"&after={end_ms}&before={start_ts}&limit={min(100, limit)}"
                )
                r = await client.get(okx_url)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    if data:
                        data.reverse()
                        return data
            except Exception:
                pass

        # 2. Binance US
        binance_interval = okx_interval.lower()
        if binance_interval == "1h":
            binance_interval = "1h"
        try:
            b_url = (
                f"https://api.binance.us/api/v3/klines"
                f"?symbol={symbol}&interval={binance_interval}"
                f"&startTime={start_ts}&limit={limit}"
            )
            r = await client.get(b_url)
            if r.status_code == 200:
                data = r.json()
                if data:
                    return data
        except Exception:
            pass

        # 3. Bybit Public API
        try:
            bybit_url = (
                f"https://api.bybit.com/v5/market/kline"
                f"?category=linear&symbol={symbol}&interval={bybit_interval}"
                f"&limit={limit}&start={start_ts}"
            )
            r = await client.get(bybit_url)
            if r.status_code == 200:
                klines = r.json().get("result", {}).get("list", [])
                if klines:
                    klines.reverse()
                    return klines
        except Exception:
            pass

        # 4. Local Proxy Fallback
        try:
            proxy_url = (
                f"{config.BYBIT_PROXY_URL}/klines/{symbol}"
                f"?interval={bybit_interval}&limit={limit}&start={start_ts}"
            )
            r = await client.get(proxy_url)
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

    return []
