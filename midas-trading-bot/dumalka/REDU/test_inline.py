import httpx
import asyncio
import time
import os

BYBIT_PROXY_URL = "http://100.117.168.63:8002"

async def fetch_klines_with_fallbacks(symbol: str, start_ts: int, limit: int, bybit_interval: str, okx_interval: str):
    async with httpx.AsyncClient(timeout=8.0) as client:
        # 1. OKX
        try:
            okx_symbol = symbol[:-4] + "-USDT" if symbol.endswith("USDT") else symbol
            if bybit_interval == "15":
                end_ms = start_ts + (limit * 15 * 60 * 1000)
            elif bybit_interval == "60":
                end_ms = start_ts + (limit * 60 * 60 * 1000)
            else:
                end_ms = start_ts + (limit * 5 * 60 * 1000)
                
            okx_url = f"https://www.okx.com/api/v5/market/history-candles?instId={okx_symbol}&bar={okx_interval}&after={end_ms}&before={start_ts}&limit={min(100, limit)}"
            print(f"Trying OKX: {okx_url}")
            r = await client.get(okx_url)
            print("OKX status:", r.status_code)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    data.reverse()
                    return data
            print("OKX response:", r.text[:200])
        except Exception as e:
            print("OKX Exception:", str(e))

        # 2. Binance
        binance_interval = okx_interval.lower()
        if binance_interval == "1h": binance_interval = "1h"
        try:
            b_url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval={binance_interval}&startTime={start_ts}&limit={limit}"
            print(f"Trying Binance: {b_url}")
            r = await client.get(b_url)
            print("Binance status:", r.status_code)
            if r.status_code == 200:
                data = r.json()
                if data:
                    return data
        except Exception as e:
            print("Binance Exception:", str(e))

        # 3. Bybit
        try:
            bybit_url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_interval}&limit={limit}&start={start_ts}"
            print(f"Trying Bybit: {bybit_url}")
            r = await client.get(bybit_url)
            print("Bybit status:", r.status_code)
            if r.status_code == 200:
                klines = r.json().get("result", {}).get("list", [])
                if klines:
                    klines.reverse()
                    return klines
        except Exception as e:
            print("Bybit Exception:", str(e))

        # 4. Proxy
        try:
            proxy_url = f"{BYBIT_PROXY_URL}/klines/{symbol}?interval={bybit_interval}&limit={limit}&start={start_ts}"
            print(f"Trying Proxy: {proxy_url}")
            r = await client.get(proxy_url)
            print("Proxy status:", r.status_code)
            if r.status_code == 200:
                data = r.json()
                klines = data.get("result", {}).get("list", [])
                if not klines:
                    klines = data.get("klines", data) if isinstance(data, dict) else data
                if isinstance(klines, list) and klines:
                    klines.reverse()
                    return klines
        except Exception as e:
            print("Proxy Exception:", str(e))
    return []

async def test():
    # exactly 24 hours ago
    start_ts = int(time.time()*1000) - (24 * 3600 * 1000)
    kl = await fetch_klines_with_fallbacks("WIFUSDT", start_ts, 192, "15", "15m")
    print(f"Final Fallback Length: {len(kl)}")

asyncio.run(test())
