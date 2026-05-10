"""Shared fixtures for trade_manager tests."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.models.trade_state import TradeState, TradeSide, TradeStage


@pytest.fixture
def trade_manager():
    """Fresh TradeManager with all external deps mocked."""
    with patch("app.services.trade_manager.bybit_client") as mock_bybit, \
         patch("app.services.trade_manager.notifier") as mock_notifier, \
         patch("app.services.trade_manager.async_session") as mock_session, \
         patch("app.services.trade_manager.bot_state") as mock_bot_state:

        mock_bot_state.is_running = True
        mock_bybit.get_wallet_balance = AsyncMock(return_value=500.0)
        mock_bybit.get_symbol_info = AsyncMock(return_value={
            "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1"},
            "priceFilter": {"tickSize": "0.0001"},
        })
        mock_bybit.open_position = AsyncMock(return_value="order-123")
        mock_bybit.close_position_full = AsyncMock(return_value="close-456")
        mock_bybit.set_trading_stop_combined = AsyncMock(return_value=True)
        mock_bybit.get_current_price = AsyncMock(return_value=100.0)
        mock_bybit.get_position_info = AsyncMock(return_value={
            "avgPrice": "100.0", "size": "10",
        })
        mock_bybit.get_closed_pnl = AsyncMock(return_value=None)
        mock_bybit.set_stop_loss = AsyncMock(return_value=True)
        mock_bybit.set_trailing_stop = AsyncMock(return_value=True)
        mock_bybit.invalidate_balance_cache = MagicMock()
        mock_bybit.is_symbol_available = AsyncMock(return_value=True)
        mock_bybit.get_max_leverage = AsyncMock(return_value=75)
        mock_bybit.set_take_profit = AsyncMock(return_value=True)
        mock_bybit._call_api = AsyncMock(return_value=True)

        mock_notifier.notify_trade_opened = AsyncMock()
        mock_notifier.notify_trade_closed = AsyncMock()
        mock_notifier.notify_tp_reached = AsyncMock()
        mock_notifier.notify_error = AsyncMock()

        session_ctx = MagicMock()
        session_obj = AsyncMock()
        session_obj.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalar=MagicMock(return_value=0.0),
        ))
        session_obj.commit = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session_obj)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = session_ctx

        from app.services.trade_manager import TradeManager
        tm = TradeManager()

        tm._mock_bybit = mock_bybit
        tm._mock_notifier = mock_notifier
        tm._mock_session = mock_session
        tm._mock_bot_state = mock_bot_state
        yield tm


@pytest.fixture
def sample_sell_trade():
    """Active SELL trade for BTCUSDT."""
    return TradeState(
        id="trade-sell-1",
        user_id=1,
        symbol="BTCUSDT",
        side=TradeSide.SHORT,
        entry_price=50000.0,
        size=100.0,
        leverage=20,
        tp1=48000.0, tp2=47000.0, tp3=46000.0,
        sl=51000.0,
        stage=TradeStage.OPEN,
        signal_hash="hash_sell_1",
        created_at=datetime(2026, 4, 9, 12, 0, 0),
    )


@pytest.fixture
def sample_buy_trade():
    """Active BUY trade for BTCUSDT."""
    return TradeState(
        id="trade-buy-1",
        user_id=1,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=50000.0,
        size=100.0,
        leverage=20,
        tp1=52000.0, tp2=53000.0, tp3=54000.0,
        sl=49000.0,
        stage=TradeStage.OPEN,
        signal_hash="hash_buy_1",
        created_at=datetime(2026, 4, 9, 10, 0, 0),
    )


def make_buy_signal():
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "direction": "LONG",
        "entry_price": 50500.0,
        "sl": 49000.0,
        "tp1": 52000.0,
        "tp2": 53000.0,
        "tp3": 54000.0,
        "trailing_stop_pct": None,
    }


def make_sell_signal():
    return {
        "symbol": "BTCUSDT",
        "side": "Sell",
        "direction": "SHORT",
        "entry_price": 50500.0,
        "sl": 51500.0,
        "tp1": 49000.0,
        "tp2": 48000.0,
        "tp3": 47000.0,
        "trailing_stop_pct": None,
    }
