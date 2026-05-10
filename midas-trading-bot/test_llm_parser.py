import asyncio
import os
from dotenv import load_dotenv

# Load env vars before importing config
load_dotenv(".env")

from app.services.llm_parser import parse_signal_with_llm
from app.core.config import settings

# Sample messages
SAMPLES = [
    """
    #BTCUSDT
    LONG
    Entry: 65000 - 65500
    Targets: 66000, 67000, 68000
    Stop: 64000
    Leverage: 10x
    """,
    """
    Всем привет! Рынок сегодня скучный, ждем новостей.
    Не забывайте подписываться на канал!
    """,
    """
    SHORT #ETH
    Entry: 3500
    Take-Profit: 3400
    Stop-Loss: 3550
    """
]

async def run_tests():
    print(f"Testing with Model: {settings.OPENROUTER_MODEL}")
    print(f"API Key present: {bool(settings.OPENROUTER_API_KEY)}")
    
    for i, text in enumerate(SAMPLES):
        print(f"\n--- Test Case {i+1} ---")
        print(f"Input: {text.strip()[:50]}...")
        result = await parse_signal_with_llm(text)
        print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(run_tests())
