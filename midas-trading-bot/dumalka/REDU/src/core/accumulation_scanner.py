"""
Accumulation Scanner — "Coiled Spring" Detector (v0.10.1)

Scans watchlist symbols for accumulation patterns:
  - Rising Open Interest (OI) = leveraged positions building
  - Volume anomaly (higher than 24h avg) = institutional activity
  - Funding rate divergence = crowd positioning (squeeze fuel)
  - Price compression = tight range despite OI growth

Runs as asyncio background task every 4 hours.
Sends Telegram digest at 00:00 and 12:00 UTC with top accumulating symbols.

Data sources: Bybit Proxy (/tickers/{sym}, /market-data/{sym})
Storage: PostgreSQL `accumulation_snapshots` table
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from config import config
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute

logger = logging.getLogger("risk-engine.accumulation")

# ── Configuration ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_HOURS = 4       # Scan every 4 hours
DIGEST_HOURS_UTC = [0, 12]    # Send digest at 00:00 and 12:00 UTC
MIN_SCORE_FOR_DIGEST = 0.30   # Minimum score to include in digest
TOP_N_DIGEST = 10             # Max symbols in digest


# ── Database Schema ───────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accumulation_snapshots (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    symbol VARCHAR(32) NOT NULL,
    price NUMERIC,
    open_interest NUMERIC,
    oi_delta_24h_pct NUMERIC,
    volume_24h NUMERIC,
    volume_ratio NUMERIC,
    funding_rate NUMERIC,
    price_range_4h_pct NUMERIC,
    accumulation_score NUMERIC,
    predicted_direction VARCHAR(10)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_accum_symbol_ts
ON accumulation_snapshots (symbol, created_at DESC);
"""


async def ensure_table():
    """Create the accumulation_snapshots table if it doesn't exist."""
    try:
        await pg_execute(CREATE_TABLE_SQL)
        await pg_execute(CREATE_INDEX_SQL)
        logger.info("accumulation_snapshots table ensured")
    except Exception as e:
        logger.error(f"Failed to create accumulation_snapshots table: {e}")


# ── Scoring Logic ─────────────────────────────────────────────────────────────

def compute_accumulation_score(
    oi_delta_24h: float,
    vol_ratio: float,
    funding_rate: float,
    price_range_pct: float,
) -> tuple[float, str]:
    """
    Compute accumulation score (0.0 - 1.0) and predicted direction.

    Returns: (score, predicted_direction)
      - score: 0.0 (no accumulation) to 1.0 (extreme accumulation)
      - predicted_direction: 'long' or 'short' based on funding pressure
    """
    score = 0.0

    # Factor 1: OI growth (max 0.35)
    # OI growth > 5% in 24h = positions are building
    if oi_delta_24h > 0.05:
        score += min(oi_delta_24h * 1.5, 0.35)

    # Factor 2: Volume anomaly (max 0.25)
    # Volume > 1.5x average = abnormal activity
    if vol_ratio > 1.5:
        score += min((vol_ratio - 1.0) * 0.25, 0.25)

    # Factor 3: Funding divergence (max 0.20)
    # Extreme funding = one side is overcrowded
    funding_abs = abs(funding_rate)
    if funding_abs > 0.0003:  # > 0.03%
        score += min(funding_abs * 500, 0.20)

    # Factor 4: Price compression (max 0.20)
    # Tight range + growing OI = pressure building (coiled spring)
    if price_range_pct < 3.0 and oi_delta_24h > 0.03:
        score += max(0.0, 0.20 - (price_range_pct * 0.06))

    score = round(min(score, 1.0), 3)

    # Predicted direction: opposite of crowd positioning
    # Positive funding = longs pay shorts = crowd is long → expect SHORT squeeze DOWN
    # Negative funding = shorts pay longs = crowd is short → expect LONG squeeze UP
    if funding_rate > 0.0001:
        direction = "short"  # crowd is long, expect reversal down
    elif funding_rate < -0.0001:
        direction = "long"   # crowd is short, expect squeeze up
    else:
        # Neutral funding: use OI momentum direction
        direction = "long" if oi_delta_24h > 0 else "short"

    return score, direction


# ── Data Fetching ─────────────────────────────────────────────────────────────

# In-memory OI cache for 24h delta computation
_oi_history: dict[str, list[tuple[float, float]]] = {}  # symbol → [(timestamp, oi), ...]
MAX_OI_HISTORY = 7  # Keep ~24h at 4h intervals


async def fetch_symbol_data(symbol: str) -> Optional[dict]:
    """
    Fetch all required data for a single symbol from Bybit Proxy.
    Returns dict with all metrics or None on failure.
    """
    result = {}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch ticker data (OI + funding)
            ticker_resp = await client.get(f"{config.BYBIT_PROXY_URL}/tickers/{symbol}")
            if ticker_resp.status_code == 200:
                ticker = ticker_resp.json()
                result["open_interest"] = float(ticker.get("openInterest", 0))
                result["funding_rate"] = float(ticker.get("fundingRate", 0))
                result["volume_24h"] = float(ticker.get("volume24h", 0))
                result["price"] = float(ticker.get("lastPrice", 0))

            # Fetch market data (volume ratio + klines for price range)
            market_resp = await client.get(f"{config.BYBIT_PROXY_URL}/market-data/{symbol}")
            if market_resp.status_code == 200:
                market = market_resp.json()
                result["volume_ratio"] = float(market.get("volume_ratio", 1.0))
                if result.get("price", 0) <= 0:
                    result["price"] = float(market.get("price", 0))

                # Compute 4h price range from klines
                klines = market.get("klines", [])
                if klines and len(klines) >= 4:
                    # Take last 4 hourly candles
                    recent = klines[:4]
                    highs = [float(k[2]) for k in recent]
                    lows = [float(k[3]) for k in recent]
                    if highs and lows and min(lows) > 0:
                        range_pct = ((max(highs) - min(lows)) / min(lows)) * 100
                        result["price_range_4h_pct"] = round(range_pct, 2)

    except Exception as e:
        logger.warning(f"[Accumulation] Failed to fetch data for {symbol}: {e}")
        return None

    if result.get("price", 0) <= 0:
        return None

    # Defaults
    result.setdefault("volume_ratio", 1.0)
    result.setdefault("price_range_4h_pct", 5.0)
    result.setdefault("funding_rate", 0.0)
    result.setdefault("open_interest", 0.0)
    result.setdefault("volume_24h", 0.0)
    result["symbol"] = symbol

    return result


def compute_oi_delta(symbol: str, current_oi: float) -> float:
    """Compute OI change over ~24h using in-memory history."""
    now = time.time()

    if symbol not in _oi_history:
        _oi_history[symbol] = []

    history = _oi_history[symbol]
    history.append((now, current_oi))

    # Trim to MAX_OI_HISTORY entries
    if len(history) > MAX_OI_HISTORY:
        _oi_history[symbol] = history[-MAX_OI_HISTORY:]

    # Find oldest entry (should be ~24h ago if we've been running)
    if len(history) >= 2 and history[0][1] > 0:
        oldest_oi = history[0][1]
        return (current_oi - oldest_oi) / oldest_oi
    
    # Fallback: try DB for 24h-ago snapshot
    return 0.0


async def get_oi_delta_from_db(symbol: str, current_oi: float) -> float:
    """Get OI delta from DB snapshot 24h ago."""
    try:
        row = await pg_fetch_one(
            "SELECT open_interest FROM accumulation_snapshots "
            "WHERE symbol = %s AND created_at < NOW() - INTERVAL '20 hours' "
            "ORDER BY created_at DESC LIMIT 1",
            (symbol,)
        )
        if row and row.get("open_interest") and float(row["open_interest"]) > 0:
            old_oi = float(row["open_interest"])
            return (current_oi - old_oi) / old_oi
    except Exception as e:
        logger.debug(f"OI delta DB lookup failed for {symbol}: {e}")
    return 0.0


# ── Main Scan Logic ───────────────────────────────────────────────────────────

async def scan_all_symbols() -> list[dict]:
    """
    Scan all watchlist symbols, compute accumulation scores, save to DB.
    Returns sorted list of results (highest score first).
    """
    results = []
    t0 = time.time()

    for symbol in config.WATCHLIST_SYMBOLS:
        try:
            data = await fetch_symbol_data(symbol)
            if not data:
                continue

            # Compute OI delta
            oi_delta = compute_oi_delta(symbol, data["open_interest"])
            if oi_delta == 0.0 and data["open_interest"] > 0:
                oi_delta = await get_oi_delta_from_db(symbol, data["open_interest"])

            data["oi_delta_24h_pct"] = oi_delta

            # Compute accumulation score
            score, direction = compute_accumulation_score(
                oi_delta_24h=oi_delta,
                vol_ratio=data["volume_ratio"],
                funding_rate=data["funding_rate"],
                price_range_pct=data["price_range_4h_pct"],
            )
            data["accumulation_score"] = score
            data["predicted_direction"] = direction

            # Save to DB
            await pg_execute("""
                INSERT INTO accumulation_snapshots (
                    symbol, price, open_interest, oi_delta_24h_pct,
                    volume_24h, volume_ratio, funding_rate,
                    price_range_4h_pct, accumulation_score, predicted_direction
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                symbol, data["price"], data["open_interest"], oi_delta,
                data["volume_24h"], data["volume_ratio"], data["funding_rate"],
                data["price_range_4h_pct"], score, direction,
            ))

            results.append(data)

        except Exception as e:
            logger.warning(f"[Accumulation] Error scanning {symbol}: {e}")

    # Sort by score descending
    results.sort(key=lambda x: x["accumulation_score"], reverse=True)

    elapsed = time.time() - t0
    logger.info(
        f"[Accumulation] Scan complete: {len(results)} symbols, "
        f"{elapsed:.1f}s, top={results[0]['symbol'] if results else 'none'} "
        f"(score={results[0]['accumulation_score']:.3f})" if results else ""
    )

    return results


# ── Telegram Digest ───────────────────────────────────────────────────────────

async def build_and_send_digest(results: list[dict]):
    """Build and send the accumulation radar digest to Telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    now_utc = datetime.now(timezone.utc)
    timestamp = now_utc.strftime("%d %b, %H:%M UTC")

    # Filter and take top N
    top = [r for r in results if r["accumulation_score"] >= MIN_SCORE_FOR_DIGEST][:TOP_N_DIGEST]

    if not top:
        logger.info("[Accumulation] No symbols above threshold for digest")
        return

    # Group by score tier
    high = [r for r in top if r["accumulation_score"] >= 0.65]
    moderate = [r for r in top if 0.30 <= r["accumulation_score"] < 0.65]

    msg = f"📊 *ACCUMULATION RADAR* ({timestamp})\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    def _format_entry(idx: int, r: dict) -> str:
        direction = r["predicted_direction"].upper()
        oi_pct = r["oi_delta_24h_pct"] * 100
        vol_r = r["volume_ratio"]
        fund = r["funding_rate"] * 100

        crowd = ""
        if r["funding_rate"] > 0.0001:
            crowd = " (crowd is long)"
        elif r["funding_rate"] < -0.0001:
            crowd = " (crowd is short)"

        return (
            f"{idx}. *{r['symbol']}* — Score: {r['accumulation_score']:.2f}\n"
            f"   OI: {oi_pct:+.0f}% | Vol: {vol_r:.1f}x | Fund: {fund:+.3f}%\n"
            f"   ⚡ Direction: {direction}{crowd}\n"
        )

    if high:
        msg += "\n🔴 *HIGH ACCUMULATION:*\n"
        for i, r in enumerate(high, 1):
            msg += _format_entry(i, r)

    if moderate:
        msg += "\n🟡 *MODERATE:*\n"
        start = len(high) + 1
        for i, r in enumerate(moderate, start):
            msg += _format_entry(i, r)

    next_scan = now_utc + timedelta(hours=SCAN_INTERVAL_HOURS)
    msg += (
        f"\n📈 Symbols scanned: {len(results)}\n"
        f"⏱ Next scan: {next_scan.strftime('%H:%M UTC')}"
    )

    # Send to Telegram
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            })
            if resp.status_code == 200:
                logger.info(f"[Accumulation] Digest sent: {len(top)} symbols")
            else:
                logger.warning(f"[Accumulation] Telegram error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[Accumulation] Failed to send digest: {e}")


# ── API Data for Widget ───────────────────────────────────────────────────────

async def get_latest_scan_data() -> list[dict]:
    """Get the most recent scan data for the analytics widget."""
    rows = await pg_fetch_all("""
        SELECT DISTINCT ON (symbol)
            symbol, price, open_interest, oi_delta_24h_pct,
            volume_24h, volume_ratio, funding_rate,
            price_range_4h_pct, accumulation_score, predicted_direction,
            created_at
        FROM accumulation_snapshots
        ORDER BY symbol, created_at DESC
    """)
    # Sort by score
    rows.sort(key=lambda x: float(x.get("accumulation_score", 0)), reverse=True)
    return rows


# ── Background Loop ───────────────────────────────────────────────────────────

_heartbeat: dict[str, float] = {}


async def accumulation_loop():
    """
    Background task: scan every 4 hours, send digest at 00:00 and 12:00 UTC.
    """
    await asyncio.sleep(30)  # Wait for DB init
    await ensure_table()

    logger.info(
        f"[Accumulation] Scanner started | "
        f"{len(config.WATCHLIST_SYMBOLS)} symbols | "
        f"scan every {SCAN_INTERVAL_HOURS}h | "
        f"digest at {DIGEST_HOURS_UTC} UTC"
    )

    last_digest_hour = -1

    while True:
        try:
            _heartbeat["last_run"] = time.time()

            # Run scan
            results = await scan_all_symbols()

            # Check if it's digest time (00:00 or 12:00 UTC)
            now_utc = datetime.now(timezone.utc)
            current_hour = now_utc.hour

            if current_hour in DIGEST_HOURS_UTC and current_hour != last_digest_hour:
                last_digest_hour = current_hour
                await build_and_send_digest(results)

            _heartbeat["status"] = "healthy"
            _heartbeat["symbols_scanned"] = len(results)
            if results:
                _heartbeat["top_symbol"] = results[0]["symbol"]
                _heartbeat["top_score"] = results[0]["accumulation_score"]

        except asyncio.CancelledError:
            logger.info("[Accumulation] Scanner shutting down")
            break
        except Exception as e:
            logger.error(f"[Accumulation] Loop error: {e}")
            _heartbeat["status"] = "error"

        await asyncio.sleep(SCAN_INTERVAL_HOURS * 3600)


def get_heartbeat() -> dict:
    """Return heartbeat data for /health endpoint."""
    return _heartbeat.copy()
