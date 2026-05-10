"""
Market Data Client — v0.13.2 (671 LOC)

3-tier fallback architecture:
  Primary:  Bybit Proxy on VPS (via Tailscale) → real Bybit data
  Fallback: Binance.US Spot (orderbook, klines) + OKX Public (funding, OI, LSR)
  Default:  Graceful zero/None for ML safety

Functions:
  Core Market Data:
    fetch_market_data(symbol)       → (price, annualized_vol)
    fetch_volume_info(symbol)       → volume_ratio
    fetch_orderbook_depth(symbol)   → (spread_pct, slippage_pct)
    fetch_orderbook_raw(symbol)     → (bids, asks)
    fetch_multi_timeframe(symbol)   → {15m, 1h, 4h: direction}
    fetch_funding_and_oi(symbol)    → (funding_rate, oi_change_pct)
  Account:
    fetch_closed_pnl()              → [records]
    fetch_wallet_balance()          → {equity, available, ...}
  ML Intelligence Layer (v0.13.2):
    fetch_btc_change_1h()           → BTC 1h price change (Binance.US)
    fetch_rsi_from_klines(symbol)   → RSI-14 (Bybit → Binance.US fallback)
    compute_rsi(closes)             → RSI from price array (pure math)
    compute_orderbook_imbalance()   → bid/ask volume ratio
    fetch_long_short_ratio(symbol)  → OKX account L/S ratio
"""
import httpx
import logging
import numpy as np
from config import config

logger = logging.getLogger("risk-engine.market")


async def fetch_market_data(symbol: str) -> tuple[float, float]:
    """
    Fetch current price + annualized volatility.
    Primary: VPS Bybit Proxy (100.117.168.63:8002) — with retry on transient errors
    Fallback: Binance US
    Returns: (price, annualized_vol)
    """
    import asyncio
    
    # ── Try VPS Bybit Proxy first (with retry for transient 500s) ──
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"{config.BYBIT_PROXY_URL}/market-data/{symbol}"
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()

                price = data["price"]
                klines = data.get("klines", [])

                if price > 0 and klines:
                    # Bybit klines: [timestamp, open, high, low, close, volume, turnover]
                    # They come newest-first, reverse for chronological order
                    closes = [float(k[4]) for k in klines]
                    closes.reverse()
                    if len(closes) >= 2:
                        log_returns = np.diff(np.log(closes))
                        hourly_vol = np.std(log_returns)
                        annual_vol = float(hourly_vol * np.sqrt(8760))
                        logger.info(f"[Bybit Proxy] {symbol}: price={price}, vol={annual_vol:.4f}, klines={len(klines)}")
                        return price, annual_vol

                if price > 0:
                    logger.info(f"[Bybit Proxy] {symbol}: price={price}, no klines for vol")
                    return price, config.VOL_FALLBACK

        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"[Bybit Proxy] Attempt {attempt+1}/{max_retries} failed for {symbol}: {e}. Retrying in 1s...")
                await asyncio.sleep(1.0)
            else:
                logger.warning(f"[Bybit Proxy] All {max_retries} attempts failed for {symbol}: {e}. Trying Binance fallback...")

    # ── Fallback: Binance US ───────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Price
            pr = await client.get(f"https://api.binance.us/api/v3/ticker/price?symbol={symbol}")
            price = float(pr.json()["price"])

            # Klines
            kr = await client.get(
                f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1h&limit={config.VOL_LOOKBACK_HOURS}"
            )
            closes = [float(k[4]) for k in kr.json()]
            if len(closes) >= 2:
                log_returns = np.diff(np.log(closes))
                hourly_vol = np.std(log_returns)
                annual_vol = float(hourly_vol * np.sqrt(8760))
                logger.info(f"[Binance] {symbol}: price={price}, vol={annual_vol:.4f}")
                return price, annual_vol

            return price, config.VOL_FALLBACK

    except Exception as e:
        logger.error(f"[Binance] Also failed for {symbol}: {e}")
        return 0.0, config.VOL_FALLBACK


async def fetch_volume_info(symbol: str) -> float:
    """
    Fetch volume ratio: last_hour_volume / avg_24h_volume.
    Primary: VPS Bybit Proxy (already included in /market-data response)
    Fallback: Binance US klines
    Returns: volume_ratio (1.0 = normal)
    """
    # ── Try VPS Bybit Proxy ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/market-data/{symbol}"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                ratio = data.get("volume_ratio", 1.0)
                logger.debug(f"[Bybit Proxy] {symbol}: volume_ratio={ratio}")
                return ratio
    except Exception as e:
        logger.warning(f"[Bybit Proxy] volume_info failed for {symbol}: {e}")

    # ── Fallback: Binance US ───────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1h&limit=24"
            r = await client.get(url)
            r.raise_for_status()
            klines = r.json()
            if len(klines) >= 2:
                volumes = [float(k[5]) for k in klines]
                avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
                return volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    except Exception as e:
        logger.error(f"[Binance] volume_info failed for {symbol}: {e}")

    return 1.0

# ── Fallback APIs (no auth required) ─────────────────────────────────────
# fapi.binance.com → GEO-BLOCKED from this server
# api.bybit.com   → GEO-BLOCKED from this server
# api.binance.us  → ✅ Works (Spot only: price, klines, orderbook)
# www.okx.com     → ✅ Works (Derivatives: funding rate, OI, no auth)
BINANCE_US_BASE = "https://api.binance.us/api/v3"
OKX_BASE = "https://www.okx.com/api/v5"

# Symbols that exist on Bybit but NOT on Binance.US
# These will skip the Binance fallback entirely (graceful degradation)
_BINANCE_EXCLUDED: set[str] = {
    "FARTCOINUSDT", "SAHARAUSDT", "AVAAIUSDT",  # Bybit-only memecoins
    # Add symbols as Binance 400 errors are discovered in logs
}


def _binance_us_symbol(bybit_symbol: str) -> str | None:
    """
    Check if a Bybit symbol exists on Binance.US.
    Returns None if symbol is known to NOT exist on Binance.US.
    Otherwise returns the symbol as-is (tickers match for most pairs).
    """
    if bybit_symbol in _BINANCE_EXCLUDED:
        return None
    return bybit_symbol


def _to_okx_inst_id(bybit_symbol: str) -> str:
    """
    Convert Bybit symbol (e.g. 'BTCUSDT') to OKX instId (e.g. 'BTC-USDT-SWAP').
    OKX uses dash-separated format for perpetual swaps.
    """
    # Strip 'USDT' suffix and rebuild as OKX format
    if bybit_symbol.endswith("USDT"):
        base = bybit_symbol[:-4]
        return f"{base}-USDT-SWAP"
    return bybit_symbol  # fallback, shouldn't happen


async def fetch_orderbook_depth(symbol: str, trade_size_usd: float = 100.0) -> tuple[float, float]:
    """
    Fetch orderbook and estimate spread + slippage for a given trade size.
    Primary: Bybit Proxy /orderbook/{symbol}
    Fallback: Binance.US Spot /depth
    Returns: (spread_pct, estimated_slippage_pct)
    """
    def _calc_spread_slippage(bids, asks, trade_size_usd):
        """Shared calculation for spread + slippage from orderbook data."""
        if not bids or not asks:
            return 0.0, 0.0
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2.0
        spread_pct = ((best_ask - best_bid) / mid_price) * 100 if mid_price > 0 else 0.0

        filled_usd = 0.0
        filled_qty = 0.0
        for ask_price, ask_qty in [(float(a[0]), float(a[1])) for a in asks]:
            level_usd = ask_price * ask_qty
            remaining = trade_size_usd - filled_usd
            if remaining <= 0:
                break
            take_usd = min(level_usd, remaining)
            take_qty = take_usd / ask_price
            filled_usd += take_usd
            filled_qty += take_qty
        if filled_qty > 0 and best_ask > 0:
            avg_fill = filled_usd / filled_qty
            slippage_pct = ((avg_fill - best_ask) / best_ask) * 100
        else:
            slippage_pct = 0.0
        return spread_pct, slippage_pct

    # ── Try VPS Bybit Proxy ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/orderbook/{symbol}"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if bids and asks:
                    spread, slippage = _calc_spread_slippage(bids, asks, trade_size_usd)
                    logger.info(
                        f"[Orderbook] {symbol}: spread={spread:.4f}% "
                        f"slippage={slippage:.4f}% (size=${trade_size_usd:.0f})"
                    )
                    return spread, slippage
    except Exception as e:
        logger.warning(f"[Orderbook] Bybit Proxy failed for {symbol}: {e}")

    # ── Fallback: Binance.US Spot ──────────────────────────────────
    b_sym = _binance_us_symbol(symbol)
    if b_sym:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{BINANCE_US_BASE}/depth", params={"symbol": b_sym, "limit": 20})
                if r.status_code == 200:
                    data = r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    if bids and asks:
                        spread, slippage = _calc_spread_slippage(bids, asks, trade_size_usd)
                        logger.info(
                            f"[Orderbook][Binance Fallback] {symbol}: spread={spread:.4f}% "
                            f"slippage={slippage:.4f}%"
                        )
                        return spread, slippage
        except Exception as e:
            logger.debug(f"[Orderbook][Binance Fallback] Failed for {symbol}: {e}")

    return 0.0, 0.0  # graceful fallback — no penalty


async def fetch_orderbook_raw(symbol: str) -> tuple[list, list]:
    """
    Fetch raw orderbook bids and asks for wall detection (Orderbook Shielding).
    Primary: Bybit Proxy → Fallback: Binance.US Spot
    Returns: (bids, asks) where each is [[price, qty], ...]
    """
    # ── Try VPS Bybit Proxy ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/orderbook/{symbol}"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
                asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]
                if bids or asks:
                    return bids, asks
    except Exception as e:
        logger.warning(f"[Orderbook Raw] Bybit Proxy failed for {symbol}: {e}")

    # ── Fallback: Binance.US Spot ──────────────────────────────────
    b_sym = _binance_us_symbol(symbol)
    if b_sym:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{BINANCE_US_BASE}/depth", params={"symbol": b_sym, "limit": 50})
                if r.status_code == 200:
                    data = r.json()
                    bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
                    asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]
                    if bids or asks:
                        logger.info(f"[Orderbook Raw][Binance Fallback] {symbol}: {len(bids)} bids, {len(asks)} asks")
                        return bids, asks
        except Exception as e:
            logger.debug(f"[Orderbook Raw][Binance Fallback] Failed for {symbol}: {e}")

    return [], []


async def fetch_multi_timeframe(symbol: str) -> dict | None:
    """
    Fetch klines on 3 timeframes (15m, 1h, 4h) and detect trend direction on each.
    Uses EMA20 vs EMA50 crossover for trend detection.
    Primary: Bybit Proxy → Fallback: Binance.US Spot klines
    Returns: {"15m": "bullish"|"bearish"|"neutral", "1h": ..., "4h": ...}
    """
    import asyncio

    # Bybit interval (minutes) → Binance Futures interval label
    tf_map = {"15": ("15m", "15m"), "60": ("1h", "1h"), "240": ("4h", "4h")}

    async def _fetch_tf(interval_minutes: str, label: str, binance_interval: str) -> tuple[str, str]:
        """Fetch one timeframe, try Bybit Proxy first then Binance.US."""
        closes = None

        # ── Try Bybit Proxy ──
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"{config.BYBIT_PROXY_URL}/klines/{symbol}?interval={interval_minutes}&limit=60"
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    klines = data.get("klines", data) if isinstance(data, dict) else data
                    if isinstance(klines, list) and len(klines) >= 50:
                        closes = [float(k[4]) for k in klines]
                        closes.reverse()  # newest-first → chronological
        except Exception:
            pass

        # ── Fallback: Binance.US Spot ──
        if closes is None:
            b_sym = _binance_us_symbol(symbol)
            if b_sym:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r = await client.get(
                            f"{BINANCE_US_BASE}/klines",
                            params={"symbol": b_sym, "interval": binance_interval, "limit": 60}
                        )
                        if r.status_code == 200:
                            klines = r.json()
                            if isinstance(klines, list) and len(klines) >= 50:
                                closes = [float(k[4]) for k in klines]  # already chronological
                                logger.debug(f"[Multi-TF][Binance Fallback] {symbol} {label}: {len(klines)} klines")
                except Exception:
                    pass

        # ── EMA calculation ──
        if closes and len(closes) >= 50:
            ema20 = _ema(closes, 20)
            ema50 = _ema(closes, 50)
            if ema20 is not None and ema50 is not None:
                if ema20 > ema50 * 1.001:
                    return label, "bullish"
                elif ema20 < ema50 * 0.999:
                    return label, "bearish"
                else:
                    return label, "neutral"

        return label, "neutral"

    try:
        results = await asyncio.gather(
            *[_fetch_tf(interval, label, binance_interval)
              for interval, (label, binance_interval) in tf_map.items()]
        )
        trends = {label: direction for label, direction in results}
        logger.info(f"[Multi-TF] {symbol}: {trends}")
        return trends
    except Exception as e:
        logger.warning(f"[Multi-TF] Failed for {symbol}: {e}")
        return None


def _ema(values: list[float], period: int) -> float | None:
    """Calculate Exponential Moving Average, return last value."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # SMA seed
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


# In-memory OI cache for computing delta between ticks
_oi_cache: dict[str, float] = {}


async def fetch_funding_and_oi(symbol: str) -> tuple[float, float]:
    """
    Fetch funding rate and open interest change.
    Primary: Bybit Proxy /tickers/{symbol}
    Fallback: OKX Public API (no auth, not geo-blocked)

    Funding rates converge across exchanges via arbitrage (±0.01%),
    so OKX rates are a valid proxy for Bybit when proxy is unavailable.
    OI change (%) tracks the same market sentiment across exchanges.

    Returns: (funding_rate, oi_change_pct)
    """
    global _oi_cache

    def _compute_oi_change(symbol: str, oi_value: float) -> float:
        """Compute OI % change vs previous reading, update cache."""
        oi_change = 0.0
        if oi_value > 0:
            prev_oi = _oi_cache.get(symbol)
            if prev_oi and prev_oi > 0:
                oi_change = (oi_value - prev_oi) / prev_oi
            _oi_cache[symbol] = oi_value
        return oi_change

    # ── Try VPS Bybit Proxy ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/tickers/{symbol}"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                funding_rate = float(data.get("fundingRate", 0.0))
                oi_value = float(data.get("openInterest", 0.0))

                if funding_rate != 0 or oi_value > 0:
                    oi_change = _compute_oi_change(symbol, oi_value)
                    logger.info(
                        f"[Funding/OI] {symbol}: funding={funding_rate:.6f} "
                        f"oi={oi_value:.0f} oi_chg={oi_change:.4f}"
                    )
                    return funding_rate, oi_change
                # Proxy returned 200 but zero data → fall through to OKX
                logger.debug(f"[Funding/OI] {symbol}: proxy returned zeros, trying OKX...")
    except Exception as e:
        logger.debug(f"[Funding/OI] Bybit Proxy failed for {symbol}: {e}")

    # ── Fallback: OKX Public API (not geo-blocked, no auth) ────────
    okx_inst = _to_okx_inst_id(symbol)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Funding rate
            funding_rate = 0.0
            r_fund = await client.get(
                f"{OKX_BASE}/public/funding-rate",
                params={"instId": okx_inst}
            )
            if r_fund.status_code == 200:
                fund_data = r_fund.json().get("data", [])
                if fund_data:
                    funding_rate = float(fund_data[0].get("fundingRate", 0.0))

            # Open Interest
            oi_change = 0.0
            r_oi = await client.get(
                f"{OKX_BASE}/public/open-interest",
                params={"instType": "SWAP", "instId": okx_inst}
            )
            if r_oi.status_code == 200:
                oi_data = r_oi.json().get("data", [])
                if oi_data:
                    oi_value = float(oi_data[0].get("oi", 0.0))
                    oi_change = _compute_oi_change(symbol, oi_value)

            if funding_rate != 0 or oi_change != 0:
                logger.info(
                    f"[Funding/OI][OKX Fallback] {symbol}: "
                    f"funding={funding_rate:.6f} oi_chg={oi_change:.4f}"
                )
                return funding_rate, oi_change

    except Exception as e:
        logger.debug(f"[Funding/OI][OKX Fallback] Failed for {symbol}: {e}")

    logger.warning(f"[Funding/OI] {symbol}: NO DATA from proxy or OKX")
    return 0.0, 0.0


async def fetch_closed_pnl(symbol: str = None, limit: int = 50) -> list[dict]:
    """
    Fetch real closed PnL records from Bybit via proxy.
    Returns list of {symbol, closedPnl, qty, entryPrice, exitPrice, ...}.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"limit": limit}
            if symbol:
                params["symbol"] = symbol
            url = f"{config.BYBIT_PROXY_URL}/account/closed-pnl"
            r = await client.get(url, params=params)
            if r.status_code == 200:
                data = r.json()
                records = data.get("records", [])
                logger.info(f"[Closed PnL] Got {len(records)} records" + (f" for {symbol}" if symbol else ""))
                return records
    except Exception as e:
        logger.warning(f"[Closed PnL] Failed: {e}")
    return []


async def fetch_wallet_balance() -> dict | None:
    """
    Fetch wallet balance (equity, available) from Bybit via proxy.
    Returns {equity, available, wallet_balance, unrealized_pnl} or None.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/account/wallet-balance"
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                if "error" not in data:
                    logger.info(f"[Wallet] equity=${data.get('equity', 0):.2f}")
                    return data
    except Exception as e:
        logger.warning(f"[Wallet] Failed: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════
# Market Intelligence Layer — Lightweight ML Feature Functions
# ══════════════════════════════════════════════════════════════════════════
# These compute ML features from data we ALREADY fetch or from
# accessible free APIs. No new infrastructure (WebSocket/TimescaleDB).
#
# When project scales to 50+ symbols and needs real-time streaming,
# migrate to: cryptofeed WebSocket → TimescaleDB hypertables → feature-engine.
# ══════════════════════════════════════════════════════════════════════════


# In-memory BTC price cache for btc_change_1h
_btc_price_cache: dict[str, float] = {}  # {"prev_close": float}


async def fetch_btc_change_1h() -> float:
    """
    Fetch BTC 1-hour price change from Binance.US.
    Critical ML feature: altcoins correlate 60-80% with BTC.

    Returns: fractional change (e.g. -0.02 = -2%)
    """
    global _btc_price_cache
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{BINANCE_US_BASE}/klines",
                params={"symbol": "BTCUSDT", "interval": "1h", "limit": 2}
            )
            if r.status_code == 200:
                klines = r.json()
                if isinstance(klines, list) and len(klines) >= 2:
                    prev_close = float(klines[0][4])
                    curr_close = float(klines[1][4])
                    if prev_close > 0:
                        change = (curr_close - prev_close) / prev_close
                        _btc_price_cache["prev_close"] = prev_close
                        return round(change, 6)
    except Exception as e:
        logger.debug(f"[BTC Change] Failed: {e}")
    return 0.0


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Compute RSI (Relative Strength Index) from price closes.
    RSI > 70 = overbought (exit signal for longs)
    RSI < 30 = oversold (exit signal for shorts)

    Uses Wilder's smoothing method (standard RSI calculation).
    Requires at least period+1 closes.

    Returns: RSI value (0-100) or None if insufficient data.
    """
    if not closes or len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]

    # Seed with SMA of first `period` gains/losses
    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder's smoothing for remaining
    for d in deltas[period:]:
        gain = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def compute_orderbook_imbalance(bids: list, asks: list) -> float:
    """
    Compute bid/ask volume imbalance from L2 orderbook data.
    > 1.0 = buy pressure dominates (bullish)
    < 1.0 = sell pressure dominates (bearish)
    = 1.0 = balanced

    Uses data from fetch_orderbook_raw() which we already call.

    Returns: imbalance ratio (bid_vol / ask_vol), default 1.0 if no data.
    """
    if not bids or not asks:
        return 1.0

    bid_vol = sum(float(b[1]) if isinstance(b, list) else b for b in bids[:20])
    ask_vol = sum(float(a[1]) if isinstance(a, list) else a for a in asks[:20])

    if ask_vol == 0:
        return 1.0
    return round(bid_vol / ask_vol, 4)


async def fetch_long_short_ratio(symbol: str) -> float:
    """
    Fetch long/short account ratio from OKX public API.
    OKX endpoint: /api/v5/rubik/stat/contracts/long-short-account-ratio
    No auth required, not geo-blocked.

    Ratio > 2.0 = crowded longs (bearish signal)
    Ratio < 0.5 = crowded shorts (bullish signal)

    Funding rates + LSR together form a powerful crowding indicator.

    Returns: long/short ratio (e.g. 2.48), default 1.0 if unavailable.
    """
    # Convert BTCUSDT → BTC (OKX uses ccy not instId for this endpoint)
    ccy = symbol.replace("USDT", "")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{OKX_BASE}/rubik/stat/contracts/long-short-account-ratio",
                params={"ccy": ccy, "period": "1H"}
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    ratio = float(data[0][1])
                    logger.debug(f"[LSR] {symbol}: ratio={ratio:.2f}")
                    return round(ratio, 4)
    except Exception as e:
        logger.debug(f"[LSR] Failed for {symbol}: {e}")

    return 1.0


async def fetch_rsi_from_klines(symbol: str, period: int = 14) -> float | None:
    """
    Convenience: fetch 1h klines and compute RSI in one call.
    Primary: Bybit Proxy → Fallback: Binance.US Spot klines.

    Returns: RSI value (0-100) or None.
    """
    closes = None

    # Try Bybit Proxy
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            url = f"{config.BYBIT_PROXY_URL}/klines/{symbol}"
            r = await client.get(url, params={"interval": "60", "limit": str(period + 10)})
            if r.status_code == 200:
                data = r.json()
                klines_data = data if isinstance(data, list) else data.get("klines", [])
                if isinstance(klines_data, list) and len(klines_data) >= period + 1:
                    closes = [float(k[4]) if isinstance(k, list) else float(k.get("close", 0)) for k in klines_data]
                    closes.reverse()  # chronological order
    except Exception:
        pass

    # Fallback: Binance.US
    if closes is None:
        b_sym = _binance_us_symbol(symbol)
        if b_sym:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(
                        f"{BINANCE_US_BASE}/klines",
                        params={"symbol": b_sym, "interval": "1h", "limit": period + 10}
                    )
                    if r.status_code == 200:
                        klines = r.json()
                        if isinstance(klines, list) and len(klines) >= period + 1:
                            closes = [float(k[4]) for k in klines]
            except Exception:
                pass

    if closes:
        return compute_rsi(closes, period)
    return None
