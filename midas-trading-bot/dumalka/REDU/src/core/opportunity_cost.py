"""
Background utility to calculate Maximum Favorable/Adverse Excursion (Opportunity Cost).
Tracks what happens to a coin after the Risk Engine closes a position.
"""
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from config import config
from db import get_pending_opportunity_costs, insert_opportunity_cost

logger = logging.getLogger("risk-engine.opportunity_cost")

async def compute_post_trade_excursions():
    """
    Fetch pending outcomes (closed > 4 hours ago) and compute MFE/MAE.
    Does not block or affect live trading.
    """
    try:
        pending = await get_pending_opportunity_costs()
        if not pending:
            return

        logger.info(f"[Opportunity Cost] Processing {len(pending)} pending trades...")

        async with httpx.AsyncClient(timeout=15.0) as client:
            for trade in pending:
                signal_hash = trade["signal_hash"]
                symbol = trade["symbol"]
                side = trade["side"]
                close_price = trade["close_price"]
                close_reason = trade["close_reason"]
                closed_at_str = trade["closed_at"]

                try:
                    closed_at_dt = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
                    start_ms = int(closed_at_dt.timestamp() * 1000)
                    end_4h_ms = start_ms + (4 * 3600 * 1000)
                    end_1h_ms = start_ms + (3600 * 1000)

                    klines = None

                    # 1. Try Binance US first to save Bybit rate limits
                    try:
                        binance_url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=5m&startTime={start_ms}&endTime={end_4h_ms}"
                        r1 = await client.get(binance_url, timeout=5.0)
                        if r1.status_code == 200:
                            b_data = r1.json()
                            if len(b_data) > 0:
                                # Binance klines: [open_time, open, high, low, close, volume, ...]
                                klines = [{"timestamp": int(k[0]), "high": float(k[2]), "low": float(k[3])} for k in b_data]
                    except Exception as e:
                        logger.debug(f"[Opportunity Cost] Binance US failed for {symbol}: {e}")

                    # 2. Try OKX public API as priority fallback (geo-ban friendly)
                    if not klines:
                        try:
                            # OKX requires dash-separated symbols (e.g., BTC-USDT)
                            if symbol.endswith("USDT"):
                                okx_symbol = symbol[:-4] + "-USDT"
                            else:
                                okx_symbol = symbol
                            
                            okx_url = f"https://www.okx.com/api/v5/market/history-candles?instId={okx_symbol}&bar=5m&after={end_4h_ms}&before={start_ms}&limit=100"
                            r_okx = await client.get(okx_url, timeout=5.0)
                            if r_okx.status_code == 200:
                                okx_data = r_okx.json().get("data", [])
                                if okx_data:
                                    # OKX returns reverse chronological: [ts, o, h, l, c, vol, ...]
                                    klines = [{"timestamp": int(k[0]), "high": float(k[2]), "low": float(k[3])} for k in okx_data]
                                    klines.reverse()  # Match chronological order
                        except Exception as e:
                            logger.debug(f"[Opportunity Cost] OKX failed for {symbol}: {e}")

                    # 3. Fallback to Bybit Proxy if OKX failed
                    if not klines:
                        bybit_url = f"{config.BYBIT_PROXY_URL}/klines/{symbol}?interval=5&start={start_ms}&end={end_4h_ms}"
                        r2 = await client.get(bybit_url, timeout=10.0)
                        if r2.status_code == 200:
                            data = r2.json()
                            raw_klines = data.get("klines", data) if isinstance(data, dict) else data
                            if isinstance(raw_klines, list) and len(raw_klines) > 0:
                                # Bybit proxy klines: [timestamp, open, high, low, close, ...]
                                klines = [{"timestamp": int(k[0]), "high": float(k[2]), "low": float(k[3])} for k in raw_klines]

                    if not klines:
                        logger.debug(f"[Opportunity Cost] No kline data from both APIs for {symbol} after {closed_at_str}. Skipping.")
                        continue

                    highs_1h = []
                    lows_1h = []
                    highs_4h = []
                    lows_4h = []

                    for k in klines:
                        t_ms = k["timestamp"]
                        high = k["high"]
                        low = k["low"]
                        
                        if t_ms <= end_4h_ms:
                            highs_4h.append(high)
                            lows_4h.append(low)
                            if t_ms <= end_1h_ms:
                                highs_1h.append(high)
                                lows_1h.append(low)

                    if not highs_4h:
                        continue
                        
                    max_high_1h = max(highs_1h) if highs_1h else close_price
                    min_low_1h = min(lows_1h) if lows_1h else close_price
                    max_high_4h = max(highs_4h)
                    min_low_4h = min(lows_4h)

                    if side == "long":
                        mfe_1h = ((max_high_1h - close_price) / close_price) * 100
                        mae_1h = ((close_price - min_low_1h) / close_price) * 100
                        mfe_4h = ((max_high_4h - close_price) / close_price) * 100
                        mae_4h = ((close_price - min_low_4h) / close_price) * 100
                    else:
                        mfe_1h = ((close_price - min_low_1h) / close_price) * 100
                        mae_1h = ((max_high_1h - close_price) / close_price) * 100
                        mfe_4h = ((close_price - min_low_4h) / close_price) * 100
                        mae_4h = ((max_high_4h - close_price) / close_price) * 100

                    mfe_1h, mae_1h = max(0.0, float(mfe_1h)), max(0.0, float(mae_1h))
                    mfe_4h, mae_4h = max(0.0, float(mfe_4h)), max(0.0, float(mae_4h))

                    await insert_opportunity_cost(
                        signal_hash=signal_hash, symbol=symbol, side=side, closed_at=closed_at_str,
                        close_price=close_price, close_reason=close_reason,
                        mfe_1h=mfe_1h, mfe_4h=mfe_4h, mae_1h=mae_1h, mae_4h=mae_4h
                    )
                    logger.info(f"[Opportunity Cost] Processed {symbol} ({close_reason}): MFE_4h={mfe_4h:.2f}%, MAE_4h={mae_4h:.2f}%")

                    # Sleep slightly to avoid spamming the APIs doing bulk historic reads
                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.error(f"[Opportunity Cost] Failed analyzing {symbol} {signal_hash}: {e}")

        logger.info("[Opportunity Cost] Finished pending updates.")
    except Exception as e:
        logger.error(f"[Opportunity Cost] Loop crashed: {e}")

if __name__ == "__main__":
    import db
    import db_adapter
    async def _test():
        await db_adapter.init_pg_pool()
        await compute_post_trade_excursions()
    asyncio.run(_test())
