# Midas Trading Bot — Полная техническая документация

> **Версия:** Phase 4.0 (Март 2026)  
> **Язык:** Python 3.13+ | FastAPI | Telethon | pybit | PostgreSQL  
> **Сервер:** VPS (Docker), Risk Engine на Proxmox VM с Titan V GPU

---

## Содержание

1. [Обзор системы](#1-обзор-системы)
2. [Архитектура](#2-архитектура)
3. [Структура проекта](#3-структура-проекта)
4. [Модули и сервисы](#4-модули-и-сервисы)
5. [База данных](#5-база-данных)
6. [Поток обработки сигнала](#6-поток-обработки-сигнала)
7. [Жизненный цикл сделки](#7-жизненный-цикл-сделки)
8. [Risk Engine (RE)](#8-risk-engine-re)
9. [Думалка (Dumalka)](#9-думалка-dumalka)
10. [LLM-интеграция](#10-llm-интеграция)
11. [Уведомления](#11-уведомления)
12. [Конфигурация (.env)](#12-конфигурация-env)
13. [Развёртывание](#13-развёртывание)
14. [Web-интерфейс](#14-web-интерфейс)
15. [Защитные механизмы (Kill Switch)](#15-защитные-механизмы-kill-switch)
16. [FAQ и Траблшутинг](#16-faq-и-траблшутинг)

---

## 1. Обзор системы

Midas Trading Bot — автоматизированный торговый бот для Bybit. Подключается к Telegram под **пользовательским аккаунтом** (Telethon), получает сигналы от внешнего бота-источника, парсит их (regex + LLM), проверяет через Risk Engine и открывает сделки маркет-ордерами на Bybit.

### Ключевые возможности

| Функция | Описание |
|---------|----------|
| **Приём сигналов** | Telegram ЛС/каналы → classifier → regex/LLM parser |
| **LLM-валидация** | OpenRouter API (Gemini, Qwen) — проверка качества парсинга и риска |
| **Risk Engine** | Titan V GPU: Monte Carlo VaR/CVaR, signal scoring, conviction sizing |
| **Думалка (Dumalka)** | Внешнее управление позициями: partial_close, move_sl, move_tp, full_close |
| **Мониторинг** | TP1→инфо, TP2→SL в безубыток, TP3/SL→Bybit нативно |
| **Уведомления** | Telethon → Telegram чат/группа, RE-отчёты в @uebot_report |
| **Web UI** | Дашборд, сигналы, позиции, логи, настройки, Telegram-авторизация |

---

## 2. Архитектура

```
┌──────────────────────────────────────────────────────────────────┐
│                        ВНЕШНИЙ МИР                                │
├──────────────────────────────────────────────────────────────────┤
│  Telegram              │  Bybit API           │  OpenRouter LLM  │
│  (сигнальный бот       │  (ордера, позиции,   │  (парсинг,       │
│   @uebot333bot)        │   баланс, тикеры)    │   валидация)     │
└────────┬───────────────┴──────────┬───────────┴────────┬─────────┘
         │                         │                     │
         ▼                         ▼                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                    MIDAS TRADING BOT (FastAPI)                     │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  telegram_listener.py    trade_manager.py    telegram_notifier.py │
│  ├─ Telethon Client      ├─ open_trade()     ├─ send()           │
│  ├─ handle_signal()      ├─ monitor_trade()  ├─ notify_*()       │
│  ├─ classify_message()   ├─ check_*()        └─ RE events        │
│  └─ _handle_trade/aux()  └─ dumalka cmds                         │
│                                                                  │
│  signal_parser.py        bybit_client.py     llm_parser.py       │
│  ├─ classify (regex)     ├─ open_position()  ├─ extract signal   │
│  ├─ parse_trade()        ├─ set_sl/tp()      └─ via OpenRouter   │
│  └─ parse_auxiliary()    ├─ close_partial()                      │
│                          └─ close_full()     llm_validator.py    │
│                                              ├─ validate_trade() │
│                                              └─ monitor_aux()    │
├──────────────────────────────────────────────────────────────────┤
│  main.py                                                         │
│  ├─ FastAPI routes: /, /signals, /settings, /logs, /positions    │
│  ├─ Dumalka API: /dumalka/command, /positions, /status, uploads  │
│  ├─ Bot API: /api/bot/start, /stop, /status                     │
│  ├─ Telegram Auth: /api/telegram/send_code, /verify_code         │
│  └─ WebSocket: /ws (live stats broadcast)                        │
└───────────────────────────────┬──────────────────────────────────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│  PostgreSQL      │  │  Risk Engine     │  │  @uebot_report       │
│  (users,settings │  │  (Titan V GPU)   │  │  (Telegram группа)   │
│   trades,signals │  │  Monte Carlo     │  │  для RE-отчётов      │
│   channels)      │  │  Signal Scoring  │  │  и trade events      │
└─────────────────┘  └──────────────────┘  └──────────────────────┘
```

---

## 3. Структура проекта

```
midas-trading-bot/
├── app/
│   ├── main.py                 # FastAPI приложение, все маршруты
│   ├── core/
│   │   ├── config.py           # Pydantic Settings из .env
│   │   ├── database.py         # SQLAlchemy async engine + session
│   │   ├── logger.py           # Логирование (файл + консоль)
│   │   ├── bot_state.py        # Состояние бота (running, stats)
│   │   ├── security.py         # Шифрование API-ключей (Fernet)
│   │   ├── cache.py            # In-memory TTL cache для баланса
│   │   └── auth.py             # (резерв)
│   ├── services/
│   │   ├── telegram_listener.py # Telethon: приём сигналов
│   │   ├── signal_parser.py     # Regex-парсер сигналов
│   │   ├── llm_parser.py        # LLM-парсер (OpenRouter)
│   │   ├── llm_validator.py     # LLM-валидация + auxiliary monitor
│   │   ├── llm_base.py          # Общий HTTP-клиент для OpenRouter
│   │   ├── trade_manager.py     # Менеджер сделок + мониторинг
│   │   ├── bybit_client.py      # Обёртка Bybit API v5
│   │   ├── telegram_notifier.py # Уведомления через Telethon
│   │   └── dumalka_artifacts.py # Хранение context bundles от RE
│   ├── models/
│   │   ├── db_models.py         # SQLAlchemy модели (ORM)
│   │   └── trade_state.py       # Pydantic модель позиции в памяти
│   └── templates/               # Jinja2 HTML-шаблоны
├── static/                      # CSS, JS для Web UI
├── docs/                        # Документация
├── scripts/                     # Утилиты (миграция, дебаг)
├── tests/                       # Тесты
├── logs/                        # Файлы логов
├── dumalka_uploads/             # Context bundles от RE
├── .env                         # Конфигурация
├── docker-compose.yml           # Docker Compose (bot + PostgreSQL)
├── Dockerfile                   # Образ бота
├── requirements.txt             # Python-зависимости
└── create_session.py            # Создание Telethon-сессии
```

---

## 4. Модули и сервисы

### 4.1 `main.py` — Точка входа

FastAPI приложение с маршрутами:

| Группа | Маршруты | Назначение |
|--------|----------|------------|
| **Страницы** | `GET /`, `/signals`, `/positions`, `/logs`, `/settings` | Web UI (Jinja2) |
| **Настройки** | `POST /settings` | Обновление настроек через форму |
| **Каналы** | `POST /`, `/channels/delete/{id}` | Управление Telegram-каналами |
| **Bot API** | `POST /api/bot/start`, `/stop`, `GET /status` | Старт/стоп бота |
| **Telegram Auth** | `POST /api/telegram/send_code`, `/verify_code` | Авторизация в Web UI |
| **Думалка** | `/dumalka/command`, `/positions`, `/status`, `/context-upload` | API для Risk Engine |
| **WebSocket** | `WS /ws` | Live-обновления баланса и позиций |

**При старте (`startup_event`):**
1. Создаётся таблицы БД (если не существуют)
2. Создаётся/обновляется пользователь `admin` с ключами из `.env`
3. Инициализируются настройки по умолчанию
4. Загружаются каналы и конфиг фильтра сигнального бота
5. Восстанавливаются активные сделки из БД в память
6. Запускается Telethon-клиент и notifier

### 4.2 `telegram_listener.py` — Приём сигналов

**Ответственности:**
- Telethon-клиент (user session) — слушает все входящие сообщения
- Фильтрует: только от бота-источника (`SIGNAL_BOT_ID`/`USERNAME`) или из активных каналов
- Классифицирует сообщения: `trade`, `auxiliary`, `ignore`
- Парсит (LLM → regex fallback)
- Сохраняет в БД (таблица `signals`)
- Запускает обработку: `_handle_trade_signal()` или `_handle_auxiliary_signal()`

**Ключевые функции:**

| Функция | Описание |
|---------|----------|
| `handle_signal(event)` | Главный обработчик (Telethon handler) |
| `_handle_trade_signal()` | RE approval → LLM validation → Conviction Sizing → open trade |
| `_handle_auxiliary_signal()` | LLM анализ + action (close/tighten_sl/do_nothing) |
| `_execute_trade()` | Финальное открытие сделки через trade_manager |
| `re_timeout_checker()` | Background: авто-reject для pending сигналов > 5 мин |

### 4.3 `signal_parser.py` — Regex-парсер

Парсит текст сигналов формата:
```
ETHUSDT.P 1M - SELL
Вход: 1988.34-1987.25
Цели: 1982.37, 1980.05, 1975.78
Стоп: 1988.13, Трейлинг: 0.03%
```

**Извлекает:** symbol, side, direction, entry_price (среднее диапазона), tp1-tp3, sl, trailing_stop_pct, timeframe, metadata (risk_reward, probability, win_rate, volatility, volume).

**Auxiliary-сигналы:** денежный поток, импульс разворота, лента Midas, активность китов.

### 4.4 `llm_parser.py` — LLM-парсер

Вызывает OpenRouter API для извлечения параметров сигнала через промпт. Используется как **основной** парсер (если LLM включён), regex — как **fallback**.

### 4.5 `llm_validator.py` — LLM-валидатор

Два режима:

| Функция | Вход | Выход |
|---------|------|-------|
| `validate_trade_signal()` | Сырой текст + parsed JSON + текущая цена | `approve`/`reject` + corrections |
| `monitor_auxiliary_signal()` | Текст + открытые позиции | `close`/`tighten_sl`/`do_nothing` |

### 4.6 `trade_manager.py` — Менеджер сделок

Центральный модуль. Управляет всем жизненным циклом:

| Метод | Назначение |
|-------|------------|
| `open_trade()` | Проверки → расчёт size → MARKET order → SL+TP3 → запуск monitor |
| `monitor_trade()` | Цикл 3с: проверка pos_size, TP1→инфо, TP2→SL breakeven, close detection |
| `handle_auxiliary_action()` | Закрытие или подтяжка SL по вспомогательным сигналам |
| `execute_dumalka_command()` | Выполнение команд Думалки (partial_close, move_sl, move_tp, full_close) |
| `get_positions_for_re()` | Формирование списка позиций для Думалки |
| `notify_risk_engine()` | HTTP POST trade events → RE /trade-outcome |
| `check_max_positions()` | Kill switch: лимит позиций |
| `check_daily_loss()` | Kill switch: дневной убыток |

**REDU-PATCH v0.10.1 (Conviction Sizing):**
- При `RE_CONVICTION_SIZING=true`: бот запрашивает RE **перед** открытием сделки
- RE возвращает `conviction_size_usd` на основе signal score:
  - `score ≥ 0.75` → 1.5x стандартного размера
  - `score ≥ 0.60` → 1.0x
  - `score ≥ 0.45` → 0.5x
  - `score < 0.45` → reject (сделка не открывается)
- Safety cap: максимум 2x от настроенного `deposit_percent`

### 4.7 `bybit_client.py` — Bybit API

Обёртка над pybit (Bybit API v5):

| Метод | Описание |
|-------|----------|
| `get_wallet_balance()` | USDT баланс (кеш 30с) |
| `get_current_price()` | Последняя цена символа |
| `is_symbol_available()` | Доступен ли символ для торговли |
| `get_symbol_info()` | Информация о инструменте (tick, qty step) |
| `get_max_leverage()` | Максимальное плечо |
| `open_position()` | MARKET ордер + set leverage |
| `set_stop_loss()` | Установка SL через set_trading_stop |
| `set_take_profit()` | Установка TP |
| `set_trading_stop_combined()` | SL + TP одним вызовом |
| `set_trailing_stop()` | Trailing stop |
| `close_position_partial()` | Частичное закрытие (reduceOnly) |
| `close_position_full()` | Полное закрытие (проверка размера → reduceOnly) |

**Особенности:** retry с exponential backoff (rate limits), автопереключение на max leverage, кеширование баланса.

### 4.8 `telegram_notifier.py` — Уведомления

Отправляет сообщения через **тот же** Telethon user-аккаунт:

| Событие | Метод |
|---------|-------|
| Сделка открыта | `notify_trade_opened()` |
| Сделка закрыта | `notify_trade_closed()` |
| TP достигнут | `notify_tp_reached()` |
| SL сработал | `notify_sl_hit()` |
| LLM отклонила | `notify_llm_rejected()` |
| Auxiliary action | `notify_auxiliary_action()` |
| Kill switch | `notify_kill_switch()` |
| RE запрос одобрения | `notify_re_request()` |
| Trade event для RE | `notify_trade_event()` |

**Поддержка адреса чата:** numeric ID (`-100...`) или username (`uebot_report`, `@uebot_report`).

### 4.9 `bot_state.py` — Состояние бота

Хранит runtime-состояние:
- `is_running` — бот запущен/остановлен
- `started_at` — время старта
- `loss_reset_at` — время сброса счётчика убытков (при каждом старте)
- `processed_signals_count` — количество обработанных сигналов

**Важно:** При рестарте бота (`start()`) сбрасывается только `loss_reset_at`, что позволяет заново работать даже если дневной убыток был достигнут.

### 4.10 `dumalka_artifacts.py` — Context Bundles

Хранение бандлов от Risk Engine:
- Бинарный артефакт (до 64 MB)
- Документация (Markdown, до 8 MB)
- Changelog (до 8 MB)
- Manifest JSON с метаданными

Сохраняются в `dumalka_uploads/<bundle_id>/` с сортируемыми именами.

---

## 5. База данных

PostgreSQL с async SQLAlchemy (asyncpg). 5 таблиц:

### 5.1 `users`

| Поле | Тип | Описание |
|------|-----|----------|
| id | Integer PK | — |
| username | String UNIQUE | Логин (admin) |
| password_hash | String | Хеш пароля |
| bybit_api_key | String | Зашифрованный API key |
| bybit_api_secret | String | Зашифрованный API secret |
| is_testnet | Boolean | Тестнет Bybit |

### 5.2 `settings`

| Поле | По умолчанию | Описание |
|------|-------------|----------|
| deposit_percent | 10.0 | % депозита на сделку |
| leverage | 20 | Плечо |
| max_price_deviation | 1.0 | Макс. отклонение цены |
| default_stop_loss_percent | 10.0 | SL по умолчанию (если не в сигнале) |
| max_open_positions | 10 | Лимит открытых позиций |
| max_daily_loss_percent | 10.0 | Макс. дневной убыток % |
| signal_bot_id | — | ID бота-источника |
| signal_bot_username | — | Username бота |
| openrouter_api_key | — | LLM ключ (зашифрован) |
| openrouter_model | qwen/qwen3-coder:free | LLM модель |
| llm_validation_enabled | true | Включить LLM-валидацию |
| telegram_notify_chat_id | — | Куда слать уведомления |

### 5.3 `signals`

| Поле | Описание |
|------|----------|
| raw_text | Исходный текст сообщения |
| parsed_data | JSON с распарсенными полями |
| signal_hash | MD5 хеш для дедупликации |
| signal_type | `trade` / `auxiliary` |
| is_processed | Была ли открыта сделка |
| llm_decision | `approve` / `reject` |
| llm_reason | Причина LLM |
| re_status | `pending` / `approved` / `rejected` / `timeout` |
| re_score | Оценка от RE (0-1.0) |

### 5.4 `trades`

| Поле | Описание |
|------|----------|
| id | UUID сделки |
| symbol, side, entry_price, size, leverage | Параметры позиции |
| tp1, tp2, tp3, sl | Цели и стоп |
| trailing_stop_pct | Трейлинг стоп (%) |
| stage | 0=OPEN, 1=TP1_REACHED, 2=TP2_REACHED, 3=CLOSED |
| status | `active` / `closed` |
| profit_loss | P/L при закрытии |
| signal_id | FK → signals |
| position_id | ID ордера на Bybit |

### 5.5 `channels`

| Поле | Описание |
|------|----------|
| channel_id | ID канала Telegram |
| title | Название |
| signal_keywords | Ключевые слова для фильтрации (через запятую) |

---

## 6. Поток обработки сигнала

```
Telegram Message
      │
      ▼
  Фильтр: от signal bot? активный канал?
      │ Нет → игнор
      ▼ Да
  classify_message() → trade / auxiliary / ignore
      │ ignore → стоп
      ▼
  Дедупликация (signal_hash в БД)
      │ дубль → стоп
      ▼
  Парсинг:
  ├── LLM (если LLM_ENABLED + llm_validation_enabled + api_key)
  └── Regex fallback (если LLM отключён или не смог)
      │
      ▼
  Сохранение в signals (raw_text, parsed_data, type, hash)
      │
      ├── msg_type == "trade"
      │       │
      │       ▼
      │   RE_ENABLED? → Отправка запроса на одобрение в @uebot_report
      │       │         (Shadow Mode: сделка открывается сразу, RE для аналитики)
      │       ▼
      │   LLM validation → approve/reject
      │       │ reject → уведомление, стоп
      │       ▼ approve
      │   RE_CONVICTION_SIZING? → HTTP запрос к RE → conviction_size_usd
      │       │ reject от RE → стоп
      │       ▼
      │   trade_manager.open_trade()
      │       ├── check_max_positions()
      │       ├── check_daily_loss()
      │       ├── get_balance(), get_current_price()
      │       ├── calculate size (deposit% × balance × leverage)
      │       ├── MARKET order → Bybit
      │       ├── set SL + TP3 → Bybit
      │       ├── save to trades table
      │       ├── notify (Telegram + RE)
      │       └── start monitor_trade() task
      │
      └── msg_type == "auxiliary"
              │
              ▼
          LLM monitor_auxiliary_signal()
          → action: close / tighten_sl / do_nothing
          → execute action on matching trades
```

---

## 7. Жизненный цикл сделки

### Стадии (TradeStage)

```
OPEN (0) ──TP1 hit──▶ TP1_REACHED (1) ──TP2 hit──▶ TP2_REACHED (2) ──▶ CLOSED (3)
  │                                                      │
  └───────── SL/manual close ──────────────────────────▶ CLOSED (3)
```

### Мониторинг (`monitor_trade`)

Цикл каждые **3 секунды**:

1. **Проверка pos_size на Bybit** → если `0` → позиция закрыта биржей:
   - Определяется причина: TP3 (цена ≈ TP3) или SL
   - Сохраняется P/L, статус `closed`
   - Уведомление + RE callback

2. **Детектирование TP1** (информационное):
   - LONG: `current_price ≥ tp1`
   - SHORT: `current_price ≤ tp1`
   - Stage → `TP1_REACHED`
   - Уведомление + RE event `tp1_hit`

3. **Детектирование TP2** (SL → breakeven):
   - LONG: `current_price ≥ tp2`
   - SHORT: `current_price ≤ tp2`
   - SL переносится на `entry_price` через `set_stop_loss()`
   - Stage → `TP2_REACHED`
   - Уведомление + RE event `tp2_hit`

4. **TP3 закрытие** — выполняется **нативно Bybit** (TP3 установлен как takeProfit на бирже).

### Важная логика

- **Только MARKET ордера** — бот не использует лимитные ордера
- **SL и TP3 устанавливаются на Bybit нативно** при открытии сделки
- **TP1 — только информационный** — SL не двигается
- **TP2 → SL в безубыток** — единственная динамическая операция монитора
- **Думалка активный режим:** если Dumalka управляет сделкой, монитор не двигает SL/TP (но продолжает отслеживать закрытие позиции)

---

## 8. Risk Engine (RE)

### 8.1 Компоненты

Risk Engine — отдельный сервис на Proxmox VM (Titan V GPU, 12 GB HBM2):

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Monte Carlo VaR/CVaR | CuPy (FP64, 100K сценариев) | Оценка риска позиции |
| Signal Scoring | Python | Оценка качества сигнала (0-1.0) |
| FastAPI | uvicorn | HTTP API |
| Market Data | Binance.US / Bybit | Цены и волатильность |

### 8.2 Approval Flow (Shadow Mode)

```
Бот получает сигнал
    │
    ▼
Бот отправляет "Request for approval" в @uebot_report
    │
    ▼
Бот СРАЗУ открывает сделку (Shadow Mode — не ждёт ответа RE)
    │
    ▼
RE читает запрос, анализирует, отправляет callback (логируется в БД для аналитики)
```

### 8.3 Signal Scoring

Формула: `Score = 0.30×WR + 0.25×Prob + 0.20×RR + 0.15×Trend + 0.10×VolOk`

| Score | Рекомендация |
|-------|-------------|
| ≥ 0.60 | APPROVE |
| 0.45-0.60 | REDUCE |
| < 0.45 | REJECT |

**Force-reject:** Контртренд + Probability < 30%.

### 8.4 Conviction Sizing (REDU-PATCH)

При `RE_CONVICTION_SIZING=true`:
1. Бот отправляет `POST /tv-webhook` на Risk Engine перед открытием сделки
2. RE возвращает `conviction_size_usd` и `recommendation`
3. Если `recommendation == "reject"` → сделка не открывается
4. Если approve → `conviction_size_usd` используется для расчёта margin

### 8.5 Trade Outcome Callbacks

Бот отправляет `POST /trade-outcome` на RE при каждом событии:

| Event | Когда |
|-------|-------|
| `open` | Сделка открыта |
| `tp1_hit` | TP1 достигнут |
| `tp2_hit` | TP2 достигнут, SL → breakeven |
| `tp3_hit` | TP3 достигнут, позиция закрыта |
| `sl_hit` | SL сработал |
| `full_close` | Полное закрытие (auxiliary/dumalka) |
| `partial_close` | Частичное закрытие (dumalka) |

---

## 9. Думалка (Dumalka)

> **Думалка** — внешний модуль Risk Engine для интерактивного управления открытыми позициями. Коммуницирует через HTTP API.

### 9.1 Режимы работы

| Режим | `DUMALKA_MODE` | Поведение |
|-------|---------------|-----------|
| **Off** | `off` | Dumalka API отключён, бот полностью автономен |
| **Shadow** | `shadow` | Команды Думалки логируются, но НЕ исполняются |
| **Active** | `active` | Команды исполняются на бирже в реальном времени |

### 9.2 API Endpoints

> **Все запросы:** Header `X-Dumalka-Token: <DUMALKA_TOKEN>`

#### `POST /dumalka/command` — Управление позицией

```json
{
  "action": "partial_close",
  "symbol": "ETHUSDT",
  "fraction": 0.3,
  "new_sl": null,
  "new_tp": null,
  "reason": "Zone 2 reached, securing profit",
  "zone": 2,
  "diagnostics": {}
}
```

**Действия:**

| action | Параметры | Описание |
|--------|-----------|----------|
| `partial_close` | `fraction` (0.0-1.0) | Частичное закрытие позиции |
| `move_sl` | `new_sl` | Перенос стоп-лосса |
| `move_tp` | `new_tp` | Перенос тейк-профита |
| `full_close` | — | Полное закрытие позиции |

#### `GET /dumalka/positions` — Открытые позиции

```json
{
  "ok": true,
  "positions": [
    {
      "symbol": "ETHUSDT",
      "side": "long",
      "entry_price": 2450.5,
      "size": 0.123,
      "current_price": 2480.0,
      "unrealized_pnl_pct": 1.2,
      "stop_loss": 2400.0,
      "take_profit": 2600.0,
      "opened_at": "2026-03-27T10:30:00Z",
      "signal_hash": "abc123"
    }
  ]
}
```

#### `GET /dumalka/status` — Статус

```json
{
  "ok": true,
  "mode": "active",
  "active_trades": 3,
  "symbols": ["ETHUSDT", "SOLUSDT", "BTCUSDT"],
  "fallback_timeout_min": 10
}
```

#### `POST /dumalka/context-upload` — Загрузка артефактов

Multipart/form-data: бинарный артефакт + документация + changelog.

### 9.3 Fallback Timeout

Если в режиме `active` Думалка не присылала команд для сделки более `DUMALKA_FALLBACK_TIMEOUT_MIN` минут (по умолчанию 10), бот переключается на **свои** правила мониторинга (TP2 → SL breakeven).

---

## 10. LLM-интеграция

### 10.1 Два уровня LLM

| Уровень | Файл | Когда | Что делает |
|---------|------|-------|------------|
| **Парсинг** | `llm_parser.py` | При получении сигнала | Извлекает symbol, side, entry, tp, sl из текста |
| **Валидация** | `llm_validator.py` | Перед открытием сделки | Проверяет качество парсинга и рыночные условия |

### 10.2 Настройки

- `LLM_ENABLED` — глобальный тумблер (`false` = LLM не используется нигде)
- `llm_validation_enabled` — валидация сигналов через LLM (per-user в настройках)
- `OPENROUTER_API_KEY` — ключ для OpenRouter
- `OPENROUTER_MODEL` — модель (напр. `google/gemini-2.0-flash-001`)

### 10.3 Rate Limiting

Используется глобальный `asyncio.Lock` + 2с пауза между вызовами. Retry: до 5 попыток с exponential backoff (2-20с).

---

## 11. Уведомления

### 11.1 Каналы

| Канал | Кому | Что |
|-------|------|-----|
| `TELEGRAM_NOTIFY_CHAT_ID` | Пользователь | Все события: сделки, TP, SL, LLM reject |
| `RE_REPORT_CHAT_ID` | @uebot_report | RE запросы, trade events (для Risk Engine) |

### 11.2 Формат

Сообщения отправляются в HTML через Telethon. Примеры:

```
✅ Сделка открыта
Символ: ETHUSDT
Направление: LONG
Вход: 2450.5
Размер: 245.05 USDT
Плечо: 20x
```

```
🎯 TP2 достигнут
Символ: ETHUSDT
Закрыто: 0%
SL перенесён: 2450.5
```

---

## 12. Конфигурация (.env)

### Telegram
| Переменная | Описание |
|-----------|----------|
| `TELEGRAM_API_ID` | API ID от my.telegram.org |
| `TELEGRAM_API_HASH` | API Hash |
| `TELEGRAM_SESSION_NAME` | Имя файла сессии |
| `TELEGRAM_PHONE` | Номер телефона для create_session.py |
| `SIGNAL_BOT_ID` | ID бота-источника сигналов |
| `SIGNAL_BOT_USERNAME` | Username бота без @ |
| `TELEGRAM_NOTIFY_CHAT_ID` | Куда слать уведомления |

### Bybit
| Переменная | Описание |
|-----------|----------|
| `BYBIT_API_KEY` | API Key |
| `BYBIT_API_SECRET` | API Secret |
| `BYBIT_TESTNET` | `true`/`false` |

### Торговля
| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `DEPOSIT_PERCENT` | 10 | % от баланса на сделку |
| `DEFAULT_LEVERAGE` | 20 | Плечо |
| `MAX_OPEN_POSITIONS` | 10 | Макс. одновременных позиций |
| `MAX_DAILY_LOSS_PERCENT` | 10 | Макс. дневной убыток |
| `DEFAULT_STOP_LOSS_PERCENT` | 10 | SL по умолчанию |

### LLM
| Переменная | Описание |
|-----------|----------|
| `OPENROUTER_API_KEY` | API ключ OpenRouter |
| `OPENROUTER_MODEL` | Модель LLM |
| `LLM_ENABLED` | Глобальный тумблер LLM |
| `LLM_VALIDATION_ENABLED` | Валидация сигналов через LLM |

### Risk Engine
| Переменная | Описание |
|-----------|----------|
| `RISK_ENGINE_ENABLED` | Включить RE approval flow |
| `RE_REPORT_CHAT_ID` | Чат для RE отчётов |
| `RE_WEBHOOK_SECRET` | Секрет для RE API |
| `RE_APPROVAL_TIMEOUT` | Таймаут ожидания ответа RE (сек) |
| `RE_CONVICTION_SIZING` | Conviction sizing (true/false) |
| `RE_URL` | URL Risk Engine API |

### Думалка
| Переменная | Описание |
|-----------|----------|
| `DUMALKA_MODE` | `off`/`shadow`/`active` |
| `DUMALKA_TOKEN` | Токен авторизации |
| `DUMALKA_FALLBACK_TIMEOUT_MIN` | Таймаут fallback (минуты) |

---

## 13. Развёртывание

### Docker (рекомендуемый)

```bash
# Клонировать, создать .env
cd /opt/trading-bot/midas-trading-bot

# Создать Telegram-сессию (один раз)
python3 create_session.py

# Собрать и запустить
docker compose up -d --build

# Логи
docker compose logs -f bot

# Перезапуск
docker compose restart bot

# Остановка
docker compose down
```

**Контейнеры:**
- `bot` — приложение на порту 8000 (проброс → 8001)
- `db` — PostgreSQL 15 (volume `pgdata`)

### Без Docker

```bash
cd /opt/trading-bot/midas-trading-bot
source venv/bin/activate

# БД (PostgreSQL должен быть запущен)
python3 init_db.py

# Запуск
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Systemd

```ini
[Unit]
Description=Midas Trading Bot
After=postgresql.service

[Service]
Type=simple
WorkingDirectory=/opt/trading-bot/midas-trading-bot
ExecStart=/opt/trading-bot/midas-trading-bot/venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 14. Web-интерфейс

| Страница | URL | Описание |
|----------|-----|----------|
| **Дашборд** | `/` | Баланс, активные сделки, история, Telegram-каналы |
| **Сигналы** | `/signals` | Последние 50 сигналов с результатами LLM/RE |
| **Позиции** | `/positions` | Активные + закрытые позиции |
| **Логи** | `/logs` | Последние 10 КБ файла логов |
| **Настройки** | `/settings` | Все параметры + Telegram авторизация |

**WebSocket** (`/ws`): автообновление баланса и количества позиций в реальном времени.

---

## 15. Защитные механизмы (Kill Switch)

| Механизм | Проверка | При срабатывании |
|----------|---------|-----------------|
| **Max Positions** | `active_trades ≥ max_open_positions` | Новые сделки не открываются |
| **Daily Loss** | `∣daily_pnl∣ / balance × 100 ≥ max_daily_loss_percent` | Торговля приостановлена + Telegram уведомление |
| **Bot State** | `bot_state.is_running == False` | Все сигналы игнорируются |
| **Balance** | `balance ≤ 0` | Сделка не открывается |
| **Symbol Check** | `symbol not in Trading status` | Сделка не открывается |
| **Loss Reset** | При рестарте бота (`start()`) | `loss_reset_at = now()` — счётчик обнуляется |

---

## 16. FAQ и Траблшутинг

### Бот не реагирует на сигналы
1. Проверить `bot_state.is_running` (Web UI → кнопка Start)
2. Проверить `SIGNAL_BOT_ID` / `SIGNAL_BOT_USERNAME` в настройках
3. Проверить логи: `docker compose logs -f bot | grep "Telegram:"`
4. Убедиться, что Telegram сессия авторизована

### Сделка не открывается
1. Лимит позиций: `MAX_OPEN_POSITIONS`
2. Дневной убыток: `MAX_DAILY_LOSS_PERCENT`
3. Баланс нулевой
4. Символ недоступен на Bybit
5. LLM reject (если включён)
6. RE reject (если `RE_CONVICTION_SIZING=true`)

### Уведомления не приходят
1. Проверить `TELEGRAM_NOTIFY_CHAT_ID` — для публичных групп используйте username (без @)
2. Telethon клиент подключён?
3. Аккаунт состоит в этой группе?

### Баланс показывается 0
1. Проверить API ключи Bybit в настройках или `.env`
2. Проверить `ENCRYPTION_KEY` (должен быть одинаковый для шифрования/дешифрования)
3. `BYBIT_TESTNET` совпадает с типом аккаунта?

### Risk Engine не отвечает
1. RE работает в Shadow Mode — сделки открываются без ожидания ответа
2. Проверить `RE_URL` (Tailscale VPN)
3. Через 5 минут сигнал автоматически получает статус `timeout`
