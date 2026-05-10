
import httpx
import asyncio
import json

WEBHOOK_URL = "http://localhost:8000/tv-webhook"
SECRET = "123QWEasd"

signals = [
    {
        "symbol": "SOLUSDT",
        "side": "short",
        "size": 0.5,
        "entry_low": 82.38,
        "entry_high": 83.14,
        "stop_loss": 84.39,
        "tp1": 81.48,
        "tp2": 79.03,
        "tp3": 78.38,
        "probability": 65.0, # Estimate based on Midas style
        "win_rate": 58.0,
        "risk_reward": 2.5,
        "trend": "bear",
        "current_equity": 15.66,
        "source": "bot_logs"
    },
    {
        "symbol": "ETHUSDT",
        "side": "short",
        "size": 0.02,
        "entry_low": 1943.24,
        "entry_high": 1961.63,
        "stop_loss": 1991.31,
        "tp1": 1913.6,
        "tp2": 1891.11,
        "tp3": 1869.31,
        "probability": 60.0,
        "win_rate": 55.0,
        "risk_reward": 2.2,
        "trend": "bear",
        "current_equity": 15.66,
        "source": "bot_logs"
    },
    {
        "symbol": "FARTCOINUSDT",
        "side": "short",
        "size": 319,
        "entry_low": 0.14693,
        "entry_high": 0.14771,
        "stop_loss": 0.15053,
        "tp1": 0.14219,
        "tp2": 0.13934,
        "tp3": 0.13766,
        "probability": 70.0,
        "win_rate": 60.0,
        "risk_reward": 3.0,
        "trend": "bear",
        "current_equity": 15.58,
        "source": "bot_logs"
    },
    {
        "symbol": "ETHUSDT",
        "side": "long",
        "size": 0.02,
        "entry_low": 1940.97,
        "entry_high": 1960.88,
        "stop_loss": 1909.07,
        "tp1": 1980.02,
        "tp2": 1996.13,
        "tp3": 2052.13,
        "probability": 68.0,
        "win_rate": 62.0,
        "risk_reward": 2.8,
        "trend": "strong_bull",
        "current_equity": 16.79,
        "source": "bot_logs"
    }
]

async def run_import():
    headers = {"X-Webhook-Secret": SECRET}
    async with httpx.AsyncClient() as client:
        for sig in signals:
            print(f"Importing {sig['symbol']} {sig['side']}...")
            try:
                # Note: We are running this on the host, but the Risk Engine is in VM 106.
                # However, I can run this via 'qm guest exec' or use the Cloudflare tunnel.
                # Since I am in the console, I'll recommend running this INSIDE the VM.
                r = await client.post(WEBHOOK_URL, json=sig, headers=headers)
                print(f"Status: {r.status_code}")
                # print(r.json())
            except Exception as e:
                print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_import())
