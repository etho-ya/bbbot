import asyncio
from pybit.unified_trading import HTTP
import os

from app.core.config import settings

client = HTTP(testnet=settings.BYBIT_TESTNET, api_key=settings.BYBIT_API_KEY, api_secret=settings.BYBIT_API_SECRET)
try:
    print(client.cancel_all_orders(category="linear", symbol="BTCUSDT"))
except Exception as e:
    print("Error:", e)
