import httpx
import asyncio
import json
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot-analyzer")

WEBHOOK_URL = "http://192.168.1.244:8000/tv-webhook"
SECRET = "123QWEasd"

# Portfolio tracking for sequence of trades
portfolio_positions = []
historical_results = []

# All signals from user messages with full metadata
historical_signals = [
    {
        "symbol": "ETHUSDT",
        "side": "short",
        "size": 47.25,
        "entry_low": 1942.66,
        "entry_high": 1942.66,
        "current_equity": 15.70, 
        "source": "bybit_bot_logs",
        "risk_reward": 1.5,
        "probability": 50.0,
        "win_rate": 60.0,
        "trend": "bear",
        "midas_comment": "Opened Short ETH",
        "timestamp": "3/9/2026 2:00 AM"
    },
    {
        "symbol": "FARTCOINUSDT",
        "side": "short",
        "size": 47.12,
        "entry_low": 0.14665,
        "entry_high": 0.14637,
        "current_equity": 15.65, 
        "source": "bybit_bot_logs",
        "tp1": 0.14219,
        "tp2": 0.13934,
        "tp3": 0.13766,
        "stop_loss": 0.15105,
        "risk_reward": 1.9,
        "probability": 73.0,
        "win_rate": 85.0,
        "trend": "strong_bear",
        "trend_strength": 97.0,
        "volume_level": "medium",
        "midas_comment": "Trend strong bear, but between levels.",
        "timestamp": "3/9/2026 2:00 AM"
    },
    {
        "symbol": "FARTCOINUSDT",
        "side": "long",
        "size": 44.70,
        "entry_low": 0.14646,
        "entry_high": 0.14732,
        "current_equity": 14.89, 
        "source": "bybit_bot_logs",
        "tp1": 0.15171,
        "tp2": 0.15395,
        "tp3": 0.15555,
        "stop_loss": 0.14249,
        "risk_reward": 1.7,
        "probability": 25.0,
        "win_rate": 85.0,
        "trend": "strong_bear",
        "trend_strength": 86.0,
        "volume_level": "high",
        "midas_comment": "Strong bear trend, long into resistance (Probability 25%)",
        "timestamp": "3/9/2026 4:00 AM"
    },
    {
        "symbol": "ARCUSDT",
        "side": "short",
        "size": 17.26,
        "entry_low": 0.03504,
        "entry_high": 0.03449,
        "current_equity": 14.38, 
        "source": "bybit_bot_logs",
        "tp1": 0.02979,
        "tp2": 0.02902,
        "tp3": 0.02716,
        "stop_loss": 0.03685,
        "risk_reward": 3.1,
        "probability": 74.0,
        "win_rate": 81.0,
        "trend": "strong_bear",
        "trend_strength": 100.0,
        "volume_level": "high",
        "midas_comment": "Strong bear trend, short by trend.",
        "timestamp": "3/9/2026 5:00 AM"
    },
    {
        "symbol": "AEROUSDT",
        "side": "long",
        "size": 35.91,
        "entry_low": 0.3178,
        "entry_high": 0.3192,
        "current_equity": 14.36, 
        "source": "bybit_bot_logs",
        "tp1": 0.3229,
        "tp2": 0.3252,
        "tp3": 0.3314,
        "stop_loss": 0.3153,
        "risk_reward": 3.1,
        "probability": 47.0,
        "win_rate": 77.0,
        "trend": "strong_bear",
        "trend_strength": 79.0,
        "volume_level": "high",
        "midas_comment": "Strong bear trend, long is counter-trend.",
        "timestamp": "3/9/2026 5:00 AM"
    }
]

async def analyze_signals():
    headers = {"X-Webhook-Secret": SECRET}
    async with httpx.AsyncClient(timeout=30) as client:
        print("\n=== STARTING RISK ENGINE ANALYSIS FOR BOT TRADES ===\n")
        
        for sig in historical_signals:
            timestamp = sig.pop('timestamp', '')
            print(f"\nAnalyzing [{timestamp}] {sig['symbol']} {sig['side'].upper()} ...")
            
            # Fix: User provided "Size" in USDT, but Risk Engine expects Quantity
            entry_p = sig.get("entry_low", sig.get("entry_high", 1.0))
            if entry_p > 0 and sig["size"] > 1.0: # Heuristic: if size is > 1 and it's USDT
                size_usdt = sig["size"]
                sig["size"] = size_usdt / entry_p
                print(f"  Converted {size_usdt} USDT to {sig['size']:.4f} Qty (@ {entry_p})")

            # Attach current portfolio state
            sig['open_positions'] = portfolio_positions.copy()
            
            try:
                r = await client.post(WEBHOOK_URL, json=sig, headers=headers)
                if r.status_code == 200:
                    res = r.json()
                    rec = res['recommendation'].upper()
                    print(f"  Result: {rec} | Score: {res['signal_score']:.2f} | VaR: {res['var']*100:.2f}% | Countertrend: {res['is_countertrend']}")
                    
                    historical_results.append({
                        "symbol": sig['symbol'],
                        "side": sig['side'],
                        "timestamp": timestamp,
                        "recommendation": rec,
                        "score": res['signal_score'],
                        "var": res['var'],
                        "approved": res['approved'],
                        "comment": sig.get('midas_comment', '')
                    })
                    
                    portfolio_positions.append({
                        "symbol": sig["symbol"],
                        "side": sig["side"],
                        "size": sig["size"],
                        "entry_price": sig.get("entry_low", sig.get("entry_high", 0))
                    })
                    
                else:
                    print(f"  Error {r.status_code}: {r.text}")
            except Exception as e:
                print(f"  Failed: {e}")

        # Final Report Generation
        print("\n=== FINAL COMPARISON REPORT ===\n")
        print("Our Risk Engine Analysis Results:")
        for res in historical_results:
            status = "APPROVED" if res['approved'] else "REJECTED"
            print(f"- {res['timestamp']} | {res['symbol']} {res['side'].upper()} -> {status}")
            print(f"  Risk Engine Rec: {res['recommendation']} (Score: {res['score']:.2f}, VaR: {res['var']*100:.2f}%)")
            print(f"  Midas Setup: {res['comment']}\n")

if __name__ == "__main__":
    asyncio.run(analyze_signals())
