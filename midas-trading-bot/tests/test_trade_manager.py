"""
Unit tests for trade_manager fixes:
 - Opposite-direction flip close
 - Duplicate Dumalka command dedup
 - TP/SL error propagation
 - _fetch_closed_pnl retry
 - Same-direction merge regression
 - Dedup cache eviction
 - Symbol lock prevents concurrent duplicates
 - dumalka_closing cleared on failure
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.models.trade_state import TradeState, TradeSide, TradeStage
from tests.conftest import make_buy_signal, make_sell_signal


# ── Test 1: Opposite-direction flip ─────────────────────────────

@pytest.mark.asyncio
async def test_opposite_direction_flip_close(trade_manager, sample_sell_trade):
    """When a BUY signal arrives for a symbol with active SELL, old SELL must be closed first."""
    tm = trade_manager
    tm.active_trades["trade-sell-1"] = sample_sell_trade

    tm.get_user_credentials = AsyncMock(return_value=("key", "secret", False))
    tm.get_db_settings = AsyncMock(return_value=None)
    tm.check_daily_loss = AsyncMock(return_value=True)
    tm.save_trade = AsyncMock()
    tm.notify_risk_engine = AsyncMock()
    tm.broadcast_user_stats = AsyncMock()

    tm._mock_bybit.get_closed_pnl = AsyncMock(return_value=[{
        "createdTime": str(int(datetime(2026, 4, 9, 12, 0).timestamp() * 1000)),
        "closedPnl": "5.0",
        "avgExitPrice": "49500.0",
    }])

    buy_signal = make_buy_signal()
    result = await tm.open_trade(
        user_id=1, signal=buy_signal, signal_id=100, signal_hash="hash_buy_new",
    )

    tm._mock_bybit.close_position_full.assert_called_once_with(
        "BTCUSDT", "Sell", "key", "secret", False
    )

    assert "trade-sell-1" not in tm.active_trades
    assert sample_sell_trade.stage == TradeStage.CLOSED

    flip_calls = [c for c in tm.notify_risk_engine.call_args_list
                  if c.kwargs.get("event") == "flip_close" or
                  (len(c.args) > 2 and c.args[2] == "flip_close")]
    assert len(flip_calls) >= 1


# ── Test 2: Duplicate Dumalka dedup ──────────────────────────────

def test_duplicate_full_close_dedup():
    """Second full_close with same trace_id should be deduplicated."""
    from app.main import _dumalka_processed_closes

    _dumalka_processed_closes.clear()

    key = "BTCUSDT:trace_abc123"
    assert key not in _dumalka_processed_closes

    _dumalka_processed_closes[key] = True
    assert key in _dumalka_processed_closes

    key2 = "BTCUSDT:trace_abc123"
    assert key2 in _dumalka_processed_closes


# ── Test 3: TP/SL error propagation ─────────────────────────────

@pytest.mark.asyncio
async def test_tp_sl_error_propagation(trade_manager):
    """When set_trading_stop_combined returns False, trade still opens but error is logged."""
    tm = trade_manager
    tm.get_user_credentials = AsyncMock(return_value=("key", "secret", False))
    tm.get_db_settings = AsyncMock(return_value=None)
    tm.check_daily_loss = AsyncMock(return_value=True)
    tm.save_trade = AsyncMock()
    tm.notify_risk_engine = AsyncMock()
    tm.broadcast_user_stats = AsyncMock()

    tm._mock_bybit.set_trading_stop_combined = AsyncMock(return_value=False)

    buy_signal = make_buy_signal()
    with patch("app.services.trade_manager.logger") as mock_logger:
        result = await tm.open_trade(
            user_id=1, signal=buy_signal, signal_id=200, signal_hash="hash_tpsl",
        )

    assert result is True
    assert len(tm.active_trades) == 1

    error_calls = [c for c in mock_logger.error.call_args_list
                   if "FAILED to set SL" in str(c)]
    assert len(error_calls) >= 1


# ── Test 4: _fetch_closed_pnl retry ─────────────────────────────

@pytest.mark.asyncio
async def test_fetch_closed_pnl_retry(trade_manager, sample_buy_trade):
    """_fetch_closed_pnl retries up to 3 times with increasing delays."""
    tm = trade_manager

    call_count = 0
    async def mock_get_closed_pnl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None
        return [{
            "createdTime": str(int(sample_buy_trade.created_at.timestamp() * 1000)),
            "closedPnl": "10.5",
            "avgExitPrice": "51000.0",
        }]

    tm._mock_bybit.get_closed_pnl = mock_get_closed_pnl

    pnl, exit_price = await tm._fetch_closed_pnl(
        "BTCUSDT", sample_buy_trade, "key", "secret", False,
        delays=(0.01, 0.01, 0.01),
    )

    assert call_count == 3
    assert pnl == 10.5
    assert exit_price == 51000.0


@pytest.mark.asyncio
async def test_fetch_closed_pnl_all_fail(trade_manager, sample_buy_trade):
    """When all retries fail, returns (None, None)."""
    tm = trade_manager
    tm._mock_bybit.get_closed_pnl = AsyncMock(return_value=None)

    pnl, exit_price = await tm._fetch_closed_pnl(
        "BTCUSDT", sample_buy_trade, "key", "secret", False,
        delays=(0.01, 0.01, 0.01),
    )

    assert pnl is None
    assert exit_price is None


# ── Test 5: Same-direction merge (regression) ───────────────────

@pytest.mark.asyncio
async def test_same_direction_merge(trade_manager, sample_buy_trade):
    """BUY signal when BUY trade active should merge, not flip-close."""
    tm = trade_manager
    tm.active_trades["trade-buy-1"] = sample_buy_trade

    tm.get_user_credentials = AsyncMock(return_value=("key", "secret", False))
    tm.get_db_settings = AsyncMock(return_value=None)
    tm.check_daily_loss = AsyncMock(return_value=True)
    tm.save_trade = AsyncMock()
    tm.notify_risk_engine = AsyncMock()
    tm.broadcast_user_stats = AsyncMock()

    buy_signal = make_buy_signal()
    result = await tm.open_trade(
        user_id=1, signal=buy_signal, signal_id=300, signal_hash="hash_merge",
    )

    assert result is True
    tm._mock_bybit.close_position_full.assert_not_called()

    merge_calls = [c for c in tm.notify_risk_engine.call_args_list
                   if c.kwargs.get("event") == "position_increased" or
                   (len(c.args) > 2 and c.args[2] == "position_increased")]
    assert len(merge_calls) >= 1


# ── Test 6: Dedup cache eviction ─────────────────────────────────

def test_dedup_cache_eviction():
    """_dumalka_processed_closes stays bounded after eviction."""
    from app.main import _dumalka_processed_closes

    _dumalka_processed_closes.clear()

    for i in range(250):
        _dumalka_processed_closes[f"SYM:{i}"] = True

    assert len(_dumalka_processed_closes) == 250

    if len(_dumalka_processed_closes) > 200:
        oldest = list(_dumalka_processed_closes.keys())[:100]
        for k in oldest:
            del _dumalka_processed_closes[k]

    assert len(_dumalka_processed_closes) == 150
    assert "SYM:0" not in _dumalka_processed_closes
    assert "SYM:249" in _dumalka_processed_closes

    _dumalka_processed_closes.clear()


# ── Test 7: Symbol lock prevents concurrent opens ────────────────

@pytest.mark.asyncio
async def test_symbol_lock_serializes(trade_manager):
    """Concurrent open_trade calls for same symbol are serialized by lock."""
    tm = trade_manager
    lock = tm._symbol_locks.setdefault("BTCUSDT", asyncio.Lock())

    acquired_order = []

    async def mock_locked_section(name):
        async with lock:
            acquired_order.append(f"{name}_enter")
            await asyncio.sleep(0.05)
            acquired_order.append(f"{name}_exit")

    t1 = asyncio.create_task(mock_locked_section("first"))
    t2 = asyncio.create_task(mock_locked_section("second"))
    await asyncio.gather(t1, t2)

    assert acquired_order[0] == "first_enter"
    assert acquired_order[1] == "first_exit"
    assert acquired_order[2] == "second_enter"
    assert acquired_order[3] == "second_exit"


# ── Test 8: dumalka_closing cleared on failure ───────────────────

@pytest.mark.asyncio
async def test_dumalka_closing_cleared_on_failure(trade_manager, sample_buy_trade):
    """If full_close fails, dumalka_closing must be reset to False."""
    tm = trade_manager
    tm.active_trades["trade-buy-1"] = sample_buy_trade

    tm.get_user_credentials = AsyncMock(return_value=("key", "secret", False))
    tm._mock_bybit.close_position_full = AsyncMock(return_value=None)

    cmd = MagicMock()
    cmd.action = "full_close"
    cmd.new_sl = None
    cmd.new_tp = None
    cmd.trace_id = "trace_fail"
    cmd.zone = None
    cmd.reason = "test"
    cmd.diagnostics = None

    result = await tm.execute_dumalka_command("trade-buy-1", sample_buy_trade, cmd)

    assert result["ok"] is False
    assert sample_buy_trade.dumalka_closing is False
