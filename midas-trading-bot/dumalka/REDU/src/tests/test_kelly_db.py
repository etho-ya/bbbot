import json
import asyncio
import sys
import os

# Add parent dir to path so we can import config & db
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from db import get_recent_signals

async def test_db():
    rows = await get_recent_signals(limit=1)
    if not rows: 
        print("No rows found")
        return
    r = rows[0]
    res = json.loads(r["risk_result"])
    print(f"Kelly Suggested USD: {res.get('kelly_suggested_size_usd')}")
    print(f"Exposure Warning: {res.get('exposure_warning')}")

if __name__ == "__main__":
    asyncio.run(test_db())
