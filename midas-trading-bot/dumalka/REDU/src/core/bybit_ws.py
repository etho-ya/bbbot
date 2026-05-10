"""
Bybit WebSocket Price Feed — Real-Time Ticker Monitor (v0.15.1 Experimental)

Connects to Bybit's public Linear Futures WebSocket for real-time price
updates on symbols with active positions. This runs in SHADOW MODE only:
it logs observations but does NOT execute any SL/TP commands.

Architecture:
  - Runs as asyncio background task in main.py
  - Listens to wss://stream.bybit.com/v5/public/linear
  - Subscribes to `tickers.{symbol}` for active position symbols
  - Stores latest prices in a shared dict for dashboard + future SL→BE triggers
  - Re-subscribes every 5 minutes to pick up new/closed positions

Why shadow mode first:
  - Validate WebSocket stability on this server
  - Measure latency vs REST polling (60s cycle)
  - Confirm price accuracy matches REST data
  - Zero risk — observation only

Geoblock note:
  Bybit WebSocket `stream.bybit.com` is accessible from this server
  (unlike `api.bybit.com` which requires proxy). No special handling needed.
"""

import asyncio
import json
import logging
import time
from typing import Optional, Callable, Awaitable, Dict, Set

logger = logging.getLogger("risk-engine.bybit-ws")

# ── Configuration ─────────────────────────────────────────────────────────────
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
RECONNECT_DELAY_SEC = 5
HEARTBEAT_LOG_INTERVAL_SEC = 60   # Log status every 60s
RESUB_INTERVAL_SEC = 300          # Re-subscribe to pick up new symbols every 5 min
MAX_RECONNECTS_BEFORE_BACKOFF = 10
BACKOFF_DELAY_SEC = 60


class BybitPriceFeed:
    """
    Real-time price feed from Bybit WebSocket.
    Shadow mode: collects prices, does NOT act on them.
    """

    def __init__(self, get_active_symbols: Optional[Callable[[], Set[str]]] = None):
        """
        Args:
            get_active_symbols: Callback that returns current set of symbols
                with open positions. Called every RESUB_INTERVAL_SEC to update
                subscriptions.
        """
        self._get_active_symbols = get_active_symbols
        self._running = False
        self._subscribed_symbols: Set[str] = set()

        # ── Public state ──────────────────────────────────────────────────
        # Latest prices: {symbol: {price, bid, ask, timestamp, ...}}
        self.prices: Dict[str, dict] = {}

        # Stats for /health and dashboard
        self.stats = {
            "connected_at": None,
            "last_message_at": None,
            "messages_received": 0,
            "reconnects": 0,
            "subscribed_symbols": 0,
            "price_updates_per_sec": 0.0,
            "avg_latency_ms": 0.0,  # WS vs REST comparison
        }

        # Price history for latency and accuracy analysis
        self._msg_count_window: list = []  # timestamps for rate calc
        self._latency_samples: list = []   # last 100 latency measurements

    async def run(self):
        """Main loop: connect, listen, auto-reconnect on failure."""
        self._running = True
        consecutive_fails = 0

        logger.info(
            "📡 [BYBIT-WS] Starting Bybit Price Feed (SHADOW MODE) "
            f"reconnect_delay={RECONNECT_DELAY_SEC}s"
        )

        while self._running:
            try:
                await self._connect_and_listen()
                consecutive_fails = 0
            except asyncio.CancelledError:
                logger.info("📡 [BYBIT-WS] Shutting down gracefully")
                break
            except Exception as e:
                consecutive_fails += 1
                self.stats["reconnects"] += 1

                delay = (
                    BACKOFF_DELAY_SEC
                    if consecutive_fails > MAX_RECONNECTS_BEFORE_BACKOFF
                    else RECONNECT_DELAY_SEC
                )

                logger.warning(
                    f"📡 [BYBIT-WS] Connection error: {e}. "
                    f"Reconnecting in {delay}s... "
                    f"(attempt #{self.stats['reconnects']})"
                )
                await asyncio.sleep(delay)

    async def _connect_and_listen(self):
        """Connect to Bybit WebSocket and process ticker messages."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "📡 [BYBIT-WS] 'websockets' package not installed. "
                "Install with: pip install websockets"
            )
            await asyncio.sleep(3600)
            return

        async with websockets.connect(
            BYBIT_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self.stats["connected_at"] = time.time()
            logger.info("📡 [BYBIT-WS] Connected to Bybit Linear Futures WebSocket")

            # Initial subscription
            await self._subscribe(ws)

            # Background task: re-subscribe periodically
            resub_task = asyncio.create_task(self._resub_loop(ws))

            try:
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        await self._process_message(data)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.debug(f"[BYBIT-WS] Parse error: {e}")
            finally:
                resub_task.cancel()
                try:
                    await resub_task
                except asyncio.CancelledError:
                    pass

    async def _subscribe(self, ws):
        """Subscribe to ticker channels for active symbols."""
        symbols = set()

        if self._get_active_symbols:
            try:
                symbols = self._get_active_symbols()
            except Exception as e:
                logger.warning(f"[BYBIT-WS] Failed to get active symbols: {e}")

        if not symbols:
            # Default: subscribe to a few major pairs for testing
            symbols = {"BTCUSDT", "ETHUSDT"}

        # Calculate diff
        to_unsub = self._subscribed_symbols - symbols
        to_sub = symbols - self._subscribed_symbols

        # Unsubscribe removed symbols
        if to_unsub:
            unsub_args = [f"tickers.{s}" for s in to_unsub]
            await ws.send(json.dumps({
                "op": "unsubscribe",
                "args": unsub_args,
            }))
            logger.info(f"📡 [BYBIT-WS] Unsubscribed: {to_unsub}")

        # Subscribe new symbols
        if to_sub:
            sub_args = [f"tickers.{s}" for s in to_sub]
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": sub_args,
            }))
            logger.info(f"📡 [BYBIT-WS] Subscribed: {to_sub}")

        self._subscribed_symbols = symbols
        self.stats["subscribed_symbols"] = len(symbols)

    async def _resub_loop(self, ws):
        """Periodically re-subscribe to update symbol list."""
        while True:
            await asyncio.sleep(RESUB_INTERVAL_SEC)
            try:
                await self._subscribe(ws)
            except Exception as e:
                logger.warning(f"[BYBIT-WS] Resub failed: {e}")

    async def _process_message(self, data: dict):
        """Process incoming ticker message."""
        # Bybit v5 WebSocket response format:
        # {"topic": "tickers.BTCUSDT", "type": "snapshot"/"delta", "data": {...}}

        topic = data.get("topic", "")
        if not topic.startswith("tickers."):
            return  # Skip subscription confirmations, pongs, etc.

        symbol = topic.replace("tickers.", "")
        ticker_data = data.get("data", {})

        now = time.time()
        self.stats["messages_received"] += 1
        self.stats["last_message_at"] = now

        # Extract price fields
        last_price = float(ticker_data.get("lastPrice", 0))
        if last_price <= 0:
            return

        bid = float(ticker_data.get("bid1Price", 0))
        ask = float(ticker_data.get("ask1Price", 0))
        mark_price = float(ticker_data.get("markPrice", 0))
        price_24h_pct = float(ticker_data.get("price24hPcnt", 0))

        # Store latest price
        prev = self.prices.get(symbol)
        self.prices[symbol] = {
            "price": last_price,
            "bid": bid,
            "ask": ask,
            "mark": mark_price,
            "change_24h_pct": price_24h_pct * 100,  # Convert to percentage
            "spread_pct": ((ask - bid) / last_price * 100) if last_price > 0 and bid > 0 else 0,
            "updated_at": now,
            "updates": (prev["updates"] + 1) if prev else 1,
        }

        # Track message rate (updates per second over last 10s)
        self._msg_count_window.append(now)
        cutoff = now - 10.0
        self._msg_count_window = [t for t in self._msg_count_window if t > cutoff]
        self.stats["price_updates_per_sec"] = round(len(self._msg_count_window) / 10.0, 1)

        # Heartbeat logging
        if now - (self._last_heartbeat if hasattr(self, '_last_heartbeat') else 0) >= HEARTBEAT_LOG_INTERVAL_SEC:
            self._last_heartbeat = now
            symbols_str = ", ".join(
                f"{s}=${p['price']:.2f}" for s, p in sorted(self.prices.items())
            )
            logger.info(
                f"📡 [BYBIT-WS] Heartbeat: {self.stats['price_updates_per_sec']}/s "
                f"| {len(self.prices)} symbols | {symbols_str}"
            )

    def stop(self):
        """Gracefully stop the price feed."""
        self._running = False

    def get_status(self) -> dict:
        """Return current status for /health and dashboard API."""
        now = time.time()
        connected_for = (
            round(now - self.stats["connected_at"])
            if self.stats["connected_at"] else 0
        )
        last_msg_ago = (
            round(now - self.stats["last_message_at"], 1)
            if self.stats["last_message_at"] else None
        )

        return {
            "active": self._running,
            "mode": "shadow",
            "connected_for_sec": connected_for,
            "last_message_ago_sec": last_msg_ago,
            "messages_received": self.stats["messages_received"],
            "price_updates_per_sec": self.stats["price_updates_per_sec"],
            "subscribed_symbols": self.stats["subscribed_symbols"],
            "reconnects": self.stats["reconnects"],
            "symbols": list(self._subscribed_symbols),
        }

    def get_prices(self) -> Dict[str, dict]:
        """Return all current prices for dashboard."""
        return dict(self.prices)

    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest price for a symbol. Returns None if not available."""
        entry = self.prices.get(symbol)
        return entry["price"] if entry else None


# ── Global instance ──────────────────────────────────────────────────────────
_price_feed: Optional[BybitPriceFeed] = None


async def start_price_feed(get_active_symbols=None):
    """
    Called from main.py startup. Creates global price feed and runs it.
    """
    global _price_feed
    _price_feed = BybitPriceFeed(get_active_symbols=get_active_symbols)
    await _price_feed.run()


def get_price_feed_status() -> dict:
    """Called by /health and dashboard to report price feed status."""
    if _price_feed:
        return _price_feed.get_status()
    return {"active": False, "reason": "not started"}


def get_ws_prices() -> Dict[str, dict]:
    """Called by dashboard API to get all current prices."""
    if _price_feed:
        return _price_feed.get_prices()
    return {}


def get_ws_price(symbol: str) -> Optional[float]:
    """Called by position tracker to get real-time price."""
    if _price_feed:
        return _price_feed.get_price(symbol)
    return None
