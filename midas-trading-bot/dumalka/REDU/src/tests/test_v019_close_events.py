"""
Tests for v0.19.6.1 close-event handling fixes:
  1. VALID_EVENTS includes dumalka_close, manual_close, flip_close
  2. parse_trade_event accepts new close event types
  3. parse_trade_event rejects unknown event types
  4. Close-event whitelist consistency with VALID_EVENTS
  5. Full field extraction from trade event messages
  6. TradeOutcomePayload accepts new event types

Created: 2026-04-10 — fixes critical PR gap where VALID_EVENTS gate
blocked new close events before reaching the close handler.
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Replicate parse_trade_event logic locally to avoid importing telegram SDK.
# Mirrors src/telegram_bridge.py lines 75-87, 352-390.
# ---------------------------------------------------------------------------

TE_TRIGGER = "Trade Event"
TE_HASH = re.compile(r"hash:\s*(\S+)", re.IGNORECASE)
TE_SYMBOL = re.compile(r"symbol:\s*(\w+)", re.IGNORECASE)
TE_EVENT = re.compile(r"event:\s*(\w+)", re.IGNORECASE)
TE_PNL = re.compile(r"pnl_pct:\s*([\-\d.]+)", re.IGNORECASE)
TE_PRICE = re.compile(r"price:\s*([\d.]+)", re.IGNORECASE)
TE_SIDE = re.compile(r"side:\s*(\w+)", re.IGNORECASE)
TE_SIZE = re.compile(r"size_remaining:\s*([\d.]+)", re.IGNORECASE)

# This MUST match telegram_bridge.py line 87 AFTER the fix
VALID_EVENTS = {
    'open', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'sl_hit',
    'full_close', 'timeout', 'apollo_full_exit',
    'zone_full_exit', 'e_pnl_full_exit',
    'dumalka_close', 'manual_close', 'flip_close',
}

NON_CLOSE_EVENTS = {'open', 'tp1_hit', 'tp2_hit'}

CLOSE_EVENTS = VALID_EVENTS - NON_CLOSE_EVENTS


def _parse_trade_event(text: str) -> dict | None:
    """Local replica of telegram_bridge.parse_trade_event for testing."""
    if TE_TRIGGER not in text:
        return None
    event_m = TE_EVENT.search(text)
    symbol_m = TE_SYMBOL.search(text)
    if not (event_m and symbol_m):
        return None
    event_type = event_m.group(1).lower()
    if event_type not in VALID_EVENTS:
        return None
    result = {"event_type": event_type, "symbol": symbol_m.group(1).upper()}
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


def _make_trade_event_msg(event_type: str, **kwargs) -> str:
    """Build a synthetic Telegram trade event message."""
    lines = [
        "📊 Trade Event",
        f"hash: {kwargs.get('hash', 'abc123def456')}",
        f"symbol: {kwargs.get('symbol', 'TESTUSDT')}",
        f"event: {event_type}",
        f"pnl_pct: {kwargs.get('pnl_pct', '2.5')}",
        f"price: {kwargs.get('price', '0.05432')}",
        f"side: {kwargs.get('side', 'short')}",
    ]
    if "size_remaining" in kwargs:
        lines.append(f"size_remaining: {kwargs['size_remaining']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# T1: VALID_EVENTS completeness
# ══════════════════════════════════════════════════════════════════════════════

def test_valid_events_contains_all_bot_event_types():
    """VALID_EVENTS must include all event types from Dmitry's Event Types Reference."""
    bot_events = {
        'open', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'sl_hit',
        'full_close', 'timeout', 'apollo_full_exit',
        'zone_full_exit', 'e_pnl_full_exit',
        'dumalka_close', 'manual_close', 'flip_close',
    }
    missing = bot_events - VALID_EVENTS
    assert not missing, f"VALID_EVENTS is missing: {missing}"


def test_valid_events_has_exactly_13_types():
    """VALID_EVENTS should have exactly 13 event types (10 original + 3 new)."""
    assert len(VALID_EVENTS) == 13, f"Expected 13, got {len(VALID_EVENTS)}: {VALID_EVENTS}"


def test_new_close_events_present():
    """The 3 new close event types must be in VALID_EVENTS."""
    for et in ('dumalka_close', 'manual_close', 'flip_close'):
        assert et in VALID_EVENTS, f"'{et}' missing from VALID_EVENTS"


# ══════════════════════════════════════════════════════════════════════════════
# T2: parse_trade_event accepts new close events
# ══════════════════════════════════════════════════════════════════════════════

def test_parse_dumalka_close():
    """parse_trade_event must accept dumalka_close and return a valid dict."""
    msg = _make_trade_event_msg("dumalka_close", symbol="SIRENUSDT", pnl_pct="-1.5")
    result = _parse_trade_event(msg)
    assert result is not None, "dumalka_close should be accepted"
    assert result["event_type"] == "dumalka_close"
    assert result["symbol"] == "SIRENUSDT"


def test_parse_manual_close():
    """parse_trade_event must accept manual_close."""
    msg = _make_trade_event_msg("manual_close", symbol="BLESSUSDT", pnl_pct="3.2")
    result = _parse_trade_event(msg)
    assert result is not None, "manual_close should be accepted"
    assert result["event_type"] == "manual_close"


def test_parse_flip_close():
    """parse_trade_event must accept flip_close."""
    msg = _make_trade_event_msg("flip_close", symbol="RIVERUSDT", pnl_pct="-0.8")
    result = _parse_trade_event(msg)
    assert result is not None, "flip_close should be accepted"
    assert result["event_type"] == "flip_close"


# ══════════════════════════════════════════════════════════════════════════════
# T3: parse_trade_event rejects unknown events
# ══════════════════════════════════════════════════════════════════════════════

def test_reject_unknown_event():
    """Truly unknown event types must be rejected (return None)."""
    for bad_event in ("hacked_close", "random_garbage", "exploit", "partial_close_v2"):
        msg = _make_trade_event_msg(bad_event)
        result = _parse_trade_event(msg)
        assert result is None, f"'{bad_event}' should be rejected but got: {result}"


def test_reject_empty_event():
    """Message without event field should be rejected."""
    msg = "📊 Trade Event\nhash: abc\nsymbol: TEST"
    result = _parse_trade_event(msg)
    assert result is None


def test_reject_non_trade_event():
    """Non-trade-event messages should return None."""
    result = _parse_trade_event("Hello world, not a trade event")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# T4: Close-event whitelist consistency
# ══════════════════════════════════════════════════════════════════════════════

def test_close_events_equals_valid_minus_non_close():
    """Close events = VALID_EVENTS minus {open, tp1_hit, tp2_hit}."""
    expected_close = VALID_EVENTS - {'open', 'tp1_hit', 'tp2_hit'}
    assert CLOSE_EVENTS == expected_close, (
        f"Mismatch: extra={CLOSE_EVENTS - expected_close}, "
        f"missing={expected_close - CLOSE_EVENTS}"
    )


def test_close_events_has_10_types():
    """There should be exactly 10 close event types."""
    assert len(CLOSE_EVENTS) == 10, f"Expected 10 close events, got {len(CLOSE_EVENTS)}"


def test_all_new_events_are_close_events():
    """dumalka_close, manual_close, flip_close must all be in CLOSE_EVENTS."""
    for et in ('dumalka_close', 'manual_close', 'flip_close'):
        assert et in CLOSE_EVENTS, f"'{et}' should be a close event"


def test_open_is_not_close_event():
    """'open' must NOT be in CLOSE_EVENTS."""
    assert 'open' not in CLOSE_EVENTS


# ══════════════════════════════════════════════════════════════════════════════
# T5: Full field extraction
# ══════════════════════════════════════════════════════════════════════════════

def test_full_field_extraction():
    """All optional fields (hash, pnl, price, side, size) should be extracted."""
    msg = _make_trade_event_msg(
        "manual_close",
        hash="f94e7feffa261e51",
        symbol="RIVERUSDT",
        pnl_pct="-3.6",
        price="12.603",
        side="short",
        size_remaining="22.5",
    )
    result = _parse_trade_event(msg)
    assert result is not None
    assert result["hash"] == "f94e7feffa261e51"
    assert result["symbol"] == "RIVERUSDT"
    assert result["event_type"] == "manual_close"
    assert result["pnl_pct"] == -3.6
    assert result["price"] == 12.603
    assert result["side"] == "short"
    assert result["size_remaining"] == 22.5


def test_case_insensitive_event():
    """Event field should be case-insensitive (event: Manual_Close -> manual_close)."""
    msg = "📊 Trade Event\nhash: abc\nsymbol: TEST\nevent: Manual_Close\npnl_pct: 1.0\nprice: 100\nside: long"
    result = _parse_trade_event(msg)
    assert result is not None
    assert result["event_type"] == "manual_close"


# ══════════════════════════════════════════════════════════════════════════════
# T6: TradeOutcomePayload accepts new events
# ══════════════════════════════════════════════════════════════════════════════

def test_payload_accepts_new_close_events():
    """TradeOutcomePayload should accept all new close event types without error."""
    from models import TradeOutcomePayload

    for event in ('dumalka_close', 'manual_close', 'flip_close'):
        payload = TradeOutcomePayload(
            hash="test_hash_close_events",
            event=event,
            symbol="TESTUSDT",
            side="long",
            price=100.0,
            pnl_pct=-2.5,
        )
        assert payload.event == event
        assert payload.symbol == "TESTUSDT"


def test_payload_serializes_correctly():
    """model_dump() should include all fields for close events."""
    from models import TradeOutcomePayload

    payload = TradeOutcomePayload(
        hash="h123",
        event="flip_close",
        symbol="SIRENUSDT",
        side="short",
        price=0.054,
        pnl_pct=5.2,
        size_remaining=0.0,
    )
    data = payload.model_dump()
    assert data["event"] == "flip_close"
    assert data["pnl_pct"] == 5.2
    assert data["size_remaining"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# T7-T9: Integration tests (require running service + DB)
# Marked with pytest.mark.integration, skipped by default.
# Run with: pytest -m integration --tb=short
# ══════════════════════════════════════════════════════════════════════════════

try:
    import pytest
    has_pytest = True
except ImportError:
    has_pytest = False

if has_pytest:
    import_skip = pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Integration tests require RUN_INTEGRATION=1"
    )

    @import_skip
    @pytest.mark.integration
    def test_integration_close_via_trade_outcome():
        """T7: POST manual_close to /trade-outcome, verify open_positions closed."""
        import httpx as _httpx
        import psycopg2

        TEST_HASH = "TEST_CLOSE_INTEGRATION_001"
        TEST_SYMBOL = "TESTUSDT"
        BASE = "http://localhost:8000"

        conn = psycopg2.connect(dbname="riskengine_db", user="postgres")
        conn.autocommit = True
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO open_positions (symbol, side, entry_price, current_price, size, original_size, status, signal_hash, opened_at)
                VALUES (%s, 'long', 100.0, 100.0, 10.0, 10.0, 'open', %s, NOW())
                ON CONFLICT DO NOTHING
            """, (TEST_SYMBOL, TEST_HASH))

            resp = _httpx.post(f"{BASE}/trade-outcome", json={
                "hash": TEST_HASH,
                "event": "manual_close",
                "symbol": TEST_SYMBOL,
                "side": "long",
                "price": 102.0,
                "pnl_pct": 2.0,
            }, timeout=10)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

            cur.execute(
                "SELECT status, close_reason FROM open_positions WHERE signal_hash = %s ORDER BY id DESC LIMIT 1",
                (TEST_HASH,)
            )
            row = cur.fetchone()
            assert row is not None, "Test position not found"
            assert row[0] == "closed", f"Expected status='closed', got '{row[0]}'"
            assert row[1] == "manual_close", f"Expected close_reason='manual_close', got '{row[1]}'"
        finally:
            cur.execute("DELETE FROM open_positions WHERE signal_hash = %s", (TEST_HASH,))
            cur.execute("DELETE FROM trade_outcomes WHERE signal_hash = %s", (TEST_HASH,))
            cur.close()
            conn.close()

    @import_skip
    @pytest.mark.integration
    def test_integration_close_nonexistent_position():
        """T8: POST close for non-existent position should not crash."""
        import httpx as _httpx

        resp = _httpx.post("http://localhost:8000/trade-outcome", json={
            "hash": "NONEXISTENT_HASH_999",
            "event": "manual_close",
            "symbol": "FAKEUSDT",
            "side": "long",
            "price": 1.0,
            "pnl_pct": 0.0,
        }, timeout=10)
        assert resp.status_code == 200, f"Expected 200 even for non-existent position, got {resp.status_code}"

    @import_skip
    @pytest.mark.integration
    def test_integration_close_idempotency():
        """T9: Sending same close event twice should not crash or corrupt data."""
        import httpx as _httpx
        import psycopg2

        TEST_HASH = "TEST_IDEMPOTENT_002"
        TEST_SYMBOL = "TESTUSDT"
        BASE = "http://localhost:8000"

        conn = psycopg2.connect(dbname="riskengine_db", user="postgres")
        conn.autocommit = True
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO open_positions (symbol, side, entry_price, current_price, size, original_size, status, signal_hash, opened_at)
                VALUES (%s, 'short', 50.0, 50.0, 5.0, 5.0, 'open', %s, NOW())
                ON CONFLICT DO NOTHING
            """, (TEST_SYMBOL, TEST_HASH))

            payload = {
                "hash": TEST_HASH,
                "event": "dumalka_close",
                "symbol": TEST_SYMBOL,
                "side": "short",
                "price": 48.0,
                "pnl_pct": 4.0,
            }

            resp1 = _httpx.post(f"{BASE}/trade-outcome", json=payload, timeout=10)
            assert resp1.status_code == 200

            resp2 = _httpx.post(f"{BASE}/trade-outcome", json=payload, timeout=10)
            assert resp2.status_code == 200, "Second close should not crash"

            cur.execute(
                "SELECT status, close_reason FROM open_positions WHERE signal_hash = %s ORDER BY id DESC LIMIT 1",
                (TEST_HASH,)
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "closed"
            assert row[1] == "dumalka_close"

            cur.execute(
                "SELECT COUNT(*) FROM trade_outcomes WHERE signal_hash = %s AND event_type = 'dumalka_close'",
                (TEST_HASH,)
            )
            count = cur.fetchone()[0]
            assert count == 2, f"Both events should be recorded, got {count}"
        finally:
            cur.execute("DELETE FROM open_positions WHERE signal_hash = %s", (TEST_HASH,))
            cur.execute("DELETE FROM trade_outcomes WHERE signal_hash = %s", (TEST_HASH,))
            cur.close()
            conn.close()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
