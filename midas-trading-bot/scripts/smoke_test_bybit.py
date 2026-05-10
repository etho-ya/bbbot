"""Bybit API smoke test — verify balance read and order placement.

Usage:
    uv run python scripts/smoke_test_bybit.py       # Check balance only
    uv run python scripts/smoke_test_bybit.py --order # Check balance + test order

Set BYBIT_TESTNET=true in .env for safe testing.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"


def test_balance(client: HTTP) -> dict | None:
    """Fetch USDT balance from Bybit unified account."""
    resp = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    ret = resp.get("retCode", -1)
    print(f"[BALANCE] retCode={ret}  msg={resp.get('retMsg')}")

    if ret != 0:
        print(f"  ERROR: {resp}")
        return None

    coins = resp["result"]["list"][0].get("coin", [])
    result = {}
    for c in coins:
        equity = float(c.get("equity", "0"))
        wallet = float(c.get("walletBalance", "0"))
        unrealised = float(c.get("unrealisedPnl", "0"))
        result[c["coin"]] = {"equity": equity, "wallet": wallet, "unrealised": unrealised}
        print(f"  {c['coin']}: equity={equity:.2f}  wallet={wallet:.2f}  unrealisedPnl={unrealised:.2f}")

    if not result:
        print("  WARNING: No coin balances found")
    return result


def test_single_market_order(client: HTTP) -> bool:
    """Place a minimal BTCUSDT market order (testnet only!)."""
    if not TESTNET:
        print("[ORDER]  SKIPPED — BYBIT_TESTNET=false, refusing live order")
        return False

    print("[ORDER]  Placing test market order on testnet...")
    resp = client.place_order(
        category="linear",
        symbol="BTCUSDT",
        side="Buy",
        orderType="Market",
        qty="0.001",
        isLeverage=1,
        positionIdx=0,
        reduceOnly=False,
    )
    ret = resp.get("retCode", -1)
    print(f"[ORDER]  retCode={ret}  msg={resp.get('retMsg')}")
    if ret == 0:
        order_id = resp["result"]["orderId"]
        print(f"  orderId={order_id}  OK")
        return True
    else:
        print(f"  FAILED: {resp}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit API smoke test")
    parser.add_argument("--order", action="store_true", help="Also place a test order (testnet only)")
    args = parser.parse_args()

    if not API_KEY or not API_SECRET:
        print("ERROR: BYBIT_API_KEY / BYBIT_API_SECRET not set in .env")
        return

    print(f"=== Bybit Smoke Test (testnet={TESTNET}) ===")

    client = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)
    balances = test_balance(client)

    if args.order:
        test_single_market_order(client)

    if balances:
        print("\n=== Smoke test PASSED ===")
    else:
        print("\n=== Smoke test FAILED ===")


if __name__ == "__main__":
    main()
