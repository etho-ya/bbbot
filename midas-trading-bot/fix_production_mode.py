#!/usr/bin/env python3
"""
Скрипт для переключения пользователя на Production API (вместо Testnet)
"""
import asyncio
import sys

async def main():
    try:
        # Импортируем после добавления в path
        sys.path.insert(0, '/opt/trading-bot/trading-bot')
        from app.core.database import async_session
        from app.models.db_models import UserDB
        from sqlalchemy import select, update
        
        print("🔄 Проверяем настройки пользователей...")
        
        async with async_session() as session:
            # Получаем всех пользователей
            result = await session.execute(select(UserDB))
            users = result.scalars().all()
            
            if not users:
                print("⚠️  Пользователи не найдены в БД!")
                return
            
            print(f"\n📊 Найдено пользователей: {len(users)}\n")
            
            for user in users:
                print(f"User ID: {user.id}")
                print(f"  Username: {user.username}")
                print(f"  Current testnet: {user.is_testnet}")
                print(f"  API Key: {'✅ Настроен' if user.bybit_api_key else '❌ Не настроен'}")
                
                if user.is_testnet:
                    print(f"  ⚠️  Пользователь использует TESTNET API!")
                    
                    # Спрашиваем подтверждение
                    response = input(f"\n  Переключить пользователя '{user.username}' на PRODUCTION API? (yes/no): ")
                    
                    if response.lower() in ['yes', 'y', 'да']:
                        # Обновляем настройку
                        await session.execute(
                            update(UserDB)
                            .where(UserDB.id == user.id)
                            .values(is_testnet=False)
                        )
                        await session.commit()
                        print(f"  ✅ Пользователь переключен на PRODUCTION API!")
                    else:
                        print(f"  ⏭️  Пропущено")
                else:
                    print(f"  ✅ Пользователь уже использует PRODUCTION API")
                
                print()
        
        print("✅ Готово!\n")
        print("🔄 Перезапустите бота для применения изменений:")
        print("   ./restart_bot.sh")
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
