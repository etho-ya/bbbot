# REDU v0.18.5 — Релиз-ноты и инструкция для Антона

> **Дата:** 2026-04-03  
> **PR:** https://github.com/etho-ya/REDU/pull/4  
> **Ветка:** `feat/v0.18.5-bot-integration` → `main`  
> **Базовая версия:** v0.18.4 (507dea2)  
> **Бот-версия:** v0.10.6 (уже задеплоена на сервере)

---

## Обзор

Этот релиз решает 4 практических проблемы, обнаруженных в продакшне, и добавляет одну новую фичу (SR keyword boost). Все изменения обратно совместимы — ни один существующий endpoint или payload не ломается.

---

## 1. Webhook Dedup — защита от дублей сигналов

### Проблема
Бот иногда отправляет два вебхука за 1-2 секунды для одной и той же монеты (например, VVV case). REDU обрабатывает оба, создавая два отдельных скоринга и потенциально два одобрения, что приводит к задвоению записей в `open_positions`.

### Решение
In-memory dedup словарь `_recent_webhooks` с окном 5 секунд. Второй вебхук для той же пары `(symbol, side)` получает `recommendation="reject"` с `rejection_reason="duplicate_webhook_dedup"`.

### Файл
`src/main.py` — строки ~2215-2260

### Как проверить
В логах при дубле будет:
```
Dedup reject: VVVUSDT long within 5.0s window
```

Ответ API:
```json
{
  "approved": false,
  "recommendation": "reject",
  "rejection_reason": "duplicate_webhook_dedup"
}
```

---

## 2. Conviction Mult: 0.5 → 0.7 для score [0.45, 0.60)

### Проблема
На боте введены фиксированные маржин-тиры для малых балансов:
- Balance < $200 → base margin = $10
- Balance $200-$300 → base margin = $20

При conviction_mult = 0.5 (score 0.45-0.59): `$10 × 0.5 = $5`, что ниже минимума Bybit ($5.5). Ордер отклоняется биржей.

### Решение
Поднят conviction_mult с 0.5 до 0.7: `$10 × 0.7 = $7` — проходит минимум биржи.

### Файл
`src/main.py` — строка ~2630

### Как проверить
При сигнале со score ~0.50 в логах:
```
conviction_mult=0.70 for signal_score=0.5000, base=$10 → conviction=$7
```

---

## 3. Обработка события `position_increased`

### Проблема
Когда два сигнала подряд приходят на одну монету (например, BTC long + BTC long через 20 минут), бот v0.10.5 теперь **не создает вторую позицию**, а увеличивает существующую на Bybit (merge). Бот отправляет в REDU событие `position_increased` через `/trade-outcome`, но REDU его не обрабатывал — `open_positions` оставалась с устаревшим entry_price и size.

### Решение
В `/trade-outcome` при получении `event="position_increased"`:
```sql
UPDATE open_positions 
SET entry_price = ?, size = size + COALESCE(?, 0), current_price = ?
WHERE signal_hash = ? AND status = 'open'
```

### Файл
`src/main.py` — строки ~2937-2950

### Как проверить
1. Отправить два сигнала на одну монету
2. В логах REDU:
   ```
   position_increased: updated open_positions for BTCUSDT (hash=abc123)
   ```
3. В БД: `SELECT entry_price, size FROM open_positions WHERE signal_hash = 'abc123'` — entry_price = средневзвешенная, size увеличился

---

## 4. Phantom Counter — Retry перед auto-close

### Проблема
Phantom sync (3× "no active trade" от бота → автозакрытие) иногда срабатывал ложно, когда бот перезапускался. Бот ещё не успевал восстановить trades из БД, а REDU уже считал позицию фантомной и закрывал прибыльную позицию.

### Решение
Перед phantom auto-close (counter >= 3) — подождать 5 секунд и повторно запросить `/dumalka/positions`. Если символ появился — сбросить счётчик.

### Файл
`src/position_tracker.py` — строки ~1171-1183

### Как проверить
При перезагрузке бота в логах REDU:
```
🔮 [PHANTOM RETRY] Pos #42 VVVUSDT: reappeared in bot after retry, counter reset
```

Без перезагрузки (настоящий фантом) — поведение как раньше:
```
🔮 [PHANTOM AUTO-CLOSE] Pos #42 VVVUSDT long: 3× 'no active trade' from bot → closing as phantom_sync
```

---

## 5. Auto SL→BE поля в EnrichedRiskResult

### Проблема
Бот не знал, когда и куда двигать SL в breakeven. Раньше это было зашито как константа, без привязки к конкретной сделке.

### Решение
REDU теперь вычисляет и отдаёт в ответе `/tv-webhook`:
- `auto_be_trigger` = TP1 (цена, при достижении которой бот сдвигает SL)
- `auto_be_price` = entry ± 0.2% (цена SL после breakeven, гарантирует маленький плюс)

Формула:
```
Long:  auto_be_price = entry + entry * 0.002
Short: auto_be_price = entry - entry * 0.002
Trigger = TP1
```

### Файлы
- `src/models.py` — два новых поля в `EnrichedRiskResult`
- `src/main.py` — вычисление после conviction calc (~строка 2637)

### Как проверить
Ответ `/tv-webhook` для approved сигнала:
```json
{
  "approved": true,
  "auto_be_trigger": 0.0543,
  "auto_be_price": 0.05012,
  ...
}
```

Для rejected сигнала — оба поля `null`.

---

## 6. Support/Resistance Keyword Boost (НОВАЯ ФИЧА)

### Суть
Бот теперь передает в поле `midas_comment` текст анализа сигнала (situation + recommendation из парсера). REDU анализирует этот текст на наличие ключевых слов, связанных с уровнями поддержки/сопротивления, и даёт дополнительный score boost.

### Зачем
Сигналы, упоминающие ключевые уровни (поддержка, сопротивление, "зона спроса/предложения", "Strong BUY"), исторически имеют более высокий win rate. Boost помогает одобрять такие сигналы, которые иначе могли бы попасть в "reduce" зону.

### Ключевые слова (13 штук)
```
поддержк, сопротивлен, ключев, уровен,
support, resistance, key level,
strong buy, strong sell,
зона спроса, зона предложения,
пробой, отскок от уровня, тест уровня
```

### Логика
- За каждое совпадение: **+0.03** к score
- Максимум **3 совпадения** (cap +0.09)
- Если "strong" в первых 50 символах → минимум **+0.05**
- Если после boost score ≥ 0.60 и рекомендация была "reduce" → апгрейд до "approve"

### Файлы
- `src/main.py` — строки ~2483-2517 (после repeat_boost)
- На стороне бота: `app/services/telegram_listener.py` — формирует `midas_comment` из parsed `situation` + `recommendation`

### Как проверить
В логах при сигнале с ключевыми словами:
```
🎯 [SR_BOOST] RIVERUSDT: +0.06 for keywords: ['поддержк', 'уровен']
```

Или для Strong сигнала:
```
💪 [STRONG_SIGNAL] BTCUSDT: boost=0.05
```

---

## Полный список изменённых файлов REDU

| Файл | Изменения |
|---|---|
| `src/main.py` | Dedup guard, SR keyword boost, conviction 0.7, auto-BE calc, position_increased handler, version bump |
| `src/models.py` | +2 поля: `auto_be_price`, `auto_be_trigger` в `EnrichedRiskResult` |
| `src/position_tracker.py` | Phantom retry (5s re-check), version bump |
| `src/tests/test_v018_bot_integration.py` | 20 новых тестов |
| `CHANGELOG.md` | Секция v0.18.5 |

---

## Что изменилось на стороне бота (уже задеплоено)

| Версия | Изменение | Файл |
|---|---|---|
| v0.10.3 | Conviction sizing fix, report hashes | `trade_manager.py` |
| v0.10.4 | Instant RE callback, auto SL→BE at TP1 | `main.py`, `trade_state.py`, `telegram_listener.py` |
| v0.10.5 | Fixed margin tiers ($10/$20), position merge | `trade_manager.py` |
| v0.10.6 | Передает `midas_comment` в RE payload | `telegram_listener.py` |

### Ключевое — position merge (v0.10.5)
Бот теперь при получении второго сигнала на ту же монету (то же направление):
1. Не создает новый `TradeState`, а увеличивает существующую позицию на Bybit
2. Обновляет avg entry_price, total size, SL/TP из нового сигнала
3. Отправляет `event="position_increased"` в REDU `/trade-outcome`
4. Один hash = один TradeState = одна позиция на Bybit (решает проблему VVV desync)

### Ключевое — midas_comment (v0.10.6)
Бот теперь включает в RE webhook payload:
```json
{
  "midas_comment": "Ситуация:\nЦена тестирует ключевой уровень поддержки...\nРекомендация:\nStrong BUY с хорошим RR...",
  ...
}
```
REDU использует это для SR keyword boost (пункт 6 выше).

---

## Тесты

### Как запустить
```bash
cd /opt/trading-bot/midas-trading-bot/dumalka/REDU/src
python -m pytest tests/test_v018_bot_integration.py -v
```

### Результат (20 тестов)
```
test_webhook_dedup_rejects_duplicate ........... PASSED
test_webhook_dedup_allows_after_window ......... PASSED
test_webhook_dedup_different_side_allowed ....... PASSED
test_conviction_mult_low_score_is_07 ........... PASSED
test_conviction_margin_above_bybit_min ......... PASSED
test_enriched_result_has_auto_be_fields ........ PASSED
test_enriched_result_auto_be_defaults_none ..... PASSED
test_auto_be_calculation_long .................. PASSED
test_auto_be_calculation_short ................. PASSED
test_auto_be_serializes_in_json ................ PASSED
test_trade_outcome_payload_accepts_position_increased PASSED
test_position_increased_sql_template ........... PASSED
test_phantom_threshold_is_3 .................... PASSED
test_phantom_counter_triggers_correctly ........ PASSED
test_phantom_retry_resets_counter .............. PASSED
test_phantom_retry_proceeds_if_absent .......... PASSED
test_sr_keyword_boost_detected ................. PASSED
test_sr_keyword_boost_capped ................... PASSED
test_strong_signal_boost ....................... PASSED
test_no_boost_without_keywords ................. PASSED
```

### Полный набор тестов REDU
```bash
python -m pytest tests/ -v
# 92 passed, 2 failed (pre-existing, не наши):
#   test_kelly_db — нужен pytest-asyncio
#   test_young_position_reduced_to_half_hour — YOUNG_POSITION_HOURS=1.0, тест ожидает 0.5
```

---

## Инструкция по деплою

### 1. Ревью и мерж PR
```
https://github.com/etho-ya/REDU/pull/4
```

### 2. На сервере REDU
```bash
cd /path/to/REDU
git checkout main
git pull origin main
```

### 3. Перезапуск сервиса
```bash
systemctl restart redu  # или как настроен у вас
```

### 4. Проверка здоровья
```bash
curl -s http://localhost:PORT/health | python3 -m json.tool
# Ожидаемый ответ: "version": "0.18.5"
```

### 5. Мониторинг в первый час
Следить в логах за:
- `[SR_BOOST]` и `[STRONG_SIGNAL]` — keyword boost работает
- `Dedup reject` — дедупликация ловит дубли
- `position_increased: updated` — merge позиций синхронизируется
- `[PHANTOM RETRY]` — retry перед phantom close (при рестартах бота)
- `auto_be_price` / `auto_be_trigger` в ответах `/tv-webhook`

---

## Что НЕ менялось

- Scoring формулы (веса, MC, kelly) — без изменений
- Zone policy, trailing, time-decay — без изменений  
- Dashboard endpoints — без изменений
- DB schema — без изменений (только UPDATE существующих записей)
- WebhookPayload — без изменений (midas_comment уже было Optional полем)

---

## Вопросы для Антона

1. **YOUNG_POSITION_HOURS**: В тестах v0.17 значение ожидается `0.5`, а в коде `1.0`. Это сознательное решение? Если да — можно обновить тест. Если нет — вернуть на `0.5`.

2. **pytest-asyncio**: `test_kelly_db.py` падает из-за отсутствия плагина. Стоит ли добавить `pytest-asyncio` в зависимости?

3. **SR Keywords**: Список из 13 ключевых слов покрывает основные случаи. Если есть дополнительные паттерны из реальных сигналов, которые стоит добавить — дай знать, обновим.

4. **Auto-BE offset 0.2%**: Сейчас `be_price = entry ± 0.2%`. Если по данным лучше другой offset — можно параметризировать через config.
