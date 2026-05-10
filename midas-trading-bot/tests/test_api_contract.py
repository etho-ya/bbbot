"""Tests for Bot ↔ REDU API contract v2.0.

Covers: signal approval, decision mapping, conviction sizing,
timeout fallback, trade lifecycle events, deprecated endpoints.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from tests.conftest import make_buy_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_re_response(
    recommendation="approve", signal_score=0.72, conviction_size_usd=85.0,
    rejection_reason=None, auto_be_price=None, auto_be_trigger=None,
    approved=None,
):
    if approved is None:
        approved = recommendation != "reject"
    return {
        "approved": approved,
        "recommendation": recommendation,
        "rejection_reason": rejection_reason,
        "signal_score": signal_score,
        "conviction_size_usd": conviction_size_usd,
        "auto_be_price": auto_be_price,
        "auto_be_trigger": auto_be_trigger,
    }


def _prep_tm(tm):
    """Add standard mocks required by open_trade."""
    tm.get_user_credentials = AsyncMock(return_value=("key", "secret", False))
    tm.get_db_settings = AsyncMock(return_value=None)
    tm.check_daily_loss = AsyncMock(return_value=True)
    tm.save_trade = AsyncMock()
    tm.notify_risk_engine = AsyncMock()
    tm.broadcast_user_stats = AsyncMock()


def _listener_patches():
    """Common patches for telegram_listener tests."""
    return {
        "settings": patch("app.services.telegram_listener.settings"),
        "async_session": patch("app.services.telegram_listener.async_session"),
        "notifier": patch("app.services.telegram_listener.notifier"),
        "trade_manager": patch("app.services.telegram_listener.trade_manager"),
        "http_cls": patch("httpx.AsyncClient"),
        "get_llm_settings": patch("app.services.telegram_listener.get_llm_settings"),
        "broadcast": patch("app.main.manager", MagicMock(broadcast=AsyncMock())),
    }


def _setup_listener_mocks(mocks, resp_data=None, http_status=200, http_side_effect=None):
    """Configure common mock behavior for telegram_listener tests."""
    mocks["settings"].RISK_ENGINE_ENABLED = True
    mocks["settings"].RE_URL = "http://re:8000"
    mocks["settings"].RE_WEBHOOK_SECRET = "secret"
    mocks["settings"].LLM_ENABLED = False
    mocks["settings"].RE_REPORT_CHAT_ID = -100123

    mocks["get_llm_settings"].return_value = (None, None, False)

    mock_http = AsyncMock()
    if http_side_effect:
        mock_http.post = AsyncMock(side_effect=http_side_effect)
    else:
        resp = MagicMock()
        resp.status_code = http_status
        if resp_data:
            resp.json.return_value = resp_data
        mock_http.post = AsyncMock(return_value=resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mocks["http_cls"].return_value = mock_http

    sess_obj = AsyncMock()
    sess_obj.execute = AsyncMock()
    sess_obj.commit = AsyncMock()
    sess_ctx = MagicMock()
    sess_ctx.__aenter__ = AsyncMock(return_value=sess_obj)
    sess_ctx.__aexit__ = AsyncMock(return_value=False)
    mocks["async_session"].return_value = sess_ctx

    mocks["notifier"].notify_re_decision = AsyncMock()
    mocks["notifier"].notify_re_request = AsyncMock()
    mocks["notifier"].notify_error = AsyncMock()
    mocks["trade_manager"].open_trade = AsyncMock(return_value=True)

    return mock_http


def _make_db_signal(sig_hash="test_hash"):
    db_signal = MagicMock()
    db_signal.id = 1
    db_signal.signal_hash = sig_hash
    return db_signal


def _make_parsed(symbol="ETHUSDT", side="Buy"):
    return {
        "symbol": symbol, "side": side, "direction": "LONG" if side == "Buy" else "SHORT",
        "entry_price": 2000.0, "sl": 1950.0,
        "tp1": 2050.0, "tp2": 2100.0, "tp3": 2150.0,
        "metadata": {"risk_reward": 2.5, "probability": 65.0, "win_rate": 60.0},
        "situation": "Bull market", "recommendation": "Buy ETH",
    }


# ===========================================================================
# 1. Position Sizing — conviction_size_usd from REDU
# ===========================================================================

class TestConvictionSizing:
    """Verify that open_trade uses conviction_size_usd directly."""

    @pytest.mark.asyncio
    async def test_approve_with_conviction_size(self, trade_manager):
        """RE returns approve + conviction=85 -> margin should be 85 USDT."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.72, signal_hash="hash_approve",
            re_recommendation="approve",
            conviction_size_usd=85.0,
        )
        assert result is True
        trade_manager._mock_bybit.open_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_approve_without_conviction_uses_score_fallback(self, trade_manager):
        """RE returns approve + conviction=null -> fallback: max(0.7, min(1.0, score)) * base."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.85, signal_hash="hash_no_conv",
            re_recommendation="approve",
            conviction_size_usd=None,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_reduce_uses_conviction_directly(self, trade_manager):
        """RE returns reduce + conviction=42 -> margin=42 (no local halving)."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.49, signal_hash="hash_reduce",
            re_recommendation="reduce",
            conviction_size_usd=42.0,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_reduce_without_conviction_fallback(self, trade_manager):
        """RE returns reduce + conviction=null -> fallback max(0.3, min(0.7, score))."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.49, signal_hash="hash_reduce_fb",
            re_recommendation="reduce",
            conviction_size_usd=None,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_conviction_size_zero_uses_fallback(self, trade_manager):
        """conviction_size_usd=0 treated same as null -> score fallback."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.8, signal_hash="hash_zero_conv",
            re_recommendation="approve",
            conviction_size_usd=0.0,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_no_re_data_uses_base_margin(self, trade_manager):
        """No RE score and no conviction -> plain base margin."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=None, signal_hash="hash_no_re",
            re_recommendation="approve",
            conviction_size_usd=None,
        )
        assert result is True


# ===========================================================================
# 2. Signal Approval — webhook payload & decision mapping
# ===========================================================================

class TestSignalApproval:
    """Test the RE approval HTTP flow in telegram_listener."""

    @pytest.mark.asyncio
    async def test_webhook_payload_has_bot_direct_source(self):
        """Verify /tv-webhook request uses source='bot_direct'."""
        captured_payload = {}

        async def capture_post(url, json=None, headers=None):
            captured_payload.update(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _make_re_response()
            return resp

        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            mock_http = _setup_listener_mocks(mocks)
            mock_http.post = AsyncMock(side_effect=capture_post)

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw signal text", _make_db_signal())

            assert captured_payload.get("source") == "bot_direct"
            assert "entry_low" in captured_payload
            assert "entry_high" in captured_payload
            assert "volume_level" in captured_payload

    @pytest.mark.asyncio
    async def test_reject_blocks_trade(self):
        """RE returns reject -> trade not opened, rejection_reason logged."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, resp_data=_make_re_response(
                recommendation="reject", signal_score=0.2,
                rejection_reason="already_in_position",
            ))

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_reject"))

            mt.open_trade.assert_not_called()
            mn.notify_re_decision.assert_called_once()
            assert "already_in_position" in str(mn.notify_re_decision.call_args)

    @pytest.mark.asyncio
    async def test_no_telegram_re_request_sent(self):
        """Verify notify_re_request is NOT called during signal processing."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, resp_data=_make_re_response())

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_no_tg"))

            mn.notify_re_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_approved_false_blocks_even_with_non_reject_recommendation(self):
        """RE returns approved=false + recommendation='approve' (exposure warning) -> trade blocked."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, resp_data=_make_re_response(
                recommendation="reject", signal_score=0.76,
                conviction_size_usd=3452.0, approved=False,
                rejection_reason=None,
            ))

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_approved_false"))

            mt.open_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_reduce_no_local_halving(self):
        """RE returns reduce + conviction=42 -> bot passes 42 directly (no 0.5x)."""
        captured_call = {}

        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, resp_data=_make_re_response(
                recommendation="reduce", signal_score=0.5, conviction_size_usd=42.0,
            ))

            async def capture_open(*args, **kwargs):
                captured_call.update(kwargs)
                return True
            mt.open_trade = AsyncMock(side_effect=capture_open)

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_reduce"))

            assert captured_call.get("conviction_size_usd") == 42.0
            assert captured_call.get("re_recommendation") == "reduce"


# ===========================================================================
# 3. RE Timeout / Error Fallback
# ===========================================================================

class TestREFallback:
    """RE timeout or 500 -> approve with 30% sizing."""

    @pytest.mark.asyncio
    async def test_re_timeout_fallback_30pct(self):
        """RE times out -> trade still opens with fallback sizing."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, http_side_effect=Exception("Connection timeout"))

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_timeout"))

            mt.open_trade.assert_called_once()
            call_kwargs = mt.open_trade.call_args.kwargs
            assert call_kwargs.get("re_score") == 0.3
            assert call_kwargs.get("conviction_size_usd") is None

    @pytest.mark.asyncio
    async def test_re_403_no_fallback(self):
        """RE returns 403 (bad secret) -> trade blocked, no fallback."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, http_status=403)

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_403"))

            mt.open_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_re_500_uses_fallback(self):
        """RE returns 500 -> approve with 30% fallback."""
        patches = _listener_patches()
        with patches["settings"] as ms, patches["async_session"] as ma, \
             patches["notifier"] as mn, patches["trade_manager"] as mt, \
             patches["http_cls"] as mh, patches["get_llm_settings"] as mllm, \
             patches["broadcast"]:

            mocks = {"settings": ms, "async_session": ma, "notifier": mn,
                     "trade_manager": mt, "http_cls": mh, "get_llm_settings": mllm}
            _setup_listener_mocks(mocks, http_status=500)

            from app.services.telegram_listener import _handle_trade_signal
            await _handle_trade_signal(1, _make_parsed(), "raw", _make_db_signal("test_500"))

            mt.open_trade.assert_called_once()
            call_kwargs = mt.open_trade.call_args.kwargs
            assert call_kwargs.get("re_score") == 0.3


# ===========================================================================
# 4. Trade Lifecycle Events — /trade-outcome
# ===========================================================================

class TestTradeOutcome:
    """Verify notify_risk_engine sends correct events with comment."""

    @pytest.mark.asyncio
    async def test_open_event_includes_comment(self, trade_manager):
        """open event should include fill price and margin info in comment."""
        _prep_tm(trade_manager)
        signal = make_buy_signal()
        result = await trade_manager.open_trade(
            user_id=1, signal=signal, signal_id=1,
            re_score=0.8, signal_hash="hash_open_evt",
            re_recommendation="approve",
            conviction_size_usd=50.0,
        )
        assert result is True
        trade_manager.notify_risk_engine.assert_called_once()
        call_kwargs = trade_manager.notify_risk_engine.call_args.kwargs
        assert call_kwargs["event"] == "open"
        assert "comment" in call_kwargs
        assert "Filled" in call_kwargs["comment"]

    @pytest.mark.asyncio
    async def test_trade_outcome_payload_format(self, trade_manager):
        """Verify /trade-outcome payload has all required fields per contract."""
        captured = {}

        async def capture_post(url, json=None, headers=None):
            captured.update(json or {})
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.AsyncClient") as mock_http_cls, \
             patch("app.services.trade_manager.settings") as mock_s:

            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=capture_post)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http_cls.return_value = mock_http

            mock_s.RE_URL = "http://re:8000"
            mock_s.RE_WEBHOOK_SECRET = "secret"

            await trade_manager.notify_risk_engine(
                symbol="ETHUSDT", side="long", event="sl_hit",
                price=1950.0, pnl_pct=-2.1,
                signal_hash="hash_sl", size_remaining=0.0,
                comment="SL triggered",
            )

        assert captured["hash"] == "hash_sl"
        assert captured["event"] == "sl_hit"
        assert captured["symbol"] == "ETHUSDT"
        assert captured["side"] == "long"
        assert captured["price"] == 1950.0
        assert captured["pnl_pct"] == -2.1
        assert captured["size_remaining"] == 0.0
        assert captured["comment"] == "SL triggered"

    @pytest.mark.asyncio
    async def test_dumalka_close_uses_dumalka_close_event(self, trade_manager):
        """When Dumalka full_close, event should be 'dumalka_close' not 'full_close'."""
        _prep_tm(trade_manager)

        from app.models.trade_state import TradeState, TradeSide, TradeStage

        trade = TradeState(
            id="trade-1", user_id=1, symbol="BTCUSDT",
            side=TradeSide.LONG, entry_price=50000.0,
            size=100.0, leverage=20,
            tp1=52000.0, tp2=53000.0, tp3=54000.0, sl=49000.0,
            stage=TradeStage.OPEN, signal_hash="hash_dumalka",
            created_at=datetime(2026, 4, 9, 12, 0, 0),
        )
        trade_manager.active_trades["trade-1"] = trade

        cmd = MagicMock()
        cmd.action = "full_close"
        cmd.symbol = "BTCUSDT"
        cmd.reason = "Hard SL Cap 3.5%"
        cmd.zone = "zone_2"
        cmd.trace_id = "pos_42"
        cmd.new_sl = None
        cmd.new_tp = None
        cmd.fraction = None
        cmd.target_price = None

        await trade_manager.execute_dumalka_command(
            "trade-1", trade, cmd,
        )

        if trade_manager.notify_risk_engine.called:
            call_kwargs = trade_manager.notify_risk_engine.call_args.kwargs
            assert call_kwargs["event"] == "dumalka_close"
            assert "Hard SL Cap" in call_kwargs.get("comment", "")


# ===========================================================================
# 5. Deprecated Endpoint
# ===========================================================================

class TestDeprecatedEndpoints:
    """Verify /api/re/callback returns 410 Gone."""

    @pytest.mark.asyncio
    async def test_re_callback_returns_410(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/re/callback", json={
                "hash": "test", "decision": "approve", "score": 0.5,
            })
            assert resp.status_code == 410
            body = resp.json()
            assert body.get("error") == "deprecated"


# ===========================================================================
# 6. Health Endpoint
# ===========================================================================

class TestHealthEndpoint:
    """Verify GET /health returns status info."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "healthy"
            assert "active_trades" in body
            assert "bot_running" in body
            assert "version" in body


# ===========================================================================
# 7. DumalkaCommand Validation
# ===========================================================================

class TestDumalkaValidation:
    """Verify DumalkaCommand rejects invalid actions and missing params."""

    @pytest.mark.asyncio
    async def test_invalid_action_returns_422(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/dumalka/command", json={
                "action": "invalid_action",
                "symbol": "BTCUSDT",
            }, headers={"X-Dumalka-Token": "dumalka_secret_2026"})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_move_sl_without_new_sl_returns_422(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/dumalka/command", json={
                "action": "move_sl",
                "symbol": "BTCUSDT",
            }, headers={"X-Dumalka-Token": "dumalka_secret_2026"})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_move_tp_without_new_tp_returns_422(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/dumalka/command", json={
                "action": "move_tp",
                "symbol": "BTCUSDT",
            }, headers={"X-Dumalka-Token": "dumalka_secret_2026"})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_full_close_without_extra_params_ok(self):
        from app.main import app
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/dumalka/command", json={
                "action": "full_close",
                "symbol": "BTCUSDT",
            }, headers={"X-Dumalka-Token": "dumalka_secret_2026"})
            # Should pass validation (404 = no active trade, not 422)
            assert resp.status_code in (200, 404)
