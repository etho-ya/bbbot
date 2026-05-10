# Bot Integration Changelog — v0.10.2 → v0.10.6

Changes made by the Midas Trading Bot team affecting REDU integration.

## REDU Changes (this PR)

### telegram_bridge.py — Missing close event types

**Problem:** When the bot closes a position via Dumalka command, manual action, or
opposite-direction flip, the resulting trade event (`dumalka_close`, `manual_close`,
`flip_close`) was NOT recognized by `process_trade_event()`. This left phantom
`open_positions` rows with `status = 'open'` in REDU's database, which caused
`already_in_position` guards to falsely reject new signals for those symbols.

**Fix:** Added `dumalka_close`, `manual_close`, `flip_close` to the close-event
whitelist in `telegram_bridge.py` (line ~502).

```python
# Before:
if event_type in ('sl_hit', 'tp3_hit', 'full_close', 'timeout',
                  'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit'):

# After:
if event_type in ('sl_hit', 'tp3_hit', 'full_close', 'timeout',
                  'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit',
                  'dumalka_close', 'manual_close', 'flip_close'):
```

---

## Bot-Side Changes (already deployed)

### 1. Per-symbol asyncio.Lock (CRITICAL)

Prevents race condition where two identical signals arriving within milliseconds
could open duplicate positions on Bybit for the same symbol.

### 2. Opposite-direction flip close

When a BUY signal arrives for a symbol with an active SELL (or vice versa),
the bot now explicitly closes the old position first, notifies REDU with
`event_type: flip_close`, then opens the new direction.

### 3. Duplicate Dumalka command deduplication

Bot deduplicates `full_close` commands by `symbol:trace_id`. If REDU's
`position_tracker` and `tv_webhook` both send `full_close` for the same
position within a short window, the second one is safely skipped.

### 4. dumalka_closing flag reset on failure

If `full_close` fails on Bybit (e.g., position already zero), the
`dumalka_closing` flag is now reset to `False`, allowing the position to
be retried or cleaned up.

### 5. TP/SL error propagation

`set_trading_stop_combined` returning `False` is now logged as ERROR with
explicit "Position opened WITHOUT TP/SL protection!" message.

### 6. closedPnl retry mechanism

After a position close, Bybit often doesn't return `closedPnl` immediately.
Bot now retries 3 times with delays of 1s, 2s, 3s before falling back to
estimate.

### 7. RE query timeout increased (10s → 25s)

REDU v0.19.x ML scoring takes longer. Bot HTTP timeout for `/tv-webhook`
increased from 10s to 25s.

### 8. Balance calculation fix (walletBalance → equity)

Bot was using `walletBalance` (deposits + realized PnL) which does NOT
include unrealised PnL from open positions. Switched to `equity`
(walletBalance + unrealisedPnl) for accurate margin sizing, daily loss
limits, and UI display.

---

## Event Types Reference

The bot sends these `event_type` values in trade events to REDU:

| event_type | Trigger |
|---|---|
| `open` | New position opened |
| `position_increased` | Same-direction merge (2nd signal) |
| `sl_hit` | Stop-loss triggered on Bybit |
| `tp3_hit` | TP3 hit on Bybit |
| `full_close` | Explicit close via Bybit API |
| `dumalka_close` | Close initiated by REDU command |
| `manual_close` | Close initiated by user/admin |
| `flip_close` | Old position closed due to opposite-direction signal |
| `timeout` | Position timed out |
