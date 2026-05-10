"""
Telegram Bridge — reads approval requests from @uebot_report,
forwards to Risk Engine, sends callback to Trading Bot.

v0.19.6.1 2026-04-10: Added dumalka_close, manual_close, flip_close to VALID_EVENTS gate
  and close handler. Previously these events were silently dropped at line 365
  (VALID_EVENTS filter), making the bot's close notifications invisible to REDU.
  Positions stayed open until position_tracker detected desync (5-249 min delay,
  wrong close_reason). Fix also applies to /trade-outcome HTTP endpoint in main.py.

Flow:
  1. estafetabot receives "🔔 Request for approval" in @uebot_report
  2. Parser extracts: hash, symbol, side, entry, TP1-3, SL
  3. Optional: if raw Midas signal seen earlier for same symbol — enrich with R:R, prob, WR, trend
  4. POST to Risk Engine /tv-webhook → get scoring + MC result
  5. POST callback to Trading Bot /api/re/callback with decision
  6. Send formatted report to @uebot_report
"""

import os
import re
import json
import logging
import asyncio
from db_adapter import pg_fetch_all, pg_fetch_one, pg_fetch_val, pg_execute, get_db_pool
from datetime import datetime, timezone
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import httpx

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("telegram-bridge")

# ─── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
RISK_ENGINE_URL = os.getenv("RISK_ENGINE_URL", "http://127.0.0.1:8000/tv-webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BOT_CALLBACK_URL = os.getenv("BOT_CALLBACK_URL", "http://100.117.168.63:8001/api/re/callback")
CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "")

# ─── Regex: Approval Request format ───────────────────────────────────────────
# "🔔 Request for approval"
# "hash: abc123def456"
# "Symbol: ETHUSDT"
# "Side: Buy (LONG)"
# "Entry: 1988.34"
# "TP1: 1995.00 | TP2: 2010.00 | TP3: 2025.00"
# "SL: 1975.00"

RE_TRIGGER = "Request for approval"
RE_HASH = re.compile(r"hash:\s*(\S+)", re.IGNORECASE)
RE_SYMBOL = re.compile(r"Symbol:\s*(\w+)", re.IGNORECASE)
RE_SIDE = re.compile(r"Side:\s*\w+\s*\((LONG|SHORT)\)", re.IGNORECASE)
RE_ENTRY = re.compile(r"Entry:\s*([\d.]+)", re.IGNORECASE)
RE_TP1 = re.compile(r"TP1:\s*([\d.]+)", re.IGNORECASE)
RE_TP2 = re.compile(r"TP2:\s*([\d.]+)", re.IGNORECASE)
RE_TP3 = re.compile(r"TP3:\s*([\d.]+)", re.IGNORECASE)
RE_SL = re.compile(r"SL:\s*([\d.]+)", re.IGNORECASE)

# Enriched fields (optional, from Midas metadata)
RE_RR = re.compile(r"RiskReward:\s*([\d.]+)", re.IGNORECASE)
RE_PROB = re.compile(r"Probability:\s*([\d.]+)", re.IGNORECASE)
RE_WR = re.compile(r"WinRate:\s*([\d.]+)", re.IGNORECASE)
RE_TREND = re.compile(r"Trend:\s*(\w+)", re.IGNORECASE)
RE_VOLUME = re.compile(r"Volume:\s*(\S+)", re.IGNORECASE)

# ─── Regex: Trade Event format (from Trading Bot) ─────────────────────────────
# "📊 Trade Event"
# "hash: 873c9c80efd8a9ac74d6f7466d048774"
# "symbol: RIVERUSDT"
# "event: sl_hit"
# "pnl_pct: -5.2"
# "price: 16.033"
# "side: short"

TE_TRIGGER = "Trade Event"
TE_HASH = re.compile(r"hash:\s*(\S+)", re.IGNORECASE)
TE_SYMBOL = re.compile(r"symbol:\s*(\w+)", re.IGNORECASE)
TE_EVENT = re.compile(r"event:\s*(\w+)", re.IGNORECASE)
TE_PNL = re.compile(r"pnl_pct:\s*([\-\d.]+)", re.IGNORECASE)
TE_PRICE = re.compile(r"price:\s*([\d.]+)", re.IGNORECASE)
TE_SIDE = re.compile(r"side:\s*(\w+)", re.IGNORECASE)
TE_SIZE = re.compile(r"size_remaining:\s*([\d.]+)", re.IGNORECASE)

# v0.19.6.1 (2026-04-10): added dumalka_close, manual_close, flip_close per bot v0.10.6
VALID_EVENTS = {
    'open', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'sl_hit',
    'full_close', 'timeout', 'apollo_full_exit',
    'zone_full_exit', 'e_pnl_full_exit',
    'dumalka_close', 'manual_close', 'flip_close',
}

# ─── Regex: Raw Midas signal format (for enrichment) ──────────────────────────
# "AEROUSDT.P 60M - 🟢 Strong BUY ▲"
# "-Риск к прибыли: 1 к 7.8"
# "-Вероятность: 89%"
# "-Win-Rate за последний месяц: 70.%"
# "-Тренд умеренный бычий - 67.%"

RE_MIDAS_HEADER = re.compile(r"(\w+(?:\.\w)?)\s+\d+M\s*-\s*[🟢🔴🟡⚪]\s*(Strong\s+)?(BUY|SELL)", re.IGNORECASE)
RE_MIDAS_RR = re.compile(r"Риск к прибыли:\s*1\s*к\s*([\d.]+)", re.IGNORECASE)
RE_MIDAS_PROB = re.compile(r"Вероятность:\s*([\d.]+)%", re.IGNORECASE)
RE_MIDAS_WR = re.compile(r"Win-Rate.*?:\s*([\d.]+)\.?%", re.IGNORECASE)
RE_MIDAS_TREND = re.compile(r"Тренд\s+([\w\s]+?)(?:\s*-\s*[\d.]+%|$)", re.IGNORECASE)
RE_MIDAS_ENTRY_RANGE = re.compile(r"Вход:\s*([\d.]+)-([\d.]+)", re.IGNORECASE)
RE_MIDAS_SL = re.compile(r"Стоп:\s*([\d.]+)", re.IGNORECASE)
RE_MIDAS_TRAILING = re.compile(r"Трейлинг:\s*([\d.]+)%", re.IGNORECASE)
RE_MIDAS_TARGETS = re.compile(r"Цели:\s*([\d.,\s]+)", re.IGNORECASE)
RE_MIDAS_VOLUME = re.compile(r"Объем\s+([\w\s]+?)(?:\s*⚠️|\s*-)", re.IGNORECASE)

# Setup Master recommendation text (v0.6.0)
# Multiple patterns: Telegram sends plain text (no Markdown bold markers)
RE_SETUP_MASTER_PATTERNS = [
    # Pattern 1: "Setup Master ... сводку:" followed by text (plain text)
    re.compile(r"Setup\s+Master[^:]*сводк[уе]:\s*\n(.*?)$", re.IGNORECASE | re.DOTALL),
    # Pattern 2: with Markdown bold markers (just in case)
    re.compile(r"\*\*Setup\s+Master[^*]*сводк[уе]:\*\*\s*\n(.*?)$", re.IGNORECASE | re.DOTALL),
    # Pattern 3: "Setup Master" as section header followed by content
    re.compile(r"Setup\s+Master[^\n]*:\s*\n(.*?)$", re.IGNORECASE | re.DOTALL),
    # Pattern 4: Broad fallback — anything after "Итог" / "Вывод" / "Рекомендация" section
    re.compile(r"(?:Итог|Вывод|Рекомендация|Общая оценка)[^\n]*:\s*\n(.*?)$", re.IGNORECASE | re.DOTALL),
]

# ─── Cache: raw Midas signal metadata keyed by symbol ─────────────────────────
# When a raw Midas signal is seen, we cache its metadata (R:R, prob, WR, trend)
# so when the approval request arrives for the same symbol, we can enrich the RE query.

midas_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 min TTL


def cache_midas_metadata(symbol: str, metadata: dict):
    """Store raw Midas signal metadata for later enrichment."""
    metadata["_cached_at"] = datetime.now(timezone.utc).timestamp()
    midas_cache[symbol] = metadata
    logger.info(f"Cached Midas metadata for {symbol}: R:R={metadata.get('risk_reward')}, "
                f"prob={metadata.get('probability')}, WR={metadata.get('win_rate')}, "
                f"trend={metadata.get('trend')}")


def get_cached_metadata(symbol: str) -> dict | None:
    """Retrieve cached metadata if still fresh."""
    meta = midas_cache.get(symbol)
    if not meta:
        return None
    age = datetime.now(timezone.utc).timestamp() - meta.get("_cached_at", 0)
    if age > CACHE_TTL_SECONDS:
        del midas_cache[symbol]
        return None
    return meta


# ─── Text → Numeric Converters ────────────────────────────────────────────────

def _trend_text_to_strength(raw: str) -> float:
    """Convert trend description (Russian/English) to numeric strength 0.0-1.0."""
    raw = raw.lower()
    # Strong / сильный
    if "сильн" in raw or "strong" in raw:
        return 1.0
    # Moderate / умеренный
    if "умерен" in raw or "moderate" in raw:
        return 0.67
    # Weak / слабый
    if "слаб" in raw or "weak" in raw:
        return 0.33
    # Neutral / нейтральный / sideways / flat
    if "нейтральн" in raw or "neutral" in raw or "flat" in raw or "sideways" in raw:
        return 0.0
    # If there's a percentage number in the trend string, try to extract it
    pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%?", raw)
    if pct_m:
        val = float(pct_m.group(1))
        return val / 100.0 if val > 1 else val
    # Default: moderate
    return 0.5


def _volume_text_to_level(raw: str) -> float:
    """Convert volume description to numeric level 0.0-1.0."""
    raw = raw.lower()
    if "высок" in raw or "high" in raw:
        return 0.8
    if "средн" in raw or "normal" in raw or "medium" in raw:
        return 0.5
    if "низк" in raw or "low" in raw:
        return 0.2
    # Try to extract a numeric value (e.g. "1031.2k")
    num_m = re.search(r"(\d+(?:\.\d+)?)", raw)
    if num_m:
        return 0.5  # have a number but can't interpret scale → default
    return 0.5


# ─── Parsers ───────────────────────────────────────────────────────────────────


def parse_approval_request(text: str) -> dict | None:
    """Parse the approval request format from Trading Bot."""
    if RE_TRIGGER not in text:
        return None

    hash_m = RE_HASH.search(text)
    symbol_m = RE_SYMBOL.search(text)
    side_m = RE_SIDE.search(text)
    entry_m = RE_ENTRY.search(text)
    sl_m = RE_SL.search(text)

    if not (hash_m and symbol_m and side_m and entry_m):
        logger.warning("Approval request detected but missing required fields")
        return None

    result = {
        "hash": hash_m.group(1),
        "symbol": symbol_m.group(1),
        "side": side_m.group(1).lower(),  # "long" or "short"
        "entry": float(entry_m.group(1)),
    }

    # Optional fields
    tp1_m = RE_TP1.search(text)
    tp2_m = RE_TP2.search(text)
    tp3_m = RE_TP3.search(text)
    if tp1_m:
        result["tp1"] = float(tp1_m.group(1))
    if tp2_m:
        result["tp2"] = float(tp2_m.group(1))
    if tp3_m:
        result["tp3"] = float(tp3_m.group(1))
    if sl_m:
        result["sl"] = float(sl_m.group(1))

    # Enriched Midas metadata (if bot includes them)
    rr_m = RE_RR.search(text)
    prob_m = RE_PROB.search(text)
    wr_m = RE_WR.search(text)
    trend_m = RE_TREND.search(text)
    vol_m = RE_VOLUME.search(text)
    if rr_m:
        result["risk_reward"] = float(rr_m.group(1))
    if prob_m:
        result["probability"] = float(prob_m.group(1))
    if wr_m:
        result["win_rate"] = float(wr_m.group(1))
    if trend_m:
        result["trend"] = trend_m.group(1).lower()
    if vol_m:
        result["volume"] = vol_m.group(1)

    return result


# Path for raw signal dump (for regex tuning)
MIDAS_RAW_LOG = "/tmp/midas_raw_signals.log"


def parse_raw_midas_signal(text: str) -> dict | None:
    """Parse raw Midas signal for metadata enrichment."""
    header_m = RE_MIDAS_HEADER.search(text)
    if not header_m:
        return None

    raw_symbol = header_m.group(1)
    # Normalize: "AEROUSDT.P" → "AEROUSDT"
    symbol = raw_symbol.replace(".P", "").upper()
    direction = header_m.group(3).upper()  # BUY or SELL
    side = "long" if direction == "BUY" else "short"

    # Dump raw signal text for regex tuning
    try:
        with open(MIDAS_RAW_LOG, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {symbol} {side}\n")
            f.write(text)
            f.write(f"\n{'='*60}\n")
    except Exception:
        pass

    result = {
        "symbol": symbol,
        "side": side,
    }

    rr_m = RE_MIDAS_RR.search(text)
    prob_m = RE_MIDAS_PROB.search(text)
    wr_m = RE_MIDAS_WR.search(text)
    trend_m = RE_MIDAS_TREND.search(text)
    vol_m = RE_MIDAS_VOLUME.search(text)
    trailing_m = RE_MIDAS_TRAILING.search(text)

    if rr_m:
        result["risk_reward"] = float(rr_m.group(1))
    if prob_m:
        result["probability"] = float(prob_m.group(1))
    if wr_m:
        result["win_rate"] = float(wr_m.group(1))
    if trend_m:
        raw_trend = trend_m.group(1).strip().lower()
        # Map Russian trend to English
        if "бычий" in raw_trend or "восходящ" in raw_trend:
            result["trend"] = "bullish"
        elif "медвежий" in raw_trend or "нисходящ" in raw_trend:
            result["trend"] = "bearish"
        else:
            result["trend"] = raw_trend
        # Convert trend text to numeric strength (0.0-1.0)
        result["trend_strength"] = _trend_text_to_strength(raw_trend)
    if vol_m:
        result["volume_level"] = _volume_text_to_level(vol_m.group(1).strip())
    if trailing_m:
        result["trailing_pct"] = float(trailing_m.group(1))

    # Entry range
    entry_m = RE_MIDAS_ENTRY_RANGE.search(text)
    if entry_m:
        result["entry_low"] = float(entry_m.group(1))
        result["entry_high"] = float(entry_m.group(2))

    # SL
    sl_m = RE_MIDAS_SL.search(text)
    if sl_m:
        result["stop_loss"] = float(sl_m.group(1))

    # Targets
    targets_m = RE_MIDAS_TARGETS.search(text)
    if targets_m:
        targets = [float(t.strip()) for t in targets_m.group(1).split(",") if t.strip()]
        if len(targets) >= 1:
            result["tp1"] = targets[0]
        if len(targets) >= 2:
            result["tp2"] = targets[1]
        if len(targets) >= 3:
            result["tp3"] = targets[2]

    # v0.18.6: Store raw signal text for midas_comment enrichment
    result["_raw_text"] = text

    # Setup Master text (v0.6.0) — try multiple patterns
    setup_found = False
    for pattern in RE_SETUP_MASTER_PATTERNS:
        setup_m = pattern.search(text)
        if setup_m:
            raw_setup = setup_m.group(1).strip()
            if len(raw_setup) > 10:  # sanity: at least some meaningful text
                result["setup_master_text"] = raw_setup
                logger.info(f"✅ Setup Master text captured for {symbol}: {raw_setup[:80]}...")
                setup_found = True
                break

    if not setup_found:
        logger.debug(f"Setup Master text NOT found for {symbol}. Signal length: {len(text)} chars")

    return result


def parse_trade_event(text: str) -> dict | None:
    """Parse trade event messages from Trading Bot."""
    if TE_TRIGGER not in text:
        return None

    event_m = TE_EVENT.search(text)
    symbol_m = TE_SYMBOL.search(text)

    if not (event_m and symbol_m):
        logger.warning("Trade Event detected but missing required fields (event, symbol)")
        return None

    event_type = event_m.group(1).lower()
    if event_type not in VALID_EVENTS:
        logger.warning(f"Unknown trade event type: {event_type}")
        return None

    result = {
        "event_type": event_type,
        "symbol": symbol_m.group(1).upper(),
    }

    # Optional fields
    hash_m = TE_HASH.search(text)
    pnl_m = TE_PNL.search(text)
    price_m = TE_PRICE.search(text)
    side_m = TE_SIDE.search(text)
    size_m = TE_SIZE.search(text)

    if hash_m:
        result["hash"] = hash_m.group(1)
    if pnl_m:
        result["pnl_pct"] = float(pnl_m.group(1))
    if price_m:
        result["price"] = float(price_m.group(1))
    if side_m:
        result["side"] = side_m.group(1).lower()
    if size_m:
        result["size_remaining"] = float(size_m.group(1))

    return result


# ─── RE Unavailable DB Logging ────────────────────────────────────────────────

RE_DB_PATH = os.getenv("DB_PATH", "data/signals.db")

async def _log_re_unavailable(
    signal_hash: str = None, symbol: str = "", side: str = None,
    error_type: str = "unknown", error_message: str = "",
    retry_attempted: bool = False, fallback_decision: str = "reduce",
    fallback_size_mult: float = 0.3,
):
    """Record RE unavailable event directly to SQLite for audit."""
    try:
        await pg_execute("""
            CREATE TABLE IF NOT EXISTS re_unavailable_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_at TEXT NOT NULL,
                signal_hash TEXT,
                symbol TEXT,
                side TEXT,
                error_type TEXT,
                error_message TEXT,
                retry_attempted INTEGER DEFAULT 0,
                retry_succeeded INTEGER DEFAULT 0,
                fallback_decision TEXT,
                fallback_size_mult REAL
            )
        """)
        await pg_execute("""
            INSERT INTO re_unavailable_events (
                event_at, signal_hash, symbol, side,
                error_type, error_message,
                retry_attempted, retry_succeeded,
                fallback_decision, fallback_size_mult
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal_hash, symbol, side,
            error_type, error_message,
            int(retry_attempted), 0,
            fallback_decision, fallback_size_mult,
        ))
        logger.warning(f"RE unavailable event logged to DB: {error_type} {symbol}")
    except Exception as e:
        logger.error(f"Failed to log RE unavailable event to DB: {e}")


# ─── Trade Event Processing ───────────────────────────────────────────────────

async def process_trade_event(parsed: dict):
    """Record trade event from bot into trade_outcomes (or open_positions for 'open')."""
    event_type = parsed["event_type"]
    symbol = parsed["symbol"]
    signal_hash = parsed.get("hash")
    side = parsed.get("side")
    price = parsed.get("price", 0.0)
    pnl_pct = parsed.get("pnl_pct")
    size_remaining = parsed.get("size_remaining")

    try:
        # Look up RE recommendation for this signal_hash (if available)
        re_rec = None
        re_score = None
        re_var = None
        if signal_hash:
            row = await pg_fetch_one(
                "SELECT re_recommendation, re_signal_score, var FROM signals WHERE signal_hash = ? LIMIT 1",
                (signal_hash,)
            )
            if row:
                re_rec, re_score, re_var = row['re_recommendation'], row['re_signal_score'], row['var']

        if event_type == "open":
            # Register open position for думалка tracking
            await pg_execute("""
                INSERT INTO open_positions (
                    signal_hash, opened_at, symbol, side,
                    size, original_size, entry_price,
                    current_price, status,
                    initial_signal_score, initial_recommendation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """, (
                signal_hash,
                datetime.now(timezone.utc).isoformat(),
                symbol, side or "unknown",
                size_remaining or 0, size_remaining or 0,
                price, price,
                re_score, re_rec,
            ))
            logger.info(f"📖 Registered open position: {symbol} {side} price={price} hash={signal_hash}")
        else:
            # Record trade outcome
            await pg_execute("""
                INSERT INTO trade_outcomes (
                    signal_hash, event_type, event_at,
                    symbol, side, price_at_event,
                    pnl_pct, size_remaining,
                    re_recommendation, re_signal_score, re_var
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_hash, event_type,
                datetime.now(timezone.utc).isoformat(),
                symbol, side, price,
                pnl_pct, size_remaining,
                re_rec, re_score, re_var,
            ))

            # If it's a closing event, mark open_positions as closed
            if event_type in ('sl_hit', 'tp3_hit', 'full_close', 'timeout', 'apollo_full_exit', 'zone_full_exit', 'e_pnl_full_exit', 'dumalka_close', 'manual_close', 'flip_close'):
                if signal_hash:
                    await pg_execute("""
                        UPDATE open_positions SET status = 'closed',
                            closed_at = ?, close_reason = ?, realized_pnl_pct = ?,
                            current_price = ?
                        WHERE signal_hash = ? AND status = 'open'
                    """, (
                        datetime.now(timezone.utc).isoformat(),
                        event_type, pnl_pct, price, signal_hash,
                    ))

            logger.info(f"📊 Recorded {event_type}: {symbol} pnl={pnl_pct}% hash={signal_hash}")


    except Exception as e:
        logger.error(f"Failed to process trade event: {e}", exc_info=True)


# ─── Core Logic ────────────────────────────────────────────────────────────────

async def process_approval_request(parsed: dict) -> dict | None:
    """
    Send parsed approval request to Risk Engine,
    then callback to Trading Bot with decision.
    """
    symbol = parsed["symbol"]
    side = parsed["side"]
    signal_hash = parsed["hash"]
    entry = parsed["entry"]

    # Calculate nominal size (RE expects base asset qty)
    # Use a nominal 100 USDT size since actual sizing is done by the bot
    nominal_usdt = 100.0
    qty = nominal_usdt / entry if entry > 0 else 0

    # Build payload for Risk Engine
    payload = {
        "symbol": symbol,
        "side": side,
        "size": qty,
        "source": "approval_flow",
        "signal_hash": signal_hash,  # link signal → RE assessment → trade outcome
        "stop_loss": parsed.get("sl"),
        "tp1": parsed.get("tp1"),
        "tp2": parsed.get("tp2"),
        "tp3": parsed.get("tp3"),
    }

    # 1. Use enriched fields from approval request (highest priority)
    if parsed.get("risk_reward"):
        payload["risk_reward"] = parsed["risk_reward"]
    if parsed.get("probability"):
        payload["probability"] = parsed["probability"]
    if parsed.get("win_rate"):
        payload["win_rate"] = parsed["win_rate"]
    if parsed.get("trend"):
        payload["trend"] = parsed["trend"]
    if parsed.get("volume"):
        payload["volume_level"] = parsed["volume"]

    # 2. Fallback: enrich with cached raw Midas signal metadata
    cached = get_cached_metadata(symbol)
    if cached:
        logger.info(f"Enriching {symbol} with cached Midas metadata")
        for field in ["risk_reward", "probability", "win_rate", "trend", "trend_strength", "volume_level", "setup_master_text"]:
            if not payload.get(field) and cached.get(field):
                payload[field] = cached[field]
        # v0.18.6: Pass raw Midas text as midas_comment for SR keyword boost
        if cached.get("_raw_text") and not payload.get("midas_comment"):
            payload["midas_comment"] = cached["_raw_text"]

    # 3. Last resort: calculate risk_reward from TP3/SL geometry
    if not payload.get("risk_reward") and parsed.get("tp3") and parsed.get("sl"):
        tp3_dist = abs(parsed["tp3"] - entry)
        sl_dist = abs(entry - parsed["sl"])
        if sl_dist > 0:
            payload["risk_reward"] = round(tp3_dist / sl_dist, 1)

    logger.info(f"→ RE: {symbol} {side} hash={signal_hash} entry={entry}")

    # Step 1: Query Risk Engine (with retry)
    re_result = None
    last_error = None
    error_type = "unknown"
    retry_attempted = False

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.post(
                    RISK_ENGINE_URL,
                    json=payload,
                    headers={"X-Webhook-Secret": WEBHOOK_SECRET},
                )
                if resp.status_code == 200:
                    re_result = resp.json()
                    logger.info(
                        f"← RE: {symbol} rec={re_result.get('recommendation')} "
                        f"score={re_result.get('signal_score', 0):.3f} "
                        f"VaR={re_result.get('var', 0) * 100:.2f}%"
                    )
                    break
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    error_type = "http_error"
                    logger.error(f"RE returned {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException as e:
            last_error = str(e)
            error_type = "timeout"
            if attempt == 0:
                retry_attempted = True
                logger.warning(f"RE timeout (attempt 1): {e}, retrying in 3s...")
                await asyncio.sleep(3)
                continue
            logger.error(f"RE unavailable after retry (timeout): {e}")
        except httpx.ConnectError as e:
            last_error = str(e)
            error_type = "connection"
            if attempt == 0:
                retry_attempted = True
                logger.warning(f"RE connection failed (attempt 1): {e}, retrying in 3s...")
                await asyncio.sleep(3)
                continue
            logger.error(f"RE unavailable after retry (connection): {e}")
        except Exception as e:
            last_error = str(e)
            error_type = "unknown"
            if attempt == 0:
                retry_attempted = True
                logger.warning(f"RE error (attempt 1): {e}, retrying in 3s...")
                await asyncio.sleep(3)
                continue
            logger.error(f"RE unavailable after retry: {e}")
        break  # non-timeout errors don't retry a second time from the for-loop

    # Step 2: Determine decision + score as position size multiplier
    # Per Dmitry's callback guide:
    #   score = 0.1-1.0 → position size multiplier (0.5 = 50% of configured risk)
    #   decision = "approve" | "reject"
    if re_result:
        recommendation = re_result.get("recommendation", "reject")
        signal_score = re_result.get("signal_score", 0)
        var_pct = re_result.get("var", 0) * 100
        cvar_pct = re_result.get("cvar", 0) * 100

        if recommendation == "reject":
            decision = "reject"
            size_multiplier = signal_score  # informational only
        else:
            decision = "approve"
            # Map signal_score to position size multiplier:
            # approve (0.60-1.0) → 0.7-1.0 (strong conviction = bigger position)
            # reduce  (0.35-0.60) → 0.3-0.7 (weak conviction = smaller position)
            if recommendation == "approve":
                size_multiplier = max(0.7, min(1.0, signal_score))
            else:  # reduce
                size_multiplier = max(0.3, min(0.7, signal_score))

        reason = (
            f"Score={signal_score:.2f} Size={size_multiplier:.0%} "
            f"VaR={var_pct:.1f}% CVaR={cvar_pct:.1f}% Rec={recommendation}"
        )
        if re_result.get("is_countertrend"):
            reason += " [COUNTERTREND]"
        if re_result.get("exposure_warning"):
            reason += " [EXPOSURE_LIMIT]"
    else:
        # RE unavailable — approve but with reduced size (don't fully block trading)
        decision = "approve"
        signal_score = 0
        size_multiplier = 0.3
        reason = "RE unavailable — reduced to 30%"
        logger.warning(f"RE unavailable for {symbol}, approving with 30% size")

        # Log to DB for audit
        asyncio.create_task(_log_re_unavailable(
            signal_hash=signal_hash, symbol=symbol, side=side,
            error_type=error_type, error_message=str(last_error),
            retry_attempted=retry_attempted,
            fallback_decision="reduce", fallback_size_mult=0.3,
        ))

    # Step 3: Send callback to Trading Bot
    callback_payload = {
        "hash": signal_hash,
        "decision": decision,
        "score": round(size_multiplier, 2),  # Position size multiplier for bot
        "reason": reason,
        "secret": CALLBACK_SECRET,
    }

    # v0.10.1: Forward conviction_size_usd from RE response (Score-based dynamic lot sizing)
    if re_result and re_result.get("conviction_size_usd") is not None:
        callback_payload["conviction_size_usd"] = re_result["conviction_size_usd"]

    conviction_str = f" conviction=${re_result['conviction_size_usd']:.0f}" if re_result and re_result.get("conviction_size_usd") else ""
    logger.info(f"→ Callback: {decision} hash={signal_hash} score={signal_score:.2f} size={size_multiplier:.0%}{conviction_str}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(BOT_CALLBACK_URL, json=callback_payload)
            logger.info(f"← Callback response: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Callback to bot failed: {e}")

    return re_result


async def format_and_send_report(
    bot, chat_id: str, parsed: dict, re_result: dict | None, decision: str
):
    """
    Send a formatted report to @uebot_report.

    v0.15.9 2026-03-30: added warning when midas_win_rate/probability/risk_reward
        are NULL (indicates parsing bug on bot side, score may be penalized).
    """
    symbol = parsed["symbol"]
    side = parsed["side"].upper()
    signal_hash = parsed["hash"]

    if re_result:
        score = re_result.get("signal_score", 0)
        var_pct = re_result.get("var", 0) * 100
        cvar_pct = re_result.get("cvar", 0) * 100
        rec = re_result.get("recommendation", "?")
        latency = re_result.get("latency_ms", 0)

        emoji = {"approve": "🟢", "reduce": "🟡", "reject": "🔴"}.get(rec, "❓")
        rec_emoji = {"approve": "✅", "reduce": "⚠️", "reject": "❌"}.get(rec, "❓")

        # Show actual RE recommendation (not just binary approve/reject)
        size_mult = re_result.get("signal_score", 0)
        if rec == "approve":
            decision_text = f"APPROVE ({size_mult:.0%})"
        elif rec == "reduce":
            decision_text = f"REDUCE ({size_mult:.0%})"
        else:
            decision_text = "REJECT"

        msg = (
            f"{emoji} *RE: {decision_text}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 {symbol} {side} | hash: `{signal_hash}`\n"
            f"\n"
            f"{rec_emoji} *Score:* {score:.2f} / 1.0\n"
            f"📊 *VaR (99%):* {var_pct:.2f}%\n"
            f"📊 *CVaR:* {cvar_pct:.2f}%\n"
            f"⏱ *Latency:* {latency:.0f}ms\n"
        )

        if re_result.get("is_countertrend"):
            msg += "🔄 *Countertrend signal*\n"
        if re_result.get("exposure_warning"):
            msg += "⚠️ *Exposure limit exceeded*\n"

        # Score components
        components = re_result.get("score_components", {})
        if components:
            msg += (
                f"\n*Components:*\n"
                f"  WR={components.get('wr', 0):.2f} "
                f"Prob={components.get('prob', 0):.2f} "
                f"R:R={components.get('rr', 0):.2f} "
                f"Trend={components.get('trend_align', 0):.2f} "
                f"Vol={components.get('vol_ok', 0):.2f} "
                f"Liq={components.get('liquidity_ok', 0):.2f}\n"
            )

        missing_fields = []
        if re_result.get("midas_win_rate") is None:
            missing_fields.append("WinRate")
        if re_result.get("midas_probability") is None:
            missing_fields.append("Probability")
        if re_result.get("midas_risk_reward") is None:
            missing_fields.append("R:R")
        if missing_fields:
            msg += f"⚠️ _Midas не передал: {', '.join(missing_fields)} (score снижен)_\n"
    else:
        msg = (
            f"⚠️ *RE UNAVAILABLE — APPROVED AT 30%*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 {symbol} {side} | hash: `{signal_hash}`\n"
            f"Risk Engine did not respond. Trade approved at 30% size.\n"
        )

    try:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send report to TG: {e}")


# ─── Telegram Handlers ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler — routes to approval parser or Midas cache."""
    if not update.message or not update.message.text:
        return

    text = update.message.text

    # 1. Check for approval request (from Trading Bot)
    approval = parse_approval_request(text)
    if approval:
        logger.info(f"📩 Approval request: {approval['symbol']} {approval['side']} hash={approval['hash']}")

        re_result = await process_approval_request(approval)

        decision = "approve"  # default
        if re_result:
            rec = re_result.get("recommendation", "reject")
            decision = "reject" if rec == "reject" else "approve"

        await format_and_send_report(
            context.bot, CHAT_ID, approval, re_result, decision
        )
        return

    # 2. Check for trade event (from Trading Bot)
    trade_event = parse_trade_event(text)
    if trade_event:
        logger.info(f"📊 Trade event: {trade_event['symbol']} {trade_event['event_type']} "
                     f"hash={trade_event.get('hash', 'n/a')} pnl={trade_event.get('pnl_pct', 'n/a')}%")
        await process_trade_event(trade_event)
        return

    # 3. Check for raw Midas signal (cache metadata for enrichment)
    midas = parse_raw_midas_signal(text)
    if midas:
        logger.info(f"📡 Raw Midas signal cached: {midas['symbol']} {midas['side']}")
        cache_midas_metadata(midas["symbol"], midas)
        return


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    """
    Entrypoint for the Telegram Bridge service.
    Starts a python-telegram-bot long-polling agent that listens to @uebot_report.
    Responsibilities:
    1. Parse incoming Midas signals to cache rich ML metadata (R:R, WR).
    2. Intercept Bot Approval Requests and route them to Risk Engine HTTP API.
    3. Broadcast HFT JSON signals back to the Trading Bot via HTTP callback.
    """
    logger.info("=" * 60)
    logger.info("Telegram Bridge v3.0 — Approval Flow + Trade Events")
    logger.info(f"  Bot Token: ...{BOT_TOKEN[-8:]}")
    logger.info(f"  Chat ID: {CHAT_ID}")
    logger.info(f"  Risk Engine: {RISK_ENGINE_URL}")
    logger.info(f"  Bot Callback: {BOT_CALLBACK_URL}")
    logger.info("=" * 60)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
