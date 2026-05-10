"""
Portfolio Allocator — Risk Engine v0.6.0
Prevents hidden risk concentration from correlated assets.

Sectors:
  BTC_LIKE:   BTCUSDT and BTC-derivative tokens
  ETH_LIKE:   ETHUSDT and ETH-derivative tokens
  MAJOR_ALT:  Top-20 alts by market cap
  ALT:        Everything else (higher risk, stricter limits)
"""
import logging
from typing import Optional
from config import config

logger = logging.getLogger("risk-engine.allocator")

# ── Sector Classification ─────────────────────────────────────────────────────
MAJOR_ALTS = {
    "SOLUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "ADAUSDT",
    "MATICUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT",
    "ARBUSDT", "OPUSDT", "XRPUSDT", "DOGEUSDT", "SHIBUSDT",
    "LTCUSDT", "UNIUSDT", "AAVEUSDT", "INJUSDT", "TONUSDT",
}


def classify_sector(symbol: str) -> str:
    """Classify a trading symbol into a sector."""
    s = symbol.upper()
    if s.startswith("BTC"):
        return "BTC_LIKE"
    elif s.startswith("ETH"):
        return "ETH_LIKE"
    elif s in MAJOR_ALTS:
        return "MAJOR_ALT"
    else:
        return "ALT"


def check_portfolio_limits(
    candidate_symbol: str,
    candidate_side: str,
    candidate_size: float,
    candidate_price: float,
    equity: float,
    existing_positions: list[dict] | None = None,
) -> dict:
    """
    Check if adding a candidate trade would exceed portfolio concentration limits.

    Parameters
    ----------
    candidate_symbol : str         e.g. "BTCUSDT"
    candidate_side : str           "long" / "short"
    candidate_size : float         position size in base asset
    candidate_price : float        current price
    equity : float                 account equity in USD
    existing_positions : list      [{symbol, side, size, entry_price}, ...]

    Returns
    -------
    dict with:
      - allowed: bool
      - reason: str
      - suggested_size_mult: float  (1.0 = full, 0.5 = half, 0.0 = reject)
      - sector: str
      - sector_exposure_pct: float
    """
    if equity <= 0:
        return {"allowed": True, "reason": "no_equity_data", "suggested_size_mult": 1.0,
                "sector": "unknown", "sector_exposure_pct": 0.0}

    candidate_sector = classify_sector(candidate_symbol)

    # Calculate current exposure per sector
    sector_exposure = {"BTC_LIKE": 0.0, "ETH_LIKE": 0.0, "MAJOR_ALT": 0.0, "ALT": 0.0}
    symbol_exposure = {}

    if existing_positions:
        for pos in existing_positions:
            sym = pos.get("symbol", "")
            sz = float(pos.get("size", 0))
            ep = float(pos.get("entry_price", 0))
            exposure_usd = sz * ep
            sector = classify_sector(sym)
            sector_exposure[sector] = sector_exposure.get(sector, 0.0) + exposure_usd
            symbol_exposure[sym] = symbol_exposure.get(sym, 0.0) + exposure_usd

    # Add candidate
    candidate_usd = candidate_size * candidate_price
    new_sector_total = sector_exposure.get(candidate_sector, 0.0) + candidate_usd
    new_symbol_total = symbol_exposure.get(candidate_symbol, 0.0) + candidate_usd

    sector_pct = new_sector_total / equity
    symbol_pct = new_symbol_total / equity

    # ── Check 1: Per-symbol limit ────────────────────────────────
    if symbol_pct > config.MAX_EXPOSURE_PER_SYMBOL:
        logger.warning(
            f"[Allocator] REJECT: {candidate_symbol} symbol exposure "
            f"{symbol_pct*100:.1f}% > {config.MAX_EXPOSURE_PER_SYMBOL*100:.0f}% limit"
        )
        return {
            "allowed": False,
            "reason": f"symbol_exposure_{symbol_pct*100:.0f}pct",
            "suggested_size_mult": 0.0,
            "sector": candidate_sector,
            "sector_exposure_pct": sector_pct * 100,
        }

    # ── Check 2: Sector concentration ────────────────────────────
    if sector_pct > config.MAX_SECTOR_EXPOSURE:
        # Calculate how much we can still add
        remaining = (config.MAX_SECTOR_EXPOSURE * equity) - sector_exposure.get(candidate_sector, 0.0)
        if remaining <= 0:
            logger.warning(
                f"[Allocator] REJECT: {candidate_symbol} sector {candidate_sector} "
                f"at {sector_pct*100:.1f}% > {config.MAX_SECTOR_EXPOSURE*100:.0f}% limit"
            )
            return {
                "allowed": False,
                "reason": f"sector_{candidate_sector}_full",
                "suggested_size_mult": 0.0,
                "sector": candidate_sector,
                "sector_exposure_pct": sector_pct * 100,
            }
        else:
            suggested_mult = min(1.0, remaining / candidate_usd)
            logger.info(
                f"[Allocator] REDUCE: {candidate_symbol} sector {candidate_sector} "
                f"at {sector_pct*100:.1f}% — suggested size mult={suggested_mult:.2f}"
            )
            return {
                "allowed": True,
                "reason": f"sector_{candidate_sector}_near_limit",
                "suggested_size_mult": suggested_mult,
                "sector": candidate_sector,
                "sector_exposure_pct": sector_pct * 100,
            }

    # ── All clear ────────────────────────────────────────────────
    logger.debug(
        f"[Allocator] OK: {candidate_symbol} ({candidate_sector}) "
        f"sector={sector_pct*100:.1f}% symbol={symbol_pct*100:.1f}%"
    )
    return {
        "allowed": True,
        "reason": "ok",
        "suggested_size_mult": 1.0,
        "sector": candidate_sector,
        "sector_exposure_pct": sector_pct * 100,
    }
