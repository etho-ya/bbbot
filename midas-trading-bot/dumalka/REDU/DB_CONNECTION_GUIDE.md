# PostgreSQL Connection Guide for AI Agents

> **Цель:** Инструкция для AI-ассистентов по работе с БД Risk Engine без зависаний.  
> **БД:** PostgreSQL 18.3 · `riskengine_db` · User: `riskengine` · Pass: `riskengine123`

---

## ⚡ Золотые правила

1. **НИКОГДА не оставляй открытые psql соединения.** Каждый незакрытый `psql` блокирует слот подключения.
2. **Один запрос — один процесс.** Не запускай новый запрос, пока предыдущий не завершился.
3. **Убивай зомби в начале сессии:** `pkill -9 -f "psql.*riskengine" 2>/dev/null`
4. **Используй таймауты.** При `WaitMsBeforeAsync` ставь 10000 (10 сек), не больше.

---

## ✅ Рекомендуемый способ: Python psycopg2 (одноразовое соединение)

```bash
python3 -c "
import psycopg2, json
conn = psycopg2.connect('postgresql://riskengine:riskengine123@/riskengine_db', 
                        connect_timeout=5)
conn.set_session(autocommit=True)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM signals')
print(cur.fetchone()[0])
conn.close()
"
```

**Преимущества:** автоматическое закрытие, таймаут, нет pager-проблем.

---

## ✅ psql CLI (если нужен — ТОЛЬКО с флагами)

```bash
sudo -u postgres psql -d riskengine_db --no-psqlrc -t -A -c "SELECT COUNT(*) FROM signals;"
```

| Флаг | Зачем |
|---|---|
| `--no-psqlrc` | Не загружать пользовательские настройки |
| `-t` | Без заголовков таблиц |
| `-A` | Без выравнивания (быстрый вывод) |
| `-c "..."` | Одноразовая команда, не интерактивный режим |

**⚠️ СТРОГОЕ ПРАВИЛО ДЛЯ AI (Извлеченный урок):**
Отныне при работе с БД Risk Engine вы **ОБЯЗАНЫ** переопределять конфигурацию `psql`, принудительно отключая пейджер на уровне команды! Иначе ваш процесс `psql` зависнет в ожидании пробела, оставив зомби-сессию, которая заблокирует порт 8000 и слоты базы данных:
- Используйте флаги: `psql -P pager=off` (или `-t -A`), чтобы вывод **никогда не блокировал** терминал.
- При использовании Python `psycopg2` **всегда** жестко устанавливайте `connect_timeout=5`.

**⚠️ НИКОГДА не используй интерактивный `psql` без `-c`.** Если psql зависнет в pager, процесс останется навсегда.

---

## ❌ Чего НЕ делать

```bash
# ПЛОХО: TCP через 127.0.0.1 (медленнее на 2-3x)
PGPASSWORD=riskengine123 psql -h 127.0.0.1 -U riskengine -d riskengine_db

# ПЛОХО: Интерактивный psql (может зависнуть в pager)
psql -d riskengine_db

# ПЛОХО: Запуск нового psql пока предыдущий ещё работает
# → Каждый процесс = новый слот подключения → лавина зомби
```

---

## 🧹 Аварийная очистка зомби-процессов

Если запросы начали зависать — вероятно, накопились зомби. Выполни:

```bash
# 1. Убить все psql/python зомби
kill -9 $(ps aux | grep -E "psql|python3.*psyco" | grep -v grep | awk '{print $2}') 2>/dev/null

# 2. Проверить активные соединения внутри PG
sudo -u postgres psql -d riskengine_db --no-psqlrc -t -A -c \
  "SELECT pid, state, left(query,60) FROM pg_stat_activity WHERE datname='riskengine_db' AND state != 'idle';"

# 3. Принудительно завершить зависшие PG-бэкенды
sudo -u postgres psql -d riskengine_db --no-psqlrc -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='riskengine_db' AND state='active' AND query_start < now() - interval '2 minutes';"
```

---

## 📊 Ключевые таблицы и индексы

| Таблица | Строк (~) | Ключевые индексы | Примечания (v0.16.1) |
|---|---|---|---|
| `signals` | ~500+ | PK `id`, UNIQUE `signal_hash`, CHECK `side` | +shadow_resolved_at, +corrected_score, +score_quality_penalty |
| `open_positions` | ~400 | PK `id`, `idx_positions_signal_hash`, CHECK `status` in (open,closed,phantom) | FK → signals.signal_hash |
| `position_snapshots` | ~72K | PK `id`, `idx_snapshots_signal_hash` | 29 ML features |
| `trade_outcomes` | ~400 | PK `id`, `idx_outcomes_signal_hash` | FK → signals.signal_hash |
| `watchlist_alerts` | ~340 | PK `id` | |

---

## 🔑 Строки подключения

```
# Standard TCP (используется приложением)
postgresql://riskengine:riskengine123@127.0.0.1:5432/riskengine_db

# Unix Socket (рекомендуется для CLI — быстрее)
sudo -u postgres psql -d riskengine_db

# Python psycopg2 через Unix Socket
psycopg2.connect('postgresql://riskengine:riskengine123@/riskengine_db')
```
