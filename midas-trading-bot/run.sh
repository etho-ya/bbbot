#!/bin/bash
# Запуск бота без Docker: venv + uvicorn (PostgreSQL должен быть запущен отдельно)

set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "=== Создаю venv и ставлю зависимости ==="
  python3 -m venv venv
  venv/bin/pip install --upgrade pip
  venv/bin/pip install -r requirements.txt
  echo "OK"
fi

# Загрузка .env
export $(grep -v '^#' .env | xargs)

echo "=== Запуск бота (uvicorn) ==="
exec venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
