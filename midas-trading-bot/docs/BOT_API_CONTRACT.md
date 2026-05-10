# Bot ↔ Risk Engine: API Contract

> **Version**: 2.0 (API-first migration)
> **Date**: 2026-04-11
> **RE version**: 0.19.8+
> **Status**: Phase 1 — RE ready, bot migration pending
> **Previous doc**: `src/DEVELOPER_RECOMMENDATIONS.md` (v0.16.1, superseded)

---

## Overview

All machine-to-machine communication between the Trading Bot and Risk Engine
must use **HTTP JSON API only**. Telegram is used exclusively for
human-facing observability (formatted reports, alerts, dashboards).

```
Bot ──POST──► RE /tv-webhook          (signal approval)
Bot ──POST──► RE /trade-outcome       (trade lifecycle events)
RE  ──POST──► Bot /dumalka/command    (position management)
RE  ──GET───► Bot /dumalka/positions  (roster sync)
```

---

## 1. Authentication

All endpoints require the `X-Webhook-Secret` header (or `X-Dumalka-Token`
for bot-side endpoints).

| Direction | Header | Value |
|-----------|--------|-------|
| Bot → RE | `X-Webhook-Secret` | `$WEBHOOK_SECRET` env var on RE |
| RE → Bot | `X-Dumalka-Token` | `$DUMALKA_TOKEN` env var on Bot |

Requests with missing or incorrect secret receive **HTTP 403**.

---

## 2. Signal Approval — `POST /tv-webhook`

The bot sends a signal for risk assessment. RE runs Monte Carlo simulation,
scoring, and state guards, then returns the decision **synchronously** in the
HTTP response. No callback needed.

### Request

```
POST http://<RE_HOST>:8000/tv-webhook
Content-Type: application/json
X-Webhook-Secret: <WEBHOOK_SECRET>
```

```json
{
  "symbol": "WIFUSDT",
  "side": "long",
  "size": 1.0,
  "source": "bot_direct",
  "signal_hash": "a1b2c3d4e5f6",
  "entry_low": 0.199,
  "entry_high": 0.201,
  "stop_loss": 0.189,
  "tp1": 0.210,
  "tp2": 0.225,
  "tp3": 0.250,
  "risk_reward": 2.6,
  "probability": 67.0,
  "win_rate": 63.0,
  "trend": "moderate_bull",
  "volume_level": "high",
  "midas_comment": "Full Midas analysis text..."
}
```

### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | **yes** | Bybit perpetual pair, e.g. `ETHUSDT` |
| `side` | string | **yes** | `"long"` or `"short"` |
| `size` | float | no | Nominal size (default 1.0, RE uses for risk calc) |
| `source` | string | **yes** | Must be `"bot_direct"` for direct API calls |
| `signal_hash` | string | **yes** | Unique hash linking signal → position → outcomes |
| `entry_low` | float | recommended | Lower bound of entry zone |
| `entry_high` | float | recommended | Upper bound of entry zone |
| `stop_loss` | float | recommended | Stop-loss price level |
| `tp1` | float | recommended | Take-profit level 1 |
| `tp2` | float | no | Take-profit level 2 |
| `tp3` | float | no | Take-profit level 3 |
| `risk_reward` | float | recommended | Risk/reward ratio (e.g. 2.6 = 1:2.6) |
| `probability` | float | recommended | Midas signal probability, 0-100 |
| `win_rate` | float | recommended | Midas win rate, 0-100 |
| `trend` | string | no | `"strong_bull"`, `"moderate_bull"`, `"neutral"`, `"moderate_bear"`, `"strong_bear"` |
| `volume_level` | string | no | `"high"`, `"medium"`, `"low"` |
| `midas_comment` | string | no | Full Midas analysis text |

**Important**: `probability`, `win_rate`, and `risk_reward` significantly
affect the signal score. If omitted, RE penalizes the score (missing data
penalty). Always send them when available.

### Response (200 OK)

```json
{
  "approved": true,
  "recommendation": "approve",
  "rejection_reason": null,
  "signal_hash": "a1b2c3d4e5f6",
  "signal_score": 0.72,
  "conviction_size_usd": 85.0,
  "var": 0.023,
  "cvar": 0.031,
  "liquidation_prob": 0.002,
  "drawdown_estimate": -0.045,
  "latency_ms": 142.5,
  "is_countertrend": false,
  "exposure_warning": false,
  "auto_be_price": 0.2014,
  "auto_be_trigger": 0.210,
  "score_components": {
    "wr": 0.85,
    "prob": 0.78,
    "rr": 0.91,
    "trend_align": 0.70,
    "vol_ok": 1.0,
    "liquidity_ok": 1.0
  },
  "kelly_suggested_size_usd": 120.0,
  "midas_probability": 67.0,
  "midas_win_rate": 63.0,
  "midas_risk_reward": 2.6,
  "computed_volatility": 1.24,
  "timestamp": "2026-04-11T14:30:00+00:00"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `approved` | bool | `true` if recommendation is `approve` or `reduce` |
| `recommendation` | string | `"approve"` / `"reduce"` / `"reject"` |
| `rejection_reason` | string? | Why rejected: `"already_in_position"`, `"reentry_cooldown"`, `"whipsaw_protection"`, `"duplicate_webhook_dedup"`, or `null` |
| `signal_score` | float | 0.0–1.0, composite quality score |
| `conviction_size_usd` | float? | Recommended position size in USDT (use this for sizing) |
| `var` | float | Value-at-Risk (99%), fraction of equity |
| `cvar` | float | Conditional VaR, fraction of equity |
| `liquidation_prob` | float | Liquidation probability from Monte Carlo |
| `drawdown_estimate` | float | Worst-case drawdown estimate |
| `latency_ms` | float | Monte Carlo computation time |
| `is_countertrend` | bool | Signal is against the prevailing trend |
| `exposure_warning` | bool | Portfolio exposure limit exceeded |
| `auto_be_price` | float? | SL→breakeven price (set SL here after trigger) |
| `auto_be_trigger` | float? | Price that triggers SL→BE move |
| `score_components` | object? | Breakdown of score (wr, prob, rr, trend_align, vol_ok, liquidity_ok) |
| `kelly_suggested_size_usd` | float? | Kelly criterion optimal size |

### Decision Mapping (what the bot should do)

```
if recommendation == "reject":
    DO NOT OPEN. Log rejection_reason for debugging.

if recommendation == "reduce":
    OPEN with conviction_size_usd (reduced size).
    If conviction_size_usd is null, use: size_mult = max(0.3, min(0.7, signal_score))

if recommendation == "approve":
    OPEN with conviction_size_usd (full conviction).
    If conviction_size_usd is null, use: size_mult = max(0.7, min(1.0, signal_score))
```

**Preferred approach**: always use `conviction_size_usd` for position sizing.
It incorporates Kelly criterion, volatility, and portfolio exposure. The
`signal_score` → `size_mult` mapping above is a legacy fallback.

### Error Responses

| HTTP | Meaning | Bot action |
|------|---------|------------|
| 200 | Success | Read `recommendation` from body |
| 403 | Bad secret | Fix `X-Webhook-Secret` header |
| 400 | Malformed payload | Fix request body |
| 500 | Internal error | Treat as RE unavailable |

### Timeout Handling

If RE does not respond within **25 seconds**:
- Approve with **30% of configured risk size**
- Log the timeout for post-mortem analysis
- This matches the existing bridge fallback behavior

---

## 3. Trade Events — `POST /trade-outcome`

Report all trade lifecycle events (opens, closes, TP/SL hits) to RE. This
data feeds ML training, analytics, and position state sync.

### Request

```
POST http://<RE_HOST>:8000/trade-outcome
Content-Type: application/json
X-Webhook-Secret: <WEBHOOK_SECRET>
```

```json
{
  "hash": "a1b2c3d4e5f6",
  "event": "sl_hit",
  "symbol": "WIFUSDT",
  "side": "long",
  "price": 0.189,
  "pnl_pct": -5.2,
  "size_remaining": 0.0,
  "comment": "SL triggered at support"
}
```

### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hash` | string | recommended | Signal hash (links to original signal). Without it, event is still recorded but not linked to analytics |
| `event` | string | **yes** | Event type (see table below) |
| `symbol` | string | **yes** | e.g. `ETHUSDT` |
| `side` | string | recommended | `"long"` or `"short"` |
| `price` | float | recommended | Price at event (exchange fill price) |
| `pnl_pct` | float | recommended | Realized PnL percentage for this event |
| `size_remaining` | float | no | Remaining position size after event (0.0 = fully closed) |
| `comment` | string | no | Optional context or notes |

### Event Types

| Event | When to send | Description |
|-------|-------------|-------------|
| `open` | Position opened on exchange | Confirms actual entry (updates entry price if different from signal) |
| `tp1_hit` | TP1 partial take-profit | First target reached |
| `tp2_hit` | TP2 partial take-profit | Second target reached |
| `tp3_hit` | TP3 full take-profit | Final target, full exit |
| `sl_hit` | Stop-loss triggered | Position fully closed by SL |
| `full_close` | Manual full exit | Trader or bot closed entire position |
| `timeout` | Time-based exit | Position exceeded max hold duration |
| `dumalka_close` | RE command exit | Closed by Risk Engine command |
| `manual_close` | Manual override | Human intervention close |
| `flip_close` | Reversal close | Closed due to opposite-direction signal |
| `apollo_full_exit` | Apollo bail exit | Apollo strategy triggered full exit |
| `zone_full_exit` | Zone policy exit | Zone-based exit policy triggered |
| `e_pnl_full_exit` | Expected PnL exit | E[PnL] below threshold |
| `position_increased` | Position merge | Second signal merged into existing position |

**Closing events** (RE marks position as closed in DB):
`sl_hit`, `tp3_hit`, `full_close`, `timeout`, `apollo_full_exit`,
`zone_full_exit`, `e_pnl_full_exit`, `dumalka_close`, `manual_close`,
`flip_close`

### Response (200 OK)

```json
{
  "status": "recorded",
  "hash": "a1b2c3d4e5f6",
  "event": "sl_hit",
  "linked_to_signal": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"recorded"` on success |
| `hash` | string? | Echo of provided hash |
| `event` | string | Echo of event type |
| `linked_to_signal` | bool | `true` if hash matched a known signal in DB |

---

## 4. Dumalka Commands — `POST /dumalka/command` (Bot-side endpoint)

RE sends position management commands to the bot. **No changes** to this
contract — listed here for completeness.

### Request (RE → Bot)

```
POST http://<BOT_HOST>:8001/dumalka/command
Content-Type: application/json
X-Dumalka-Token: <DUMALKA_TOKEN>
```

### Command Types

#### `move_sl` — Adjust Stop-Loss
```json
{"action": "move_sl", "symbol": "ETHUSDT", "new_sl": 1950.0, "trace_id": "pos_42"}
```

#### `full_close` — Emergency Full Exit
```json
{"action": "full_close", "symbol": "ETHUSDT", "reason": "Hard SL Cap 3.5%", "trace_id": "pos_42"}
```

#### `partial_close` — Partial Take-Profit
```json
{"action": "partial_close", "symbol": "ETHUSDT", "fraction": 0.25, "reason": "Zone 2 DD Protection"}
```

#### `place_limit_tp` — Maker Limit Grid
```json
{"action": "place_limit_tp", "symbol": "ETHUSDT", "fraction": 0.3, "target_price": 2050.0, "reason": "Maker Limit Grid 3R"}
```

#### `move_tp` — Update Take-Profit
```json
{"action": "move_tp", "symbol": "ETHUSDT", "new_tp": 2100.0}
```

---

## 5. Roster Sync — `GET /dumalka/positions` (Bot-side endpoint)

RE polls this every 30 seconds to verify which positions the bot actually
has open. **No changes** to this contract.

```
GET http://<BOT_HOST>:8001/dumalka/positions
X-Dumalka-Token: <DUMALKA_TOKEN>
```

Response: list of active symbols (the exact format the bot already provides).

---

## 6. Testing with cURL

### Signal approval
```bash
curl -s -X POST http://localhost:8000/tv-webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{
    "symbol": "ETHUSDT",
    "side": "long",
    "size": 1.0,
    "source": "bot_direct",
    "signal_hash": "test_'$(date +%s)'",
    "entry_low": 1980.0,
    "entry_high": 1990.0,
    "stop_loss": 1950.0,
    "tp1": 2020.0,
    "tp2": 2060.0,
    "tp3": 2100.0,
    "risk_reward": 2.5,
    "probability": 65.0,
    "win_rate": 60.0,
    "trend": "moderate_bull",
    "volume_level": "high"
  }' | python3 -m json.tool
```

### Trade event (close)
```bash
curl -s -X POST http://localhost:8000/trade-outcome \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{
    "hash": "test_signal_hash",
    "event": "sl_hit",
    "symbol": "ETHUSDT",
    "side": "long",
    "price": 1950.0,
    "pnl_pct": -2.1,
    "size_remaining": 0.0
  }' | python3 -m json.tool
```

### Trade event (open confirmation)
```bash
curl -s -X POST http://localhost:8000/trade-outcome \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{
    "hash": "test_signal_hash",
    "event": "open",
    "symbol": "ETHUSDT",
    "side": "long",
    "price": 1985.50,
    "size_remaining": 0.05,
    "comment": "Filled at 1985.50 (slippage: 0.02%)"
  }' | python3 -m json.tool
```

---

## 7. Migration Checklist (Bot Developer)

### Phase 1: Signal Approval (replace Telegram → direct API)

- [ ] Implement `POST /tv-webhook` call with `source: "bot_direct"`
- [ ] Read `recommendation` from HTTP response (not Telegram callback)
- [ ] Implement decision mapping: reject / reduce / approve
- [ ] Use `conviction_size_usd` for position sizing
- [ ] Handle `rejection_reason` for logging
- [ ] Implement 25s timeout with 30% fallback
- [ ] Set `auto_be_price` / `auto_be_trigger` on the exchange if provided
- [ ] Stop posting "Request for approval" to Telegram channel
- [ ] Stop listening for callback on `/api/re/callback`

### Phase 2: Trade Events (replace Telegram → direct API)

- [ ] Send `event: "open"` to `/trade-outcome` when position opens on exchange
- [ ] Send `event: "tp1_hit"` / `"tp2_hit"` / `"tp3_hit"` on partial takes
- [ ] Send `event: "sl_hit"` on stop-loss trigger
- [ ] Send `event: "full_close"` / `"manual_close"` / `"flip_close"` as appropriate
- [ ] Send `event: "dumalka_close"` when closing by RE command
- [ ] Include `pnl_pct` and `price` in all close events
- [ ] Stop posting trade event text to Telegram channel

### Phase 3: Verification

- [ ] Confirm `/health` endpoint shows `bot_connection: ok`
- [ ] Verify positions appear in RE dashboard after opening
- [ ] Verify close events update RE dashboard within seconds (not minutes)
- [ ] Confirm `linked_to_signal: true` in trade-outcome responses
- [ ] Monitor Telegram for formatted reports from RE (human observability)

### No changes needed

- [ ] `/dumalka/command` — continues as-is
- [ ] `/dumalka/positions` — continues as-is
- [ ] Dumalka fallback timeout (15 min without commands → classic mode) — continues as-is

---

## 8. Architecture Diagram

```
┌──────────────┐         ┌─────────────────────────────────┐
│ Trading Bot  │         │ Risk Engine (GPU Titan V)        │
│   (VPS)      │         │                                  │
│              │─POST───►│ /tv-webhook                      │
│              │◄────────│   → scoring + MC → decision      │
│              │         │   → TG report (async)            │
│              │         │                                  │
│              │─POST───►│ /trade-outcome                   │
│              │◄────────│   → DB write → TG notify (async) │
│              │         │                                  │
│ /dumalka/    │◄─POST──│ position_tracker                  │
│   command    │         │   (move_sl, full_close, etc.)    │
│              │         │                                  │
│ /dumalka/    │◄─GET───│ position_tracker                  │
│   positions  │         │   (roster sync every 30s)        │
└──────────────┘         └─────────────────────────────────┘
                                    │
                                    ▼ (async, fire-and-forget)
                          ┌─────────────────┐
                          │ Telegram Channel │
                          │ (humans only)    │
                          │  • Signal reports│
                          │  • Trade events  │
                          │  • Watchdog      │
                          │  • Dumalka digest│
                          └─────────────────┘
```

---

## 9. FAQ

**Q: Can I still use the Telegram bridge in parallel during migration?**
A: Yes. Phase 1–2 are designed for parallel operation. The bridge continues
to work independently. Once all traffic flows via API, the bridge can be
deprecated.

**Q: What if I send the same signal_hash twice?**
A: The dedup guard (5s window) is bypassed for `source: "bot_direct"`. But
RE has a state guard that rejects signals if a position for the same symbol
is already open (unless re-entry conditions are met). Check `rejection_reason`
in the response.

**Q: What if RE is down or unreachable?**
A: Approve with 30% of configured size. Log the event. RE's Health Watchdog
will alert admins via Telegram if RE is unhealthy.

**Q: Do I need to send `event: "open"` if RE already registered the position?**
A: Recommended but not strictly required. RE registers the position on
`/tv-webhook` approval. The `open` event is a safety net that updates the
entry price to the actual exchange fill price (may differ from signal price
due to slippage).

**Q: What happens if I send an unknown event type?**
A: It is still recorded in `trade_outcomes` but does not trigger
position state changes. Stick to the documented event types.
