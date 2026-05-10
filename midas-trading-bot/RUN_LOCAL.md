# Запуск без Docker

## 1. PostgreSQL (если ещё нет)

```bash
sudo apt update
sudo apt install -y postgresql postgresql-client
sudo -u postgres createuser -s botuser 2>/dev/null || true
sudo -u postgres psql -c "ALTER USER botuser WITH PASSWORD 'botpass';"
sudo -u postgres createdb -O botuser trading_bot 2>/dev/null || true
```

В `.env` должна быть строка:
```
DATABASE_URL=postgresql+asyncpg://botuser:botpass@localhost:5432/trading_bot
```

## 2. Зависимости и сессия Telegram

```bash
cd /opt/trading-bot/midas-trading-bot

python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

**Один раз** создать сессию (ввести номер телефона и код из Telegram):

```bash
chmod +x create_session.sh
./create_session.sh
```

## 3. Запуск бота

```bash
chmod +x run.sh
./run.sh
```

Или вручную:

```bash
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Открыть в браузере: http://localhost:8000
