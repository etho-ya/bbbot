# Midas ↔ Risk Engine (Dumalka) Integration Guide

This document is specifically prepared for the Risk Engine (RE) developer to align on the integration architecture, resolve the current 89% rejection rate, and finalize the required endpoints.

---

## 1. Problem Analysis: Why 89% Rejection Occurs

Currently, Midas and Dumalka (RE) operate out of sync. Based on your feedback, this happens because:
1. **Blind Operation:** RE does not have a way to fetch the actual state of open positions (`GET /positions` is missing).
2. **Missing Closures:** Bybit natively executes Stop-Loss (SL) and Take-Profit (TP). Midas detects these closures via polling but **does not notify RE**. Consequently, RE tries to manage a position that Bybit already closed.
3. **Manager Conflicts:** While Midas has a partial fallback ([_should_use_dumalka](file:///opt/trading-bot/midas-trading-bot/app/services/trade_manager.py#362-375)), Midas still attempts to move SL to breakeven automatically when TP2 is reached. This conflicts with RE's Zone Policy and Apollo Protocol.

---

## 2. Proposed Architecture: "Bot Opens, Dumalka Manages"

We fully agree with your proposed architecture: **Midas will act purely as the execution layer, while RE will be the sole brain managing the trade lifecycle.**

### Flow of Execution
1. **Signal & Entry:** Midas receives the signal, calculates position size, and executes a Market Order on Bybit.
2. **Initial Protections:** Midas sets the **initial SL** natively on Bybit (as a safety net) and saves the trade in its database.
3. **Takeover:** RE detects the new position (via polling `GET /positions` or a future webhook) and takes over management.
4. **Active Management:** RE sends commands (`partial_close`, `move_sl`, `move_tp`, `full_close`) to Midas via `POST /dumalka/command`. Midas executes them strictly without applying Midas-specific logic.
5. **Closure Notification:** If Bybit natively hits the SL or RE issues a full close, Midas detects `pos_size == 0` and immediately fires a webhook (`POST /trade-outcome`) to RE.

---

## 3. Required Endpoints to Implement (The "3 Blockers")

If you confirm this plan, we will implement the following on the Midas Backend:

### Blocker 1: Disable Internal TP/SL Management
- **What we will change:** When `DUMALKA_MODE` is active, Midas will **completely disable** its internal [monitor_trade](file:///opt/trading-bot/midas-trading-bot/app/services/trade_manager.py#397-511) logic for moving SL to breakeven. 
- Midas will only poll Bybit to check if `pos_size == 0` (to detect native SL/TP hits). No other autonomous modifications will be made.

### Blocker 2: `GET /dumalka/positions` (New Endpoint in Midas)
- **Purpose:** Allows RE to fetch the current actual state of all positions managed by Midas.
- **Response Format Proposal:**
```json
{
  "ok": true,
  "positions": [
    {
      "trade_id": "uuid-string",
      "symbol": "BTCUSDT",
      "side": "LONG",
      "entry_price": 65000.50,
      "size_usdt": 150.00,
      "leverage": 20,
      "current_sl": 64000.00,
      "current_tp": 67000.00,
      "stage": "OPEN"
    }
  ]
}
```

### Blocker 3: `POST /trade-outcome` (Webhook sent FROM Midas TO RE)
- **Purpose:** Midas will actively notify RE the moment it detects a trade has been fully closed.
- **Config:** We will add `RE_WEBHOOK_URL` to Midas's [.env](file:///opt/trading-bot/midas-trading-bot/.env).
- **Payload Proposal:**
```json
{
  "trade_id": "uuid-string",
  "symbol": "BTCUSDT",
  "side": "LONG",
  "pnl_usdt": -15.50,
  "pnl_percent": -10.0,
  "close_reason": "SL_HIT",  // or "TP_HIT", "MANUAL_CLOSE", "RE_FULL_CLOSE"
  "closed_at": "2026-03-27T10:00:00Z"
}
```

---

## 4. Next Steps & Feedback Request

To the Risk Engine Developer:
1. **Does the proposed payload for `GET /dumalka/positions` contain all the specific data points your Zone Policy and Time-Decay logic need?** (e.g., Do you need `timestamp_opened`, `unrealized_pnl`, or `signal_hash` included?)
2. **Does the proposed payload for the `POST /trade-outcome` webhook satisfy the RE's state-clearing requirements?**
3. **How does RE prefer to know about newly opened trades?** Should Midas also send a `POST /trade-opened` webhook upon entry, or will RE rely entirely on polling `GET /dumalka/positions`?

Please review these proposals. Once confirmed, we will implement these 3 changes in Midas to unblock the 1,500+ LOC and activate the Apollo Protocol / Zone Policies.
