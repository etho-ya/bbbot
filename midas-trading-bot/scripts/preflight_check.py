import asyncio
import os
from dotenv import load_dotenv

# Load env vars
load_dotenv(".env")

from app.services.bybit_client import bybit_client
from app.core.config import settings
from app.core.logger import logger

async def run_preflight_check():
    print("=== MIDAS BOT PRE-FLIGHT CHECK ===")
    
    # 1. Check Config
    print(f"\n[1] Configuration:")
    print(f"    - Bybit Testnet: {settings.BYBIT_TESTNET}")
    print(f"    - LLM Validation: {settings.LLM_VALIDATION_ENABLED}")
    print(f"    - OpenRouter Model: {settings.OPENROUTER_MODEL}")
    
    # 2. Check Bybit Connectivity & Permissions
    print(f"\n[2] Bybit Connectivity:")
    balance = await bybit_client.get_wallet_balance(
        settings.BYBIT_API_KEY, 
        settings.BYBIT_API_SECRET, 
        settings.BYBIT_TESTNET
    )
    
    if balance > 0:
        print(f"    ✅ Connected! Balance: {balance:.2f} USDT")
    else:
        print(f"    ❌ Connection failed or Balance is 0. Check API keys and permissions.")
        return

    # 3. Check Symbol Access (Test with BTCUSDT)
    print(f"\n[3] Symbol Access:")
    btc_price = await bybit_client.get_current_price("BTCUSDT")
    if btc_price:
        print(f"    ✅ BTCUSDT Price: {btc_price}")
    else:
        print(f"    ❌ Cannot get BTCUSDT price.")

    # 4. Check API Permissions (Try to get positions)
    print(f"\n[4] API Permissions:")
    try:
        from pybit.unified_trading import HTTP
        client = HTTP(
            testnet=settings.BYBIT_TESTNET,
            api_key=settings.BYBIT_API_KEY,
            api_secret=settings.BYBIT_API_SECRET,
        )
        resp = client.get_positions(category="linear", settleCoin="USDT")
        if resp["retCode"] == 0:
            print(f"    ✅ API Permissions OK (Read/Position)")
        else:
            print(f"    ❌ API Permissions Error: {resp.get('retMsg')}")
    except Exception as e:
        print(f"    ❌ API Permissions Exception: {e}")

    print("\n=== CHECK COMPLETE ===")
    if balance > 0 and btc_price:
        print("\n🚀 Bot is READY for trading!")
    else:
        print("\n⚠️  Bot is NOT ready. Please fix errors above.")

if __name__ == "__main__":
    asyncio.run(run_preflight_check())
