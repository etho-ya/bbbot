import asyncio
from src.main import fetch_klines_with_fallbacks
import time

async def main():
    start_ts = int(time.time()*1000) - (24 * 3600 * 1000)
    kl = await fetch_klines_with_fallbacks("WIFUSDT", start_ts, 192, "15", "15m")
    print(f"Len: {len(kl)}")
    if kl:
        print(f"First: {kl[0]}")
        print(f"Last: {kl[-1]}")

asyncio.run(main())
