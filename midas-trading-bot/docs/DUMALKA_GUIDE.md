# Думалка — Полный гайд по интеграции с Midas Trading Bot

> **Для кого:** Разработчик Risk Engine / Думалки  
> **Версия бота:** Phase 4.0 (Март 2026)  
> **Midas URL:** `https://midas-trade.mooo.com` (или `http://localhost:8001` локально)

---

## Содержание

1. [Что такое Думалка](#1-что-такое-думалка)
2. [Как Midas и Думалка работают вместе](#2-как-midas-и-думалка-работают-вместе)
3. [Режимы работы](#3-режимы-работы)
4. [Аутентификация](#4-аутентификация)
5. [API Reference](#5-api-reference)
6. [Сценарии работы (пошагово)](#6-сценарии-работы-пошагово)
7. [Зонная политика (Zone Policy)](#7-зонная-политика-zone-policy)
8. [Fallback-механизм](#8-fallback-механизм)
9. [Context Upload](#9-context-upload-загрузка-артефактов)
10. [Conviction Sizing (REDU-PATCH)](#10-conviction-sizing-redu-patch)
11. [Trade Outcome Callbacks](#11-trade-outcome-callbacks)
12. [Примеры кода](#12-примеры-кода)
13. [Ошибки и их решение](#13-ошибки-и-их-решение)
14. [Что Midas делает сам](#14-что-midas-делает-сам-не-трогать)
15. [Чеклист перед продакшеном](#15-чеклист-перед-продакшеном)

---

## 1. Что такое Думалка

**Думалка** — это внешняя система управления рисками (Risk Engine), которая работает **параллельно** с торговым ботом Midas. Midas занимается **исполнением** (приём сигналов, открытие/закрытие ордеров на Bybit), а Думалка — **интеллектуальным управлением** позициями.

```
┌─────────────────────────────────────────────────────────────────┐
│                     ДУМАЛКА (Risk Engine)                         │
│                                                                  │
│  Monte Carlo VaR/CVaR ← Titan V GPU (100k FP64 сценариев)       │
│  Signal Scoring       ← WinRate, RiskReward, Probability        │
│  Zone Policy          ← Управление позициями по зонам (0-4)     │
│  Conviction Sizing    ← Score-based sizing перед открытием       │
│                                                                  │
│  Входные данные:                                                 │
│  ├── GET /dumalka/positions (поллинг: что сейчас открыто)        │
│  ├── Trade Events (HTTP callbacks от Midas: open, tp1, tp2...)   │
│  └── Telegram @uebot_report (сигналы + метаданные Midas)         │
│                                                                  │
│  Выходные команды:                                               │
│  └── POST /dumalka/command (partial_close, move_sl, move_tp...)  │
└─────────────────────────────────────────────────────────────────┘
         ▲                                    │
         │ Позиции + Events                   │ Команды
         │                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                     MIDAS TRADING BOT                             │
│                                                                  │
│  Telegram → Parser → Trade Manager → Bybit API                   │
│                                                                  │
│  Ответственность Midas:                                          │
│  ├── Приём сигналов из Telegram                                  │
│  ├── Открытие MARKET ордеров на Bybit                            │
│  ├── Установка нативных SL/TP на бирже                           │
│  ├── Мониторинг позиций (TP1 info, TP2 → SL breakeven)           │
│  └── Исполнение команд от Думалки                                │
└─────────────────────────────────────────────────────────────────┘
```

### Распределение ответственности

| Задача | Midas | Думалка |
|--------|-------|---------|
| Приём сигналов из Telegram | ✅ | ❌ |
| Парсинг сигналов | ✅ | ❌ |
| Открытие ордера на Bybit | ✅ | ❌ |
| Установка SL/TP на бирже | ✅ (при открытии) | ✅ (корректировка) |
| Мониторинг цены | ✅ | По необходимости |
| Решение о частичном закрытии | ❌ | ✅ |
| Решение о переносе SL/TP | ❌ (только TP2→breakeven) | ✅ |
| Полное закрытие позиции | ✅ (по SL/TP биржи) | ✅ (по аналитике) |
| Скоринг сигнала | ❌ | ✅ |
| Monte Carlo VaR | ❌ | ✅ |

---

## 2. Как Midas и Думалка работают вместе

### Полный цикл жизни сделки (при активной Думалке)

```
1. Сигнал приходит в Telegram от @uebot333bot
      │
      ▼
2. Midas парсит сигнал (regex или LLM)
      │
      ▼
3. Midas отправляет "Request for approval" в @uebot_report
   (symbol, side, entry, tp1-tp3, sl, hash, raw_text, metadata)
      │
      ▼
4. [Опционально: RE_CONVICTION_SIZING=true]
   Midas запрашивает POST /tv-webhook у Думалки
   Думалка возвращает conviction_size_usd + recommendation
   → reject → сделка НЕ открывается
   → approve → используется conviction_size_usd для расчёта лота
      │
      ▼
5. Midas открывает MARKET ордер на Bybit
   + устанавливает SL + TP3 нативно на бирже
      │
      ▼
6. Midas отправляет trade event "open" → POST /trade-outcome на RE
   Midas отправляет trade event в @uebot_report (Telegram)
      │
      ▼
7. Midas запускает monitor_trade() — цикл каждые 3 секунды:
   - Проверяет pos_size на Bybit (закрылась ли позиция)
   - Отслеживает TP1 (информационное) → event "tp1_hit"
   - Отслеживает TP2 → SL в безубыток → event "tp2_hit"
   * В режиме DUMALKA_MODE=active: SL/TP монитор подавлен,
     но детекция закрытия (pos_size=0) продолжает работать
      │
      ▼
8. Думалка поллит GET /dumalka/positions (своя периодичность)
   Видит новую позицию, начинает анализ
      │
      ▼
9. Думалка принимает решение и шлёт POST /dumalka/command:
   - partial_close (например, zone 2 → закрыть 30%)
   - move_sl (подтянуть стоп)
   - move_tp (изменить тейк)
   - full_close (закрыть полностью)
      │
      ▼
10. Midas исполняет команду на Bybit и отправляет уведомление
      │
      ▼
11. Позиция закрывается (по SL/TP Bybit или full_close от Думалки)
    Midas отправляет trade event ("tp3_hit"/"sl_hit"/"full_close")
    → POST /trade-outcome на RE
    → Telegram @uebot_report
```

---

## 3. Режимы работы

Устанавливается через `.env` → `DUMALKA_MODE`:

### `off` (по умолчанию)
- API `/dumalka/command` возвращает HTTP 400
- Бот полностью автономен
- Монитор работает по стандартным правилам (TP2 → SL breakeven)

### `shadow`
- Команды принимаются и **логируются**, но **НЕ исполняются** на бирже
- Позволяет тестировать логику Думалки на реальных позициях
- Ответ: `{"ok": true, "mode": "shadow", "action": "...", "message": "logged only (shadow mode)"}`
- Монитор работает по стандартным правилам

### `active`
- Команды **исполняются** на бирже в реальном времени
- Монитор **подавляет** свою логику SL/TP (не двигает SL при TP2), если от Думалки приходили команды в последние N минут
- При отсутствии команд дольше `DUMALKA_FALLBACK_TIMEOUT_MIN` — **fallback** на стандартный монитор

---

## 4. Аутентификация

Все Dumalka API endpoints используют **один токен** в HTTP заголовке:

```http
X-Dumalka-Token: dumalka_secret_2026
```

| Ситуация | Поведение |
|----------|-----------|
| Токен совпадает | Запрос обработан |
| Токен не совпадает | HTTP 401 `{"ok": false, "error": "unauthorized"}` |
| `DUMALKA_TOKEN` пустой в .env | **Открытый доступ** (не рекомендуется в продакшене) |

---

## 5. API Reference

### 5.1 `GET /dumalka/positions` — Получить открытые позиции

**Описание:** Возвращает все текущие открытые позиции бота. Думалка должна поллить этот endpoint для отслеживания позиций.

**Запрос:**
```bash
curl -s https://midas-trade.mooo.com/dumalka/positions \
  -H "X-Dumalka-Token: dumalka_secret_2026" | jq .
```

**Ответ (200):**
```json
{
  "ok": true,
  "positions": [
    {
      "symbol": "ETHUSDT",
      "side": "long",
      "entry_price": 2450.5,
      "size": 0.08167,
      "current_price": 2480.0,
      "unrealized_pnl_pct": 1.20,
      "stop_loss": 2400.0,
      "take_profit": 2600.0,
      "opened_at": "2026-03-27T10:30:00Z",
      "signal_hash": "abc123def456"
    },
    {
      "symbol": "SOLUSDT",
      "side": "short",
      "entry_price": 142.50,
      "size": 7.01754,
      "current_price": 140.80,
      "unrealized_pnl_pct": 1.19,
      "stop_loss": 147.50,
      "take_profit": 130.00,
      "opened_at": "2026-03-27T09:15:00Z",
      "signal_hash": "xyz789qwe012"
    }
  ]
}
```

**Описание полей:**

| Поле | Тип | Описание |
|------|-----|----------|
| `symbol` | string | Торговая пара (ETHUSDT, SOLUSDT и т.д.) |
| `side` | string | `"long"` или `"short"` |
| `entry_price` | float | Цена входа (рыночная цена на момент исполнения) |
| `size` | float | Размер позиции в **контрактах** (= size_usdt / entry_price) |
| `current_price` | float | Текущая цена символа (real-time от Bybit) |
| `unrealized_pnl_pct` | float | Нереализованная прибыль/убыток в % |
| `stop_loss` | float | Текущий стоп-лосс |
| `take_profit` | float | Финальный тейк-профит (TP3 если есть, иначе TP2 или TP1) |
| `opened_at` | string | Время открытия (ISO 8601 UTC) |
| `signal_hash` | string | MD5 хеш сигнала (для связки с RE-аналитикой) |

---

### 5.2 `POST /dumalka/command` — Отправить команду

**Описание:** Главный endpoint. Думалка отправляет команду для управления конкретной позицией.

**Запрос:**
```bash
curl -s -X POST https://midas-trade.mooo.com/dumalka/command \
  -H "X-Dumalka-Token: dumalka_secret_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "partial_close",
    "symbol": "ETHUSDT",
    "fraction": 0.3,
    "reason": "Zone 2 profit secured",
    "zone": 2
  }' | jq .
```

**Тело запроса (JSON):**

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `action` | string | **Да** | `partial_close`, `move_sl`, `move_tp`, `full_close` |
| `symbol` | string | **Да** | Символ позиции (ETHUSDT) |
| `fraction` | float | Для partial_close | Доля закрытия (0.0 — 1.0). По умолчанию 0.3 |
| `new_sl` | float | Для move_sl | Новая цена стоп-лосса |
| `new_tp` | float | Для move_tp | Новая цена тейк-профита |
| `reason` | string | Нет | Причина (записывается в логи + notifications) |
| `zone` | int | Нет | Номер зоны (0-4), информационное |
| `diagnostics` | object | Нет | Допданные (MC forward data, drawdown и т.д.) |

#### Действие `partial_close` — Частичное закрытие

Закрывает часть позиции. Midas выставляет **reduceOnly MARKET ордер** на Bybit.

```json
{
  "action": "partial_close",
  "symbol": "ETHUSDT",
  "fraction": 0.5,
  "reason": "50% profit lock at Zone 3",
  "zone": 3
}
```

**Важно:**
- `fraction` — доля от **изначального** размера позиции (не от текущего остатка)
- Midas пересчитывает qty с учётом `qtyStep` символа
- После partial_close Midas продолжает мониторить оставшуюся позицию
- RE получает callback `partial_close` с `size_remaining`

#### Действие `move_sl` — Перенос стоп-лосса

```json
{
  "action": "move_sl",
  "symbol": "ETHUSDT",
  "new_sl": 2460.0,
  "reason": "Tighten SL to entry+10",
  "zone": 2
}
```

**Важно:**
- Midas вызывает `set_trading_stop()` на Bybit
- Новый SL сохраняется в in-memory state и в БД
- Для LONG: `new_sl` должен быть < `current_price`
- Для SHORT: `new_sl` должен быть > `current_price`
- Bybit выполнит закрытие нативно при достижении нового SL

#### Действие `move_tp` — Перенос тейк-профита

```json
{
  "action": "move_tp",
  "symbol": "SOLUSDT",
  "new_tp": 125.00,
  "reason": "Extend TP based on MC forward",
  "zone": 3
}
```

**Важно:**
- Midas вызывает `set_trading_stop(takeProfit=...)` на Bybit
- Bybit закроет позицию нативно при достижении нового TP

#### Действие `full_close` — Полное закрытие

```json
{
  "action": "full_close",
  "symbol": "ETHUSDT",
  "reason": "VaR limit exceeded, risk too high",
  "zone": 4
}
```

**Важно:**
- Midas проверяет текущий pos_size на Bybit и закрывает **всю** позицию
- Рассчитывает P/L, сохраняет в БД, status → `closed`
- Отправляет уведомление + RE callback `full_close`
- Удаляет сделку из `active_trades`

---

**Ответы:**

**Успех (200):**
```json
{
  "ok": true,
  "action": "partial_close",
  "symbol": "ETHUSDT",
  "order_id": "1234567890",
  "fraction": 0.3
}
```

**Ошибки:**

| HTTP | `error` | Причина |
|------|---------|---------|
| 401 | `unauthorized` | Неверный или отсутствующий токен |
| 400 | `dumalka_mode is off` | Думалка отключена |
| 404 | `no active trade for ETHUSDT` | Нет открытой позиции с таким символом |
| 200 | `{"ok": false, "error": "partial_close failed"}` | Bybit отклонил ордер |
| 200 | `{"ok": false, "error": "new_sl required"}` | Не указан обязательный параметр |

---

### 5.3 `GET /dumalka/status` — Статус

```bash
curl -s https://midas-trade.mooo.com/dumalka/status | jq .
```

```json
{
  "ok": true,
  "mode": "active",
  "active_trades": 3,
  "symbols": ["ETHUSDT", "SOLUSDT", "BTCUSDT"],
  "fallback_timeout_min": 10
}
```

**Не требует авторизации** (публичный endpoint).

---

### 5.4 `POST /dumalka/context-upload` — Загрузка артефактов

См. [раздел 9](#9-context-upload-загрузка-артефактов).

---

## 6. Сценарии работы (пошагово)

### Сценарий 1: Думалка управляет позицией по зонам

```python
import httpx
import asyncio

BASE = "https://midas-trade.mooo.com"
TOKEN = "dumalka_secret_2026"
HEADERS = {"X-Dumalka-Token": TOKEN, "Content-Type": "application/json"}

async def main():
    async with httpx.AsyncClient(timeout=10.0) as client:
        
        # 1. Получить открытые позиции
        r = await client.get(f"{BASE}/dumalka/positions", headers=HEADERS)
        positions = r.json()["positions"]
        
        for pos in positions:
            symbol = pos["symbol"]
            pnl = pos["unrealized_pnl_pct"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            
            # 2. Зонная логика
            if pnl > 5.0:
                # Zone 3: закрыть 50%, подтянуть SL
                await client.post(f"{BASE}/dumalka/command", headers=HEADERS, json={
                    "action": "partial_close",
                    "symbol": symbol,
                    "fraction": 0.5,
                    "zone": 3,
                    "reason": f"Zone 3: PnL={pnl:.1f}%, securing 50%"
                })
                await client.post(f"{BASE}/dumalka/command", headers=HEADERS, json={
                    "action": "move_sl",
                    "symbol": symbol,
                    "new_sl": entry * 1.01,  # SL = entry + 1%
                    "zone": 3,
                    "reason": "SL to BE+1%"
                })
                
            elif pnl > 2.0:
                # Zone 2: подтянуть SL к точке входа
                if sl < entry:  # SL ещё ниже входа
                    await client.post(f"{BASE}/dumalka/command", headers=HEADERS, json={
                        "action": "move_sl",
                        "symbol": symbol,
                        "new_sl": entry,
                        "zone": 2,
                        "reason": "SL to breakeven"
                    })
                    
            elif pnl < -3.0:
                # Zone 4: стоп по drawdown
                await client.post(f"{BASE}/dumalka/command", headers=HEADERS, json={
                    "action": "full_close",
                    "symbol": symbol,
                    "zone": 4,
                    "reason": f"Drawdown limit: PnL={pnl:.1f}%"
                })

asyncio.run(main())
```

### Сценарий 2: Поллинг позиций → аналитика → команда

```python
import asyncio
import httpx

async def dumalka_loop():
    """Основной цикл Думалки: поллинг → анализ → команда."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Получить позиции
                r = await client.get(f"{BASE}/dumalka/positions",
                                     headers={"X-Dumalka-Token": TOKEN})
                if r.status_code != 200:
                    await asyncio.sleep(30)
                    continue
                    
                data = r.json()
                if not data.get("ok"):
                    await asyncio.sleep(30)
                    continue
                
                positions = data["positions"]
                
                for pos in positions:
                    # Запустить анализ (Monte Carlo, scoring, ваша логика)
                    decision = await analyze_position(pos)
                    
                    if decision.action != "do_nothing":
                        r = await client.post(
                            f"{BASE}/dumalka/command",
                            headers={"X-Dumalka-Token": TOKEN,
                                     "Content-Type": "application/json"},
                            json=decision.to_dict(),
                        )
                        result = r.json()
                        log(f"Command {decision.action} for {pos['symbol']}: {result}")
                        
        except Exception as e:
            log(f"Dumalka loop error: {e}")
            
        await asyncio.sleep(15)  # Поллинг каждые 15 секунд
```

---

## 7. Зонная политика (Zone Policy)

Рекомендуемая стратегия для Думалки. Зоны определяются по `unrealized_pnl_pct`:

| Зона | P/L (%) | Действие | Описание |
|------|---------|----------|----------|
| **0** | < 0% | `do_nothing` | Позиция в минусе. SL на Bybit сработает по нативному ордеру |
| **1** | 0-2% | `do_nothing` или `move_sl` → breakeven | Небольшая прибыль, можно подтянуть SL |
| **2** | 2-5% | `move_sl` → entry+1% | Защитить прибыль, подтянуть стоп |
| **3** | 5-10% | `partial_close` 30-50% + `move_sl` | Зафиксировать часть прибыли |
| **4** | > 10% | `partial_close` 50-70% + `move_sl` → entry+5% | Агрессивное закрытие |
| **-1** | < -5% | Рассмотреть `full_close` | Drawdown превышает допустимый |

Думалка может использовать свою зонную политику — поле `zone` в команде **информационное** и записывается в логи бота для аналитики.

---

## 8. Fallback-механизм

Если Думалка (в режиме `active`) **не присылала команд** для сделки дольше `DUMALKA_FALLBACK_TIMEOUT_MIN` минут (по умолчанию 10):

1. Бот переключается на **свои стандартные правила** мониторинга
2. При достижении TP2 — SL переносится в безубыток (entry_price)
3. TP3 / SL закрываются нативно Bybit

**Как это работает в коде:**
```
monitor_trade() → каждый цикл проверяет _should_use_dumalka(trade_id):
  - DUMALKA_MODE != "active" → False (стандартный монитор)
  - Последняя команда от Думалки > N минут назад → False (fallback)
  - Команда была недавно → True (Думалка управляет, монитор не двигает SL/TP)
```

**Чтобы не попасть в fallback:**
- Думалка должна отправлять любую команду (хотя бы `move_sl` на текущее значение) чаще чем раз в 10 минут для каждой позиции, которой хочет управлять

---

## 9. Context Upload (загрузка артефактов)

Endpoint для загрузки документации, бинарных артефактов и changelog от Думалки на сервер Midas.

### `POST /dumalka/context-upload`

**Content-Type:** `multipart/form-data`

| Поле | Обязательно | Описание |
|------|-------------|----------|
| `artifact` | Нет | Бинарный файл (до 64 MB) |
| `documentation` | Нет* | Текст документации (Markdown) |
| `changelog` | Нет* | Текст changelog |
| `version` | Нет | Версия (напр. `1.4.2`) |
| `build_id` | Нет | CI SHA / build number |

*Нужен хотя бы один из: `artifact`, `documentation`, `changelog`.

```bash
# Загрузка документации + changelog
curl -sS -X POST "https://midas-trade.mooo.com/dumalka/context-upload" \
  -H "X-Dumalka-Token: dumalka_secret_2026" \
  -F "version=1.4.2" \
  -F "build_id=$(git rev-parse --short HEAD)" \
  -F "documentation=<./DUMALKA_INTEGRATION.md" \
  -F "changelog=Fixed zone-1 SL sync; see ticket RE-442"
```

### Получение последнего бандла

```bash
curl -s "https://midas-trade.mooo.com/dumalka/context-upload/latest" \
  -H "X-Dumalka-Token: dumalka_secret_2026" | jq .
```

### На диске Midas

```
dumalka_uploads/
  20260327T121530.123456Z_1_4_2_a1b2c3d4/
    manifest.json
    artifact.bin
    DOCUMENTATION.md
    CHANGELOG.md
```

---

## 10. Conviction Sizing (REDU-PATCH)

При `RE_CONVICTION_SIZING=true` в `.env` бота, **перед каждой сделкой** Midas отправляет HTTP запрос к Думалке для получения размера позиции на основе quality score.

### Запрос от Midas

```http
POST http://100.117.168.63:8000/tv-webhook
X-Webhook-Secret: 123QWEasd

{
  "symbol": "ETHUSDT",
  "side": "Buy",
  "size": 0.001,
  "source": "conviction_sizing",
  "signal_hash": "abc123",
  "stop_loss": 2400.0,
  "risk_reward": 7.8,
  "probability": 89.0,
  "win_rate": 55.0,
  "trend": "bullish"
}
```

### Ожидаемый ответ от Думалки

```json
{
  "recommendation": "approve",
  "signal_score": 0.78,
  "conviction_size_usd": 150.0,
  "var": 0.012,
  "cvar": 0.018
}
```

### Логика бота

| `recommendation` | `signal_score` | Действие |
|-------------------|---------------|----------|
| `reject` | < 0.45 | Сделка **НЕ** открывается |
| `reduce` | 0.45-0.60 | `conviction_size_usd` используется (0.5x) |
| `approve` | ≥ 0.60 | `conviction_size_usd` используется (1x-1.5x) |

**Safety cap:** `conviction_size_usd / leverage` ограничен `2 × deposit_percent × balance`.

**Fallback:** Если RE недоступен (таймаут, ошибка) → сделка открывается со стандартным размером.

---

## 11. Trade Outcome Callbacks

Midas автоматически отправляет HTTP POST на `{RE_URL}/trade-outcome` при каждом значимом событии.

### Payload

```json
{
  "hash": "abc123def456",
  "event": "tp2_hit",
  "symbol": "ETHUSDT",
  "side": "long",
  "price": 2550.0,
  "pnl_pct": 4.07,
  "size_remaining": 0.041
}
```

### Events

| Event | Когда | size_remaining |
|-------|-------|----------------|
| `open` | Сделка открыта | Полный размер |
| `tp1_hit` | TP1 достигнут | Полный размер |
| `tp2_hit` | TP2 достигнут, SL → breakeven | Полный размер |
| `tp3_hit` | TP3 достигнут, позиция закрыта | 0 |
| `sl_hit` | SL сработал, позиция закрыта | 0 |
| `partial_close` | Частичное закрытие (Думалка) | Остаток |
| `full_close` | Полное закрытие (Думалка/auxiliary) | 0 |

### Дополнительно

Те же события дублируются текстовым сообщением в Telegram `@uebot_report`:
```
📊 Trade Event
hash: abc123def456
symbol: ETHUSDT
side: long
event: tp2_hit
pnl_pct: 4.07
price: 2550.0
size_remaining: 0.041
```

---

## 12. Примеры кода

### Python: полный клиент для Думалки

```python
"""dumalka_client.py — Минимальный клиент для интеграции с Midas."""
import httpx
from dataclasses import dataclass

@dataclass
class DumalkaClient:
    base_url: str
    token: str
    timeout: float = 10.0
    
    @property
    def _headers(self):
        return {
            "X-Dumalka-Token": self.token,
            "Content-Type": "application/json",
        }
    
    async def get_positions(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/dumalka/positions",
                           headers=self._headers)
            r.raise_for_status()
            data = r.json()
            return data.get("positions", [])
    
    async def get_status(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/dumalka/status")
            return r.json()
    
    async def partial_close(self, symbol: str, fraction: float,
                            reason: str = "", zone: int = None) -> dict:
        return await self._command("partial_close", symbol,
                                   fraction=fraction, reason=reason, zone=zone)
    
    async def move_sl(self, symbol: str, new_sl: float,
                      reason: str = "", zone: int = None) -> dict:
        return await self._command("move_sl", symbol,
                                   new_sl=new_sl, reason=reason, zone=zone)
    
    async def move_tp(self, symbol: str, new_tp: float,
                      reason: str = "", zone: int = None) -> dict:
        return await self._command("move_tp", symbol,
                                   new_tp=new_tp, reason=reason, zone=zone)
    
    async def full_close(self, symbol: str,
                         reason: str = "", zone: int = None) -> dict:
        return await self._command("full_close", symbol,
                                   reason=reason, zone=zone)
    
    async def _command(self, action: str, symbol: str, **kwargs) -> dict:
        payload = {"action": action, "symbol": symbol}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/dumalka/command",
                            headers=self._headers, json=payload)
            return r.json()


# Использование:
# client = DumalkaClient("https://midas-trade.mooo.com", "dumalka_secret_2026")
# positions = await client.get_positions()
# await client.partial_close("ETHUSDT", 0.5, reason="Zone 3", zone=3)
```

### curl: быстрая проверка

```bash
# Статус
curl -s https://midas-trade.mooo.com/dumalka/status | jq .

# Позиции
curl -s https://midas-trade.mooo.com/dumalka/positions \
  -H "X-Dumalka-Token: dumalka_secret_2026" | jq .

# Подтянуть SL
curl -s -X POST https://midas-trade.mooo.com/dumalka/command \
  -H "X-Dumalka-Token: dumalka_secret_2026" \
  -H "Content-Type: application/json" \
  -d '{"action":"move_sl","symbol":"ETHUSDT","new_sl":2460,"reason":"test"}' | jq .

# Закрыть 30%
curl -s -X POST https://midas-trade.mooo.com/dumalka/command \
  -H "X-Dumalka-Token: dumalka_secret_2026" \
  -H "Content-Type: application/json" \
  -d '{"action":"partial_close","symbol":"ETHUSDT","fraction":0.3,"reason":"test"}' | jq .
```

---

## 13. Ошибки и их решение

| Проблема | Причина | Решение |
|----------|---------|---------|
| HTTP 401 на любой endpoint | Неверный `X-Dumalka-Token` | Проверить `DUMALKA_TOKEN` в `.env` бота |
| HTTP 400 `dumalka_mode is off` | `DUMALKA_MODE=off` в `.env` | Установить `shadow` или `active` |
| HTTP 404 `no active trade for SYMBOL` | Позиция уже закрыта или не существует | Сначала проверить `GET /positions` |
| `partial_close failed` | Bybit отклонил ордер (размер, liquidity) | Проверить fraction, символ, баланс |
| `move_sl failed` | SL невалиден для позиции (long SL > price) | Проверить направление и цену |
| `new_sl required` | Не указан `new_sl` для `move_sl` | Добавить параметр в запрос |
| Позиция закрылась сама | Bybit нативный SL/TP сработал | Это нормально — monitor ловит pos_size=0 |
| Fallback: Думалка не управляет | Не было команд > 10 мин | Слать keepalive (move_sl на текущий SL) |
| `positions` пустой | Нет открытых сделок | Дождаться следующего сигнала |

---

## 14. Что Midas делает сам (НЕ ТРОГАТЬ)

> **Эти процессы Midas выполняет самостоятельно. Думалка НЕ должна дублировать их.**

1. **Приём и парсинг сигналов** из Telegram
2. **Открытие MARKET ордеров** на Bybit
3. **Установка начальных SL + TP3** при открытии сделки
4. **Детекция закрытия позиции** (pos_size=0 → запись P/L в БД)
5. **Уведомления** в Telegram (все события автоматически)
6. **RE callbacks** (`/trade-outcome`) — автоматически при каждом событии
7. **Telegram events** в `@uebot_report` — автоматически
8. **Дедупликация сигналов** по signal_hash
9. **Kill switch** (max positions, daily loss)
10. **Восстановление** active trades при рестарте

**В режиме Думалки `active`:**
- Midas **НЕ двигает** SL при TP2, если Думалка активно управляет
- Midas **продолжает** отслеживать pos_size=0 (закрытие биржей всегда работает)
- Midas **продолжает** отправлять TP1/TP2 events (информационные)

---

## 15. Чеклист перед продакшеном

### Для разработчика Думалки

- [ ] Убедиться что `GET /dumalka/positions` возвращает позиции
- [ ] Убедиться что `GET /dumalka/status` показывает `mode: active`
- [ ] Отправить тестовую команду `move_sl` в режиме `shadow`
- [ ] Проверить логи бота: `docker compose logs bot | grep Dumalka`
- [ ] Переключить `DUMALKA_MODE` с `shadow` на `active`
- [ ] Отправить реальную команду и проверить исполнение на Bybit
- [ ] Настроить поллинг позиций (рекомендуемый интервал: 10-30 сек)
- [ ] Настроить keepalive (отправлять любую команду раз в <10 минут для каждой позиции)
- [ ] Настроить обработку `trade-outcome` callbacks от Midas
- [ ] Тестовое Conviction Sizing (`RE_CONVICTION_SIZING=true`)

### Переменные .env бота

```env
# Включить Думалку
DUMALKA_MODE=active           # off → shadow → active
DUMALKA_TOKEN=dumalka_secret_2026
DUMALKA_FALLBACK_TIMEOUT_MIN=10

# Risk Engine
RISK_ENGINE_ENABLED=true
RE_URL=http://100.117.168.63:8000
RE_WEBHOOK_SECRET=123QWEasd
RE_CONVICTION_SIZING=false     # true для score-based sizing
```

---

## Контакты

| Кто | Зона ответственности |
|-----|---------------------|
| **Дмитрий** | Midas Trading Bot (исполнение, API) |
| **Антон** | Risk Engine / Думалка (анализ, скоринг, zone policy) |
| **Telegram** | @uebot_report (совместный чат для RE-отчётов) |
