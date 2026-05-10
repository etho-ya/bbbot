#!/usr/bin/env python3
"""
Миграция: Добавление поля default_stop_loss_percent в таблицу settings
"""
import asyncio
import sys
sys.path.insert(0, '/opt/trading-bot/trading-bot')

from app.core.database import engine
from sqlalchemy import text
from app.core.logger import logger


async def migrate():
    """Добавляет поле default_stop_loss_percent в таблицу settings"""
    
    async with engine.begin() as conn:
        try:
            # Проверяем, существует ли уже поле
            result = await conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='settings' AND column_name='default_stop_loss_percent'
            """))
            
            if result.fetchone():
                print("✅ Поле default_stop_loss_percent уже существует")
                return
            
            # Добавляем поле
            await conn.execute(text("""
                ALTER TABLE settings 
                ADD COLUMN default_stop_loss_percent DOUBLE PRECISION DEFAULT 10.0
            """))
            
            print("✅ Добавлено поле default_stop_loss_percent (default=10.0)")
            
            # Обновляем существующие записи
            await conn.execute(text("""
                UPDATE settings 
                SET default_stop_loss_percent = 10.0 
                WHERE default_stop_loss_percent IS NULL
            """))
            
            print("✅ Обновлены существующие настройки (установлено 10%)")
            
        except Exception as e:
            print(f"❌ Ошибка миграции: {e}")
            raise


async def main():
    print("=" * 60)
    print("МИГРАЦИЯ: Добавление настройки безопасного Stop Loss")
    print("=" * 60)
    print()
    
    try:
        await migrate()
        print()
        print("=" * 60)
        print("✅ МИГРАЦИЯ УСПЕШНО ЗАВЕРШЕНА")
        print("=" * 60)
        print()
        print("Теперь можно настраивать процент безопасного SL для каждого пользователя!")
        print("По умолчанию: 10% (если SL не указан в сигнале)")
        
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ ОШИБКА: {e}")
        print("=" * 60)
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
