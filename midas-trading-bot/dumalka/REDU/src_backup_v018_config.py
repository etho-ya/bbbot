"""
Configuration Module — Risk Engine
====================================

Central configuration for all Risk Engine subsystems.
All values are environment-variable-driven with sensible defaults.

Updated: 2026-04-02 (v0.18.3)

Configuration Groups
--------------------
  - **Core**: Database, equity, Monte Carlo scenario count.
  - **Market Data**: Bybit Proxy URL, volatility fallback, lookback.
  - **Risk Limits**: Max exposure, Kelly fraction, slippage thresholds.
  - **Notifications**: Telegram Bot API credentials.
  - **Trading Bot Integration**: Callback URLs, Думалка command channel,
    DUMALKA_BOT_TOKEN (validated at startup since v0.15.9).
  - **Phase 1 Filters**: Symbol blacklist, unprofitable hours, SL cap.
  - **Regime Detector**: Enable flag, cache TTL.
  - **Watchlist Scanner**: Symbols, dump/pump thresholds, scan interval.
  - **Compound Growth**: Dynamic sizing parameters (shadow mode by default).

Environment Variables
---------------------
All settings can be overridden via environment variables.
See each attribute's ``os.getenv()`` call for the variable name and default.

Usage
-----
    >>> from config import config
    >>> config.SCENARIOS  # 100000
    >>> config.DUMALKA_ACTIVE_MODE  # True
"""
import os


class Config:
    """
    Singleton configuration object. Instantiated once at module level as ``config``.

    All attributes are loaded from environment variables at import time.
    To change a setting at runtime, modify the ``config`` instance directly
    (useful for A/B testing or hot-reconfiguration via API).
    """
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # REQUIRED: set via env
    DB_PATH = os.getenv("DB_PATH", "data/signals.db")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://riskengine:riskengine123@127.0.0.1:5432/riskengine_db")

    # Defaults for Risk Engine if not specified
    DEFAULT_EQUITY = float(os.getenv("DEFAULT_EQUITY", "10000.0"))
    SCENARIOS = int(os.getenv("N_SCENARIOS", "100000"))

    # Bybit parameters (via VPS proxy over Tailscale)
    BYBIT_PROXY_URL = os.getenv("BYBIT_PROXY_URL", "http://100.117.168.63:8002")
    BYBIT_API_URL = os.getenv("BYBIT_API_URL", "https://api.binance.us")  # fallback
    VOL_FALLBACK = float(os.getenv("VOL_FALLBACK", "0.8"))
    VOL_LOOKBACK_HOURS = int(os.getenv("VOL_LOOKBACK_HOURS", "168")) # 1 week of 1h candles

    # Advanced Risk Limits
    MAX_EXPOSURE_PER_SYMBOL = float(os.getenv("MAX_EXPOSURE_PER_SYMBOL", "0.20")) # 20% of equity
    KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.5")) # Half-Kelly

    # Liquidity / Slippage (v0.6.0)
    SLIPPAGE_REJECT_RATIO = float(os.getenv("SLIPPAGE_REJECT_RATIO", "0.5"))  # reject if slippage > 50% of SL distance
    SPREAD_PENALTY_THRESHOLD = float(os.getenv("SPREAD_PENALTY_THRESHOLD", "0.0015"))  # 0.15% spread triggers penalty

    # Portfolio Allocator (v0.6.0)
    MAX_SECTOR_EXPOSURE = float(os.getenv("MAX_SECTOR_EXPOSURE", "0.50"))  # max 50% of equity in one sector

    # Telegram Notifications
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # REQUIRED: set via env
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # REQUIRED: set via env

    # Trading Bot Callback (approval flow)
    BOT_CALLBACK_URL = os.getenv("BOT_CALLBACK_URL", "http://100.117.168.63:8001/api/re/callback")
    CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "")  # REQUIRED: set via env

    # Position Tracker (Думалка v0.14+)
    PORTFOLIO_VAR_LIMIT = float(os.getenv("PORTFOLIO_VAR_LIMIT", "0.10"))  # 10% of equity max VaR

    # v0.18.1: E_pnl Reform — "Let Winners Run" (2026-04-02)
    E_PNL_EXIT_THRESHOLD = float(os.getenv("E_PNL_EXIT_THRESHOLD", "-1.0"))
    E_PNL_SKEW_OVERRIDE = float(os.getenv("E_PNL_SKEW_OVERRIDE", "2.0"))
    MC_LAMBDA_JUMP = float(os.getenv("MC_LAMBDA_JUMP", "2.0"))
    MC_MU_JUMP = float(os.getenv("MC_MU_JUMP", "-0.05"))
    MC_SIGMA_JUMP = float(os.getenv("MC_SIGMA_JUMP", "0.10"))

    # v0.18.2: Patience Protocol (2026-04-02)
    # Apollo Bail (full_close on stale 1-4h positions) disabled by default.
    # 3/3 exits (31 Mar–1 Apr) were net-negative after commissions.
    # Step 2 (SL→soft BE) remains active. Re-enable: APOLLO_BAIL_ENABLED=true
    APOLLO_BAIL_ENABLED = os.getenv("APOLLO_BAIL_ENABLED", "false").lower() == "true"
    # Grace period before active gates engage (was 0.5h hardcoded, now 1.0h).
    # Only Hard SL Cap + exchange SL/TP active during grace period.
    YOUNG_POSITION_HOURS = float(os.getenv("YOUNG_POSITION_HOURS", "1.0"))

    # v0.18.3 (2026-04-02): DB/bot desync — auto-close when symbol missing from bot roster
    # Consecutive successful /dumalka/position syncs without this symbol (30s cycle → 20 ≈ 10 min).
    _bac_raw = os.getenv("BOT_ABSENT_CLOSE_CYCLES", "20").strip()
    try:
        _bac_parsed = int(_bac_raw) if _bac_raw else 20
    except ValueError:
        _bac_parsed = 20
    # Clamp: min 1 cycle; max ~34 days at 30s/cycle (typo / abuse guard)
    BOT_ABSENT_CLOSE_CYCLES = max(1, min(100_000, _bac_parsed))
    # Optional: POST /api/admin/force-close-position — if empty, endpoint returns 503
    FORCE_CLOSE_SECRET = os.getenv("FORCE_CLOSE_SECRET", "").strip()

    # Думалка → Trading Bot command channel
    DUMALKA_BOT_URL = os.getenv("DUMALKA_BOT_URL", "http://100.117.168.63:8001")  # VPS bot via Tailscale
    DUMALKA_BOT_URL_FALLBACK = os.getenv("DUMALKA_BOT_URL_FALLBACK", "http://155.212.147.221:8001")  # Public IP fallback
    DUMALKA_BOT_TOKEN = os.getenv("DUMALKA_BOT_TOKEN", "")  # REQUIRED: set via env
    DUMALKA_ACTIVE_MODE = os.getenv("DUMALKA_ACTIVE_MODE", "true").lower() == "true"  # send commands to bot

    # v0.17.0: Same-coin re-entry (close old + open new on fresh signal)
    REENTRY_ENABLED = os.getenv("REENTRY_ENABLED", "true").lower() == "true"
    REENTRY_COOLDOWN_HOURS = float(os.getenv("REENTRY_COOLDOWN_HOURS", "2.0"))

    # Phase 1 — Rule-Based Filters (data-driven, from /api/analytics 2026-03-18)
    # Symbol Blacklist: symbols with <15% win rate from symbol_quality analytics
    _bl_raw = os.getenv("SYMBOL_BLACKLIST", "").strip()
    SYMBOL_BLACKLIST = set(s.strip() for s in _bl_raw.split(",") if s.strip()) if _bl_raw else set()
    # Unprofitable hours UTC: hours with avg PnL < -1% from time_of_day analytics
    _hours_raw = os.getenv("UNPROFITABLE_HOURS_UTC", "").strip()
    UNPROFITABLE_HOURS_UTC = set(int(h) for h in _hours_raw.split(",") if h.strip()) if _hours_raw else set()
    # Hard SL Cap: max loss % per position before force-close (SL optimization: breakeven at 2.066%)
    MAX_LOSS_PCT = float(os.getenv("MAX_LOSS_PCT", "5.0"))
    # SHADOW MODE: log Phase 1 decisions but do NOT intervene in trading
    # Set PHASE1_ACTIVE_MODE=true ONLY when ready to let RE influence trades
    PHASE1_SHADOW_MODE = os.getenv("PHASE1_SHADOW_MODE", "true").lower() == "true"
    PHASE1_ACTIVE_MODE = os.getenv("PHASE1_ACTIVE_MODE", "false").lower() == "true"

    # Regime Detector (v0.7.0)
    REGIME_DETECTOR_ENABLED = os.getenv("REGIME_DETECTOR_ENABLED", "true").lower() == "true"
    REGIME_CACHE_SECONDS = int(os.getenv("REGIME_CACHE_SECONDS", "900"))  # 15 min

    # Watchlist Scanner — Dump/Pump Detector (v0.8.2)
    WATCHLIST_SCANNER_ENABLED = os.getenv("WATCHLIST_SCANNER_ENABLED", "true").lower() == "true"
    WATCHLIST_SYMBOLS = [s.strip() for s in os.getenv("WATCHLIST_SYMBOLS",
        "WIFUSDT,PEPEUSDT,FARTCOINUSDT,RENDERUSDT,GRASSUSDT,SYRUPUSDT,AVAAIUSDT,SAHARAUSDT"
    ).split(",") if s.strip()]
    DUMP_THRESHOLD_PCT = float(os.getenv("DUMP_THRESHOLD_PCT", "2.5"))
    PUMP_THRESHOLD_PCT = float(os.getenv("PUMP_THRESHOLD_PCT", "2.5"))
    SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "300"))  # 5 min

    # Compound Growth Engine (v0.11.0 — REDU-PATCH)
    COMPOUND_GROWTH_ENABLED = os.getenv("COMPOUND_GROWTH_ENABLED", "false").lower() == "true"  # Shadow mode by default
    COMPOUND_RISK_PCT = float(os.getenv("COMPOUND_RISK_PCT", "0.02"))          # 2% per trade
    COMPOUND_MAX_POSITION_PCT = float(os.getenv("COMPOUND_MAX_POSITION_PCT", "0.05"))  # 5% hard cap
    COMPOUND_DD_THRESHOLD = float(os.getenv("COMPOUND_DD_THRESHOLD", "0.10"))  # 10% DD → protective mode

config = Config()

# ── Startup validation ───────────────────────────────────────────────
import logging as _logging
_cfg_logger = _logging.getLogger("config")

if not config.DUMALKA_BOT_TOKEN:
    _cfg_logger.warning(
        "DUMALKA_BOT_TOKEN is empty! All requests to the trading bot's "
        "/dumalka/* endpoints will be rejected with 401. "
        "Set DUMALKA_BOT_TOKEN env var to the bot's DUMALKA_TOKEN value."
    )
