"""
Binance Sentinel — Flash Crash Detection via Lead-Lag Oracle (v0.10.1)

Monitors BTC price on Binance Futures via public WebSocket.
When a flash crash is detected (rapid price drop), triggers emergency
closure of all positions on Bybit through the Risk Engine.

Architecture:
  - Runs as asyncio background task in main.py
  - Listens to wss://fstream.binance.com/ws/btcusdt@aggTrade
  - Maintains a rolling 5-second price window
  - Two alert levels:
    ALERT:     price drops ≥0.5% in 5s → log warning + TG notification
    EMERGENCY: price drops ≥1.0% in 5s → trigger full_close on ALL positions

Lead-Lag rationale:
  Binance futures market moves 10-50ms faster than Bybit during cascading
  liquidations. By monitoring Binance and acting on Bybit, we exit AT or
  NEAR the pre-crash price before Bybit's orderbook thins out.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("risk-engine.sentinel")

# ── Configuration ─────────────────────────────────────────────────────────────
BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
PRICE_WINDOW_SEC = 5.0       # Rolling window for velocity calculation
ALERT_THRESHOLD_PCT = -0.5   # ≥0.5% drop in 5s → ALERT
EMERGENCY_THRESHOLD_PCT = -1.0  # ≥1.0% drop in 5s → EMERGENCY
RECONNECT_DELAY_SEC = 5      # Wait before reconnecting on disconnect
HEARTBEAT_INTERVAL_SEC = 60  # Log heartbeat every 60s


class BinanceSentinel:
    """
    Monitors BTC/USDT on Binance Futures for flash crashes.
    Calls `on_emergency` callback when price drops exceed threshold.
    """

    def __init__(self, on_emergency: Optional[Callable[..., Awaitable]] = None,
                 on_alert: Optional[Callable[..., Awaitable]] = None):
        self.on_emergency = on_emergency
        self.on_alert = on_alert
        self._price_window: deque = deque()  # (timestamp, price) tuples
        self._last_heartbeat: float = 0
        self._emergency_cooldown: float = 0  # Prevent rapid re-triggers
        self._running = False
        self.stats = {
            "connected_at": None,
            "last_price": None,
            "alerts": 0,
            "emergencies": 0,
            "messages_received": 0,
            "reconnects": 0,
        }

    async def run(self):
        """Main loop: connect, listen, auto-reconnect on failure."""
        self._running = True
        logger.info(
            f"🛡️ [SENTINEL] Starting Binance Flash Crash Monitor "
            f"(alert={ALERT_THRESHOLD_PCT}%, emergency={EMERGENCY_THRESHOLD_PCT}%)"
        )

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("🛡️ [SENTINEL] Shutting down gracefully")
                break
            except Exception as e:
                self.stats["reconnects"] += 1
                logger.warning(
                    f"🛡️ [SENTINEL] WebSocket error: {e}. "
                    f"Reconnecting in {RECONNECT_DELAY_SEC}s... "
                    f"(reconnect #{self.stats['reconnects']})"
                )
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _connect_and_listen(self):
        """Connect to Binance WebSocket and process aggTrade messages."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "🛡️ [SENTINEL] 'websockets' package not installed. "
                "Install with: pip install websockets"
            )
            # Don't retry in a tight loop if the package is missing
            await asyncio.sleep(3600)
            return

        async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
            self.stats["connected_at"] = time.time()
            logger.info("🛡️ [SENTINEL] Connected to Binance Futures WebSocket")

            async for msg in ws:
                try:
                    data = json.loads(msg)
                    price = float(data.get("p", 0))
                    if price <= 0:
                        continue

                    now = time.time()
                    self.stats["messages_received"] += 1
                    self.stats["last_price"] = price

                    # Add to rolling window
                    self._price_window.append((now, price))

                    # Trim window to PRICE_WINDOW_SEC
                    cutoff = now - PRICE_WINDOW_SEC
                    while self._price_window and self._price_window[0][0] < cutoff:
                        self._price_window.popleft()

                    # Calculate price change over window
                    if len(self._price_window) >= 2:
                        oldest_price = self._price_window[0][1]
                        change_pct = ((price - oldest_price) / oldest_price) * 100

                        # ── EMERGENCY: ≥1.0% drop in 5s ──
                        if change_pct <= EMERGENCY_THRESHOLD_PCT:
                            if now - self._emergency_cooldown > 60:  # 1 min cooldown
                                self._emergency_cooldown = now
                                self.stats["emergencies"] += 1
                                logger.critical(
                                    f"🚨 [SENTINEL EMERGENCY] BTC Flash Crash! "
                                    f"{change_pct:.2f}% in {PRICE_WINDOW_SEC:.0f}s "
                                    f"(${oldest_price:.2f} → ${price:.2f})"
                                )
                                if self.on_emergency:
                                    try:
                                        await self.on_emergency(change_pct, price, oldest_price)
                                    except Exception as e:
                                        logger.error(f"[SENTINEL] Emergency callback failed: {e}")

                        # ── ALERT: ≥0.5% drop in 5s ──
                        elif change_pct <= ALERT_THRESHOLD_PCT:
                            self.stats["alerts"] += 1
                            if self.stats["alerts"] % 5 == 1:  # Log every 5th alert
                                logger.warning(
                                    f"⚠️ [SENTINEL ALERT] BTC dropping: "
                                    f"{change_pct:.2f}% in {PRICE_WINDOW_SEC:.0f}s "
                                    f"(${oldest_price:.2f} → ${price:.2f})"
                                )
                            if self.on_alert:
                                try:
                                    await self.on_alert(change_pct, price, oldest_price)
                                except Exception:
                                    pass  # Alert callbacks are non-critical

                    # Heartbeat logging
                    if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                        self._last_heartbeat = now
                        logger.debug(
                            f"🛡️ [SENTINEL] Heartbeat: BTC=${price:.2f} "
                            f"msgs={self.stats['messages_received']} "
                            f"alerts={self.stats['alerts']} "
                            f"emergencies={self.stats['emergencies']}"
                        )

                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"[SENTINEL] Parse error: {e}")

    def stop(self):
        """Gracefully stop the sentinel."""
        self._running = False

    def get_status(self) -> dict:
        """Return current status for /health endpoint."""
        return {
            "active": self._running,
            "last_price": self.stats["last_price"],
            "messages_received": self.stats["messages_received"],
            "alerts": self.stats["alerts"],
            "emergencies": self.stats["emergencies"],
            "reconnects": self.stats["reconnects"],
            "window_size": len(self._price_window),
        }


# ── Global instance (created during startup) ────────────────────────────────
_sentinel: Optional[BinanceSentinel] = None


async def start_sentinel(emergency_callback=None, alert_callback=None):
    """
    Called from main.py startup. Creates global sentinel instance and runs it.
    """
    global _sentinel
    _sentinel = BinanceSentinel(
        on_emergency=emergency_callback,
        on_alert=alert_callback,
    )
    await _sentinel.run()


def get_sentinel_status() -> dict:
    """Called by /health endpoint to report sentinel status."""
    if _sentinel:
        return _sentinel.get_status()
    return {"active": False, "reason": "not started"}
