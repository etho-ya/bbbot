#!/bin/bash
# Скрипт для перезапуска торгового бота

echo "🔄 Перезапуск торгового бота..."

# Находим процесс бота
BOT_PID=$(ps aux | grep -E "uvicorn app.main" | grep -v grep | awk '{print $2}')

if [ -z "$BOT_PID" ]; then
    echo "⚠️  Бот не запущен. Запускаем..."
else
    echo "🛑 Останавливаем бот (PID: $BOT_PID)..."
    kill $BOT_PID
    sleep 2
    
    # Проверяем, что процесс завершился
    if ps -p $BOT_PID > /dev/null 2>&1; then
        echo "⚠️  Процесс не завершился, принудительно останавливаем..."
        kill -9 $BOT_PID
        sleep 1
    fi
    echo "✅ Бот остановлен"
fi

# Переходим в директорию бота
cd /opt/trading-bot/midas-trading-bot

# Запускаем бота
echo "🚀 Запускаем бота..."
nohup /opt/trading-bot/midas-trading-bot/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /dev/null 2>&1 &

sleep 2

# Проверяем, что бот запустился
NEW_PID=$(ps aux | grep -E "uvicorn app.main" | grep -v grep | awk '{print $2}')
if [ -z "$NEW_PID" ]; then
    echo "❌ Ошибка: не удалось запустить бота"
    exit 1
else
    echo "✅ Бот успешно запущен (PID: $NEW_PID)"
    echo ""
    echo "📊 Проверить статус: ps aux | grep uvicorn"
    echo "📝 Смотреть логи: tail -f /opt/trading-bot/midas-trading-bot/logs/bot.log"
fi

