# Рекомендации разработчику боевого бота

> **Версия**: Phase 4.0 (Март 2026)
> **Цель**: Интегрировать Risk Engine (Titan V GPU, Monte Carlo VaR/CVaR) с midas-trading-bot.
> **Стратегия**: $14 → $1000 (высоко-агрессивная фаза роста → постепенный переход к консервативному управлению).

---

## Текущий статус интеграции

| Компонент | Статус |
|---|---|
| `telegram_bridge.py` (VM 106) | ✅ Парсит TG-сигналы → RE → @uebot_report |
| Risk Engine API | ✅ `http://100.87.26.107:8000/tv-webhook` |
| VPS Bybit Proxy | ✅ `http://100.117.168.63:8002` (Tailscale) |
| **midas-trading-bot → RE** | ❌ Бот НЕ вызывает RE перед торговлей |

---

## Шаг 1: Shadow Mode v2 (сейчас)

### Что добавить в `telegram_listener.py`:

В `_handle_trade_signal()`, **перед** `trade_manager.open_trade()`:

```python
# === RISK ENGINE INTEGRATION (Shadow Mode v2) ===
import httpx

RE_URL = "http://100.87.26.107:8000/tv-webhook"  # через Tailscale
RE_SECRET = "123QWEasd"

async def query_risk_engine(parsed_data: dict, current_equity: float) -> dict | None:
    """Запросить оценку у Risk Engine. Shadow Mode: логируем, но не блокируем."""
    payload = {
        "symbol": parsed_data["symbol"],
        "side": parsed_data["side"].lower(),
        "size": parsed_data.get("size", 1.0),
        "source": "midas_bot_live",
        "risk_reward": parsed_data.get("metadata", {}).get("risk_reward"),
        "probability": parsed_data.get("metadata", {}).get("probability"),
        "win_rate": parsed_data.get("metadata", {}).get("win_rate"),
        "trend": parsed_data.get("situation", ""),
        "stop_loss": parsed_data.get("sl"),
        "tp1": parsed_data.get("tp1"),
        "tp2": parsed_data.get("tp2"),
        "tp3": parsed_data.get("tp3"),
        "current_equity": current_equity,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                RE_URL,
                json=payload,
                headers={"X-Webhook-Secret": RE_SECRET}
            )
            if r.status_code == 200:
                result = r.json()
                logger.info(
                    f"[RE] {parsed_data['symbol']} {parsed_data['side']}: "
                    f"rec={result['recommendation']} score={result['signal_score']:.2f} "
                    f"VaR={result['var']*100:.2f}%"
                )
                return result
    except Exception as e:
        logger.error(f"[RE] Unavailable: {e}")
    return None  # RE недоступен → бот работает как обычно
```

### Вызов:

```python
# Shadow Mode v2: оцениваем, логируем, НЕ блокируем
balance = await bybit_client.get_wallet_balance(api_key, api_secret, testnet)
re_result = await query_risk_engine(parsed_data, balance)

# Сохранить в БД для ретроспективного анализа:
if re_result:
    await save_re_assessment(db_signal.id, re_result)

# Бот торгует как обычно
trade_opened = await trade_manager.open_trade(...)
```

> ℹ️ **Цель**: набрать 200+ трейдов с RE-оценкой И реальным P/L. Это позволит нам валидировать, какие рекомендации RE реально прибыльны.

---

## Шаг 2: Soft Filtering (после 200+ трейдов в Shadow Mode)

Когда у нас будет достаточно данных, подтверждающих, что RE-rejected действительно убыточны:

```python
if re_result:
    rec = re_result["recommendation"]
    var_pct = re_result["var"] * 100

    if rec == "reject":
        logger.info(f"[RE] BLOCKED: {parsed_data['symbol']} — score too low")
        # Уведомить но НЕ торговать
        await notify_re_blocked(parsed_data, re_result)
        return

    if rec == "reduce" or var_pct > 1.5:
        # Уменьшить позицию пропорционально VaR
        # При депо $14 — VaR-based sizing адаптивнее Kelly
        max_risk_usd = balance * 0.10  # 10% от депо (агрессивная фаза)
        var_per_unit = re_result["var"]
        if var_per_unit > 0:
            safe_size = max_risk_usd / var_per_unit
            parsed_data["size"] = min(parsed_data["size"], safe_size)
```

---

## Стратегия $14 → $1000: фазы risk management

При микро-депозите стандартные правила (1-2% на трейд) не работают.

| Фаза | Депозит | Риск на сделку | Стратегия |
|---|---|---|---|
| **Рост** | $14 – $100 | 10–15% | Агрессивный: берём почти все сигналы, RE только блокирует явно токсичные (ETHUSDT с VaR>1.7%) |
| **Стабилизация** | $100 – $500 | 5–8% | Умеренный: RE фильтрует `reject`, уменьшает `reduce` |
| **Консервативный** | $500+ | 2–3% | Стандартный: полная фильтрация, VaR-based sizing, exposure caps |

Эти фазы можно автоматизировать через `current_equity` в запросе к RE.

---

## Схема взаимодействия (Phase 4.0)

```
  TradingView Midas → Telegram @uebot333bot
                              ↓
                    Telethon Listener (VPS)
                              ↓
                    signal_parser.py → parsed signal
                              ↓
            ┌─────────────────┼──────────────────┐
            ↓                                     ↓
    query_risk_engine()              trade_manager.open_trade()
    (HTTP → Tailscale → VM 106)        (Bybit API)
            ↓                                     ↓
    Risk Engine (Titan V GPU)        monitor_trade() loop
    100k MC scenarios FP64             TP2 → breakeven
            ↓                          TP3/SL → Bybit
    Response: {                              ↓
      recommendation,               @estafetabot → @uebot_report
      signal_score,
      var, cvar,
      kelly_suggested_size_usd
    }
```

---

## Контакты и URL

| Ресурс | URL |
|---|---|
| Risk Engine API | `http://100.87.26.107:8000/tv-webhook` (Tailscale) |
| Risk Engine HTTPS | `https://rsk-eng.tail2465df.ts.net/tv-webhook` |
| Dashboard | `https://rsk-eng.tail2465df.ts.net/dashboard` |
| Health | `GET /health` → `{"gpu": "NVIDIA TITAN V", ...}` |
| Bybit Proxy | `http://100.117.168.63:8002` (Tailscale) |
| Webhook Secret | `123QWEasd` (header `X-Webhook-Secret`) |
| TG группа | `@uebot_report` (chat_id: -1003809470359) |

---

## Данные в ответе RE (EnrichedRiskResult)

| Поле | Тип | Описание |
|---|---|---|
| `recommendation` | str | `"approve"` / `"reduce"` / `"reject"` |
| `signal_score` | float | 0.0–1.0, Score (≥0.60 = approve, ≥0.45 = reduce, <0.45 = reject) |
| `var` | float | Value at Risk (99%), доля от equity |
| `cvar` | float | Conditional VaR (Expected Shortfall) |
| `approved` | bool | Финальное решение (scoring + MC) |
| `kelly_suggested_size_usd` | float | ⚠️ Пока **информационный**, не рекомендуется использовать напрямую (основан на Midas WR, который завышен) |
| `is_countertrend` | bool | Сигнал против тренда |
