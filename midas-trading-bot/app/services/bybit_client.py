import asyncio
import functools
from pybit.unified_trading import HTTP
from typing import Optional, Union
from app.core.config import settings
from app.core.logger import logger
from app.core.cache import cache


class BybitClient:
    def __init__(self):
        logger.info("Bybit: Initializing service (multi-user mode)")
        self.public_client = HTTP(testnet=settings.BYBIT_TESTNET)

    def _get_client(self, api_key: str, api_secret: str, testnet: bool = False):
        return HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )

    async def _call_api(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

    async def _call_api_with_retry(self, func, *args, max_attempts=3, **kwargs):
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._call_api(func, *args, **kwargs)
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if "rate limit" in err_str or "too many" in err_str or "502" in err_str:
                    wait_time = 2 ** attempt
                    logger.warning(f"Bybit: Rate limit (attempt {attempt}/{max_attempts}), retry in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise
        raise last_error

    async def ensure_position_mode(self, symbol: str, api_key: str, api_secret: str, testnet: bool = False):
        try:
            client = self._get_client(api_key, api_secret, testnet)
            await self._call_api(
                client.switch_position_mode,
                category="linear",
                symbol=symbol,
                mode=0,
            )
        except Exception as e:
            err_str = str(e)
            if "110025" in err_str or "not modified" in err_str.lower():
                pass
            else:
                logger.warning(f"Bybit: Could not set position mode for {symbol}: {e}")

    async def get_wallet_balance(self, api_key: str, api_secret: str, testnet: bool = False) -> float:
        cache_key = f"balance_{api_key[:8]}"
        cached_balance = cache.get(cache_key)
        if cached_balance is not None:
            return cached_balance

        try:
            client = self._get_client(api_key, api_secret, testnet)
            response = await self._call_api(
                client.get_wallet_balance,
                accountType="UNIFIED",
                coin="USDT"
            )

            if response["retCode"] != 0:
                logger.error(f"Bybit: Balance error: retCode={response['retCode']}, msg={response.get('retMsg')}")
                cache.set(cache_key, 0.0, ttl=15)
                return 0.0

            if not response.get("result", {}).get("list"):
                logger.warning("Bybit: Empty account list")
                return 0.0

            coin_list = response["result"]["list"][0].get("coin", [])
            balance = 0.0
            for coin in coin_list:
                if coin["coin"] == "USDT":
                    equity = float(coin.get("equity", "0"))
                    wallet = float(coin.get("walletBalance", "0"))
                    balance = equity if equity > 0 else wallet
                    unrealised = float(coin.get("unrealisedPnl", "0"))
                    logger.info(
                        f"Bybit: USDT equity={equity:.2f} wallet={wallet:.2f} "
                        f"unrealisedPnl={unrealised:.2f}"
                    )
                    break
            cache.set(cache_key, balance, ttl=30)
            return balance

        except Exception as e:
            logger.error(f"Bybit: Balance exception: {e}", exc_info=True)
            cache.set(cache_key, 0.0, ttl=15)
            return 0.0

    def invalidate_balance_cache(self, api_key: str):
        cache.delete(f"balance_{api_key[:8]}")

    async def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            resp = await self._call_api(
                self.public_client.get_tickers,
                category="linear", symbol=symbol
            )
            if resp["retCode"] == 0 and resp["result"]["list"]:
                return float(resp["result"]["list"][0]["lastPrice"])
        except Exception as e:
            logger.error(f"Bybit: Price error for {symbol}: {e}")
        return None

    async def is_symbol_available(self, symbol: str) -> bool:
        try:
            resp = await self._call_api(
                self.public_client.get_instruments_info,
                category="linear", symbol=symbol
            )
            if resp["retCode"] == 0 and resp["result"]["list"]:
                return resp["result"]["list"][0].get("status", "") == "Trading"
            return False
        except Exception as e:
            logger.error(f"Bybit: Symbol check error for {symbol}: {e}")
            return False

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        try:
            resp = await self._call_api(
                self.public_client.get_instruments_info,
                category="linear", symbol=symbol
            )
            if resp["retCode"] == 0 and resp["result"]["list"]:
                return resp["result"]["list"][0]
        except Exception as e:
            logger.error(f"Bybit: Symbol info error for {symbol}: {e}")
        return None

    async def get_max_leverage(self, symbol: str) -> Optional[int]:
        try:
            symbol_info = await self.get_symbol_info(symbol)
            if symbol_info:
                max_lev = symbol_info.get("leverageFilter", {}).get("maxLeverage")
                if max_lev:
                    return int(float(max_lev))
        except Exception as e:
            logger.warning(f"Bybit: Max leverage error for {symbol}: {e}")
        return None

    # ─── Open position (MARKET only) ──────────────────────────────────

    async def open_position(
        self, symbol: str, side: str, qty: Union[float, str],
        leverage: int, api_key: str, api_secret: str, testnet: bool = False,
        order_type: str = "Market", price: Optional[Union[float, str]] = None,
    ) -> Optional[str]:
        try:
            client = self._get_client(api_key, api_secret, testnet)

            await self.ensure_position_mode(symbol, api_key, api_secret, testnet)

            max_leverage = await self.get_max_leverage(symbol)
            if max_leverage and leverage > max_leverage:
                logger.warning(f"Bybit: Leverage {leverage}x > max {max_leverage}x for {symbol}")
                leverage = max_leverage

            try:
                await self._call_api(
                    client.set_leverage,
                    category="linear", symbol=symbol,
                    buyLeverage=str(leverage), sellLeverage=str(leverage),
                )
            except Exception as e:
                if "110043" not in str(e) and "not modified" not in str(e).lower():
                    logger.warning(f"Bybit: Leverage error for {symbol}: {e}")

            order_type_norm = "Market" if not order_type else str(order_type).capitalize()
            if order_type_norm not in ("Market", "Limit"):
                order_type_norm = "Market"

            logger.info(f"Bybit: {order_type_norm} {symbol} {side} qty={qty} lev={leverage}x"
                        f"{f' price={price}' if order_type_norm == 'Limit' and price is not None else ''}")

            order_kwargs = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": order_type_norm,
                "qty": str(qty),
                "isLeverage": 1,
                "positionIdx": 0,
            }
            if order_type_norm == "Limit":
                order_kwargs["price"] = str(price) if price is not None else None
                order_kwargs["timeInForce"] = "GTC"
                if order_kwargs["price"] is None:
                    logger.warning(f"Bybit: Limit order requested without price for {symbol}")
                    return None

            order = await self._call_api_with_retry(
                client.place_order,
                **order_kwargs,
            )

            if order["retCode"] != 0:
                logger.error(f"Bybit: Order rejected: {order['retCode']} {order.get('retMsg')}")
                return None

            order_id = order["result"]["orderId"]
            logger.info(f"Bybit: Order filled! ID={order_id}")
            self.invalidate_balance_cache(api_key)
            return order_id

        except Exception as e:
            logger.error(f"Bybit: Position open error: {e}")
            return None

    # ─── Native TP/SL (set_trading_stop) ──────────────────────────────

    async def set_stop_loss(self, symbol: str, stop_loss_price: Union[float, str],
                            api_key: str, api_secret: str, testnet: bool = False) -> bool:
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api_with_retry(
                client.set_trading_stop,
                category="linear",
                symbol=symbol,
                stopLoss=str(stop_loss_price),
                positionIdx=0,
            )
            if resp["retCode"] == 0:
                logger.info(f"Bybit: SL set to {stop_loss_price} for {symbol}")
                return True
            logger.error(f"Bybit: SL error: {resp['retCode']} {resp.get('retMsg')}")
            return False
        except Exception as e:
            if "not modified" in str(e).lower():
                return True
            logger.error(f"Bybit: SL exception: {e}")
            return False

    async def set_take_profit(self, symbol: str, take_profit_price: Union[float, str],
                              api_key: str, api_secret: str, testnet: bool = False) -> bool:
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api_with_retry(
                client.set_trading_stop,
                category="linear",
                symbol=symbol,
                takeProfit=str(take_profit_price),
                positionIdx=0,
            )
            if resp["retCode"] == 0:
                logger.info(f"Bybit: TP set to {take_profit_price} for {symbol}")
                return True
            logger.error(f"Bybit: TP error: {resp['retCode']} {resp.get('retMsg')}")
            return False
        except Exception as e:
            if "not modified" in str(e).lower():
                return True
            logger.error(f"Bybit: TP exception: {e}")
            return False

    async def set_trading_stop_combined(self, symbol: str,
                                        take_profit_price: Union[float, str],
                                        stop_loss_price: Union[float, str],
                                        api_key: str, api_secret: str,
                                        testnet: bool = False) -> bool:
        """Set both TP and SL in a single API call."""
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api_with_retry(
                client.set_trading_stop,
                category="linear",
                symbol=symbol,
                takeProfit=str(take_profit_price),
                stopLoss=str(stop_loss_price),
                positionIdx=0,
            )
            if resp["retCode"] == 0:
                logger.info(f"Bybit: TP={take_profit_price} SL={stop_loss_price} set for {symbol}")
                return True
            logger.error(f"Bybit: TP/SL combined error: {resp['retCode']} {resp.get('retMsg')}")
            return False
        except Exception as e:
            if "not modified" in str(e).lower():
                return True
            logger.error(f"Bybit: TP/SL combined exception: {e}")
            return False

    async def set_trailing_stop(self, symbol: str, trailing_stop_distance: Union[float, str],
                                api_key: str, api_secret: str, testnet: bool = False) -> bool:
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api_with_retry(
                client.set_trading_stop,
                category="linear",
                symbol=symbol,
                trailingStop=str(trailing_stop_distance),
                positionIdx=0,
            )
            if resp["retCode"] == 0:
                logger.info(f"Bybit: Trailing stop set to {trailing_stop_distance} for {symbol}")
                return True
            logger.error(f"Bybit: Trailing stop error: {resp['retCode']} {resp.get('retMsg')}")
            return False
        except Exception as e:
            if "not modified" in str(e).lower():
                return True
            logger.error(f"Bybit: Trailing stop exception: {e}")
            return False

    # ─── Close positions ──────────────────────────────────────────────

    async def close_position_full(self, symbol: str, side: str,
                                  api_key: str, api_secret: str, testnet: bool = False) -> Optional[str]:
        try:
            client = self._get_client(api_key, api_secret, testnet)
            close_side = "Sell" if side.lower() == "buy" else "Buy"

            pos_resp = await self._call_api(
                client.get_positions,
                category="linear",
                symbol=symbol,
            )
            if pos_resp["retCode"] != 0 or not pos_resp["result"]["list"]:
                logger.warning(f"Bybit: No position found for {symbol}")
                return None

            pos_size = None
            for pos in pos_resp["result"]["list"]:
                if float(pos.get("size", 0)) > 0:
                    pos_size = pos["size"]
                    break

            if not pos_size or float(pos_size) == 0:
                logger.warning(f"Bybit: Position size is 0 for {symbol}")
                return None

            order = await self._call_api_with_retry(
                client.place_order,
                category="linear",
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=pos_size,
                isLeverage=1,
                positionIdx=0,
                reduceOnly=True,
            )

            if order["retCode"] != 0:
                logger.error(f"Bybit: Full close error: {order['retCode']} {order.get('retMsg')}")
                return None

            order_id = order["result"]["orderId"]
            logger.info(f"Bybit: Full close {symbol} done, ID={order_id}")
            self.invalidate_balance_cache(api_key)
            return order_id
        except Exception as e:
            logger.error(f"Bybit: Full close exception: {e}")
            return None

    # ─── Position info (for real entry price) ────────────────────────

    async def get_position_info(self, symbol: str, api_key: str, api_secret: str,
                                testnet: bool = False) -> Optional[dict]:
        """Get full position dict from Bybit (avgPrice, unrealisedPnl, etc)."""
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api(
                client.get_positions,
                category="linear",
                symbol=symbol,
            )
            if resp["retCode"] == 0:
                for pos in resp["result"]["list"]:
                    if float(pos.get("size", "0")) > 0:
                        return pos
            return None
        except Exception as e:
            logger.error(f"Bybit: get_position_info exception for {symbol}: {e}")
            return None

    async def get_closed_pnl(self, symbol: str, api_key: str, api_secret: str,
                             testnet: bool = False, limit: int = 5) -> Optional[list]:
        """Fetch recent closed PnL records from Bybit. Returns list of dicts with
        avgEntryPrice, avgExitPrice, closedPnl, closedSize, etc."""
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api(
                client.get_closed_pnl,
                category="linear",
                symbol=symbol,
                limit=limit,
            )
            if resp["retCode"] == 0:
                return resp["result"].get("list", [])
            logger.warning(f"Bybit: get_closed_pnl error for {symbol}: {resp.get('retMsg')}")
            return None
        except Exception as e:
            logger.error(f"Bybit: get_closed_pnl exception for {symbol}: {e}")
            return None

    async def cancel_order(
        self, symbol: str, order_id: str,
        api_key: str, api_secret: str, testnet: bool = False,
    ) -> bool:
        try:
            client = self._get_client(api_key, api_secret, testnet)
            resp = await self._call_api(
                client.cancel_order,
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            if resp["retCode"] == 0:
                logger.info(f"Bybit: Cancelled order {symbol} ID={order_id}")
                return True
            logger.warning(f"Bybit: Cancel error {symbol} ID={order_id}: {resp.get('retMsg')}")
            return False
        except Exception as e:
            logger.warning(f"Bybit: Cancel exception {order_id}: {e}")
            return False


bybit_client = BybitClient()
