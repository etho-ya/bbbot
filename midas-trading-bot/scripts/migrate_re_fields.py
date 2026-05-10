import asyncio
import sys
import logging
from sqlalchemy import text
from app.core.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def migrate():
    logger.info("Starting schema migration for Risk Engine fields...")
    async with engine.begin() as conn:
        commands = [
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS re_status VARCHAR;",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS re_score FLOAT;",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS re_reason TEXT;",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS re_responded_at TIMESTAMP;"
        ]
        
        for cmd in commands:
            try:
                await conn.execute(text(cmd))
                logger.info(f"Executed: {cmd}")
            except Exception as e:
                logger.error(f"Error on {cmd}: {e}")
                
    logger.info("Migration complete. Disposing engine.")
    await engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())
