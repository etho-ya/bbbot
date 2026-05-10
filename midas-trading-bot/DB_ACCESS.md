# Доступ к базе данных (сигналы и прочие данные)

## Креды PostgreSQL

| Параметр    | Значение      |
|------------|----------------|
| Хост       | `localhost` (если заходишь с той же машины, где крутится Docker) или IP сервера (если порт 5432 проброшен наружу) |
| Порт       | `5432`         |
| База       | `trading_bot`  |
| Пользователь | `botuser`   |
| Пароль     | `botpass`     |

**Строка подключения:**  
`postgresql://botuser:botpass@localhost:5432/trading_bot`

---

## Способ 1: Зайти в БД с сервера (рекомендуется)

Если у партнёра есть SSH на сервер, где запущен бот:

```bash
# Перейти в каталог проекта
cd /opt/trading-bot/midas-trading-bot

# Запустить psql внутри контейнера БД
docker compose exec db psql -U botuser -d trading_bot
```

Дальше можно вводить SQL-запросы. Выход: `\q`.

---

## Способ 2: Подключиться с другой машины (DBeaver, pgAdmin и т.п.)

Сейчас порт PostgreSQL **не проброшен** наружу. Чтобы подключаться с другой машины:

1. Пробросить порт в `docker-compose.yml` у сервиса `db`:

```yaml
  db:
    image: postgres:15
    restart: always
    ports:
      - "127.0.0.1:5432:5432"   # добавить эти строки
    environment:
      ...
```

2. Перезапустить: `docker compose up -d`
3. На своей машине использовать SSH-туннель до сервера (без туннеля подключаться к 5432 в интернет не стоит):

```bash
ssh -L 5432:127.0.0.1:5432 user@IP_СЕРВЕРА
```

4. В клиенте (DBeaver / pgAdmin) указать:
   - Host: `127.0.0.1` (или `localhost`)
   - Port: `5432`
   - Database: `trading_bot`
   - User: `botuser`
   - Password: `botpass`

---

## Таблица сигналов и полезные запросы

Все сигналы лежат в таблице **`signals`**.

### Основные столбцы

| Столбец          | Описание |
|------------------|----------|
| id               | Уникальный ID сигнала |
| user_id          | ID пользователя (обычно 1) |
| raw_text         | Исходный текст сообщения из Telegram |
| parsed_data      | JSON с распарсенными полями (символ, сторона, вход, цели, стоп и т.д.) |
| channel_id       | Откуда пришло (например ID бота) |
| channel_title    | Название источника |
| signal_type      | `trade` — торговый, `auxiliary` — вспомогательный |
| is_processed     | `true` — по сигналу открыли сделку |
| llm_decision     | Решение LLM: `approve` / `reject` |
| llm_reason       | Текст причины от LLM |
| llm_model        | Модель LLM |
| created_at       | Время получения сигнала |

### Примеры запросов

**Последние 50 сигналов (все поля):**
```sql
SELECT id, user_id, signal_type, is_processed, llm_decision, created_at, left(raw_text, 200) AS raw_preview
FROM signals
ORDER BY created_at DESC
LIMIT 50;
```

**Только торговые сигналы за последние 7 дней:**
```sql
SELECT id, signal_type, is_processed, llm_decision, llm_reason, created_at, raw_text
FROM signals
WHERE signal_type = 'trade'
  AND created_at >= now() - interval '7 days'
ORDER BY created_at DESC;
```

**Сигналы, по которым открыли сделку:**
```sql
SELECT s.id, s.created_at, s.raw_text, s.parsed_data, s.llm_decision, s.llm_reason
FROM signals s
WHERE s.is_processed = true
ORDER BY s.created_at DESC;
```

**Экспорт в CSV (из psql на сервере):**
```bash
docker compose exec db psql -U botuser -d trading_bot -c "\COPY (SELECT id, user_id, signal_type, is_processed, llm_decision, llm_reason, created_at, raw_text FROM signals ORDER BY created_at DESC) TO STDOUT WITH CSV HEADER" > signals_export.csv
```
Файл `signals_export.csv` появится в текущей директории на сервере; его можно скачать по scp/sftp.

---

## Связанная таблица сделок

Сделки в таблице **`trades`** связаны с сигналами по полю **`signal_id`** (ссылка на `signals.id`).

**Сигналы и их сделки:**
```sql
SELECT s.id AS signal_id, s.created_at AS signal_time, s.signal_type, s.llm_decision,
       t.id AS trade_id, t.symbol, t.side, t.entry_price, t.status, t.profit_loss, t.closed_at
FROM signals s
LEFT JOIN trades t ON t.signal_id = s.id
WHERE s.signal_type = 'trade'
ORDER BY s.created_at DESC
LIMIT 50;
```
