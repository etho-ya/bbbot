#!/bin/bash
# Запуск приложения в Docker (БД postgres в отдельном контейнере).
# Требуется: Docker и Docker Compose. .env должен быть в текущей директории.
set -e
cd "$(dirname "$0")"
docker compose up -d --build
echo "App: http://localhost:8000"
echo "Logs: docker compose logs -f bot"
