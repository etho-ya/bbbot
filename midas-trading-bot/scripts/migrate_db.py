# scripts/migrate_db.py
import asyncio
import sys
import os

# Добавляем корневую директорию в path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import engine
from app.core.logger import logger

async def migrate():
    logger.info("Starting database migration...")
    
    # Список команд для изменения таблиц
    # Используем IF NOT EXISTS через try/except или проверку колонок
    commands = [
        # Таблица users
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR UNIQUE NOT NULL,
            password_hash VARCHAR NOT NULL,
            bybit_api_key VARCHAR,
            bybit_api_secret VARCHAR,
            is_testnet BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        # Добавляем user_id в существующие таблицы
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE settings ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",

        # Убираем уникальность channel_id (теперь она будет на паре user_id, channel_id)
        # В некоторых версиях Postgres это может быть сложнее, но попробуем
        "ALTER TABLE channels DROP CONSTRAINT IF EXISTS channels_channel_id_key",
        
        # Исправляем настройки (settings)
        "ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_pkey CASCADE",
        "ALTER TABLE settings ADD COLUMN IF NOT EXISTS id_new SERIAL PRIMARY KEY",
        # (Упрощенно - если id уже есть как PK, просто убедимся что есть и user_id)
    ]

    for cmd in commands:
        async with engine.begin() as conn:
            try:
                logger.info(f"Executing: {cmd}")
                await conn.execute(text(cmd))
            except Exception as e:
                logger.warning(f"Error executing command '{cmd}': {e}")
                
    logger.info("Migration finished.")

if __name__ == "__main__":
    asyncio.run(migrate())
