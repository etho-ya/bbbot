"""
pytest tests for v0.8.1: Watchlist Scanner — dump/pump detection + scoring.

Pure unit tests — no GPU, no network required.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist_scanner import (
    compute_price_changes,
    detect_opportunity,
    _record_price,
    _price_history,
    _alert_cooldown,
    COOLDOWN_SECONDS,
)


def _setup():
    """Reset global state before each test."""
    _price_history.clear()
    _alert_cooldown.clear()


# ══════════════════════════════════════════════════════════════════════════════
# PRICE CHANGE COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def test_price_change_empty_history():
    """No history → all None changes."""
    _setup()
    result = compute_price_changes("TESTUSDT", 100.0)
    assert result["change_1h"] is None
    assert result["change_4h"] is None


def test_price_change_with_history():
    """With enough history, 1h change should be computed."""
    _setup()
    symbol = "TESTUSDT"
    now = time.time()
    # Simulate price history: 1h ago price was 100, now 97 → -3%
    _price_history[symbol] = [
        (now - 4000, 100.0),  # ~1h+ ago
        (now - 3000, 99.5),
        (now - 2000, 99.0),
        (now - 1000, 98.0),
        (now - 60, 97.5),
    ]
    result = compute_price_changes(symbol, 97.0)
    assert result["change_1h"] is not None
    assert result["change_1h"] < 0  # price dropped
    assert abs(result["change_1h"] - (-3.0)) < 0.1  # ~3% drop


def test_price_change_since_last():
    """Change since last should track inter-scan movement."""
    _setup()
    symbol = "TESTUSDT"
    now = time.time()
    _price_history[symbol] = [(now - 300, 100.0)]
    result = compute_price_changes(symbol, 95.0)
    assert result["change_since_last"] is not None
    assert abs(result["change_since_last"] - (-5.0)) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# DUMP / PUMP DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def test_dump_detected():
    """Price drop > threshold should generate SHORT alert."""
    _setup()
    changes = {"change_1h": -3.5, "change_4h": -5.0}
    result = detect_opportunity("WIFUSDT", changes, 0.9, 0.0001)
    assert result is not None
    assert result["type"] == "dump"
    assert result["side"] == "short"
    assert "1h drop" in result["trigger"]


def test_pump_detected():
    """Price rise > threshold should generate LONG alert."""
    _setup()
    changes = {"change_1h": 4.0, "change_4h": 6.0}
    result = detect_opportunity("PEPEUSDT", changes, 1.2, -0.0005)
    assert result is not None
    assert result["type"] == "pump"
    assert result["side"] == "long"


def test_no_alert_below_threshold():
    """Small price change should not generate alert."""
    _setup()
    changes = {"change_1h": -1.0, "change_4h": -1.5}  # below 2.5% default
    result = detect_opportunity("WIFUSDT", changes, 0.8, 0.0)
    assert result is None


def test_4h_dump_higher_threshold():
    """4h window uses 1.5× threshold — small 4h drop with no 1h data should not alert."""
    _setup()
    changes = {"change_1h": None, "change_4h": -3.0}  # below 2.5*1.5=3.75%
    result = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result is None


def test_4h_dump_above_threshold():
    """4h drop above 1.5× threshold should generate alert."""
    _setup()
    changes = {"change_1h": None, "change_4h": -5.0}  # above 3.75%
    result = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result is not None
    assert result["type"] == "dump"
    assert "4h drop" in result["trigger"]


def test_cooldown_prevents_spam():
    """Same symbol should not alert within cooldown window."""
    _setup()
    changes = {"change_1h": -5.0, "change_4h": -8.0}

    # First alert — should fire
    result1 = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result1 is not None

    # Set cooldown
    _alert_cooldown["WIFUSDT"] = time.time()

    # Second alert — should be blocked by cooldown
    result2 = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result2 is None

    # Different symbol — should still work
    result3 = detect_opportunity("PEPEUSDT", changes, 0.9, 0.0)
    assert result3 is not None


def test_cooldown_expires():
    """After cooldown period, alerts should fire again."""
    _setup()
    changes = {"change_1h": -5.0, "change_4h": -8.0}

    # Set expired cooldown
    _alert_cooldown["WIFUSDT"] = time.time() - COOLDOWN_SECONDS - 1

    result = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result is not None


def test_none_changes_no_crash():
    """All None changes should return None without crashing."""
    _setup()
    changes = {"change_1h": None, "change_4h": None}
    result = detect_opportunity("WIFUSDT", changes, 0.9, 0.0)
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# RECORD PRICE (ring buffer)
# ══════════════════════════════════════════════════════════════════════════════

def test_record_price_basic():
    """Recording price should add to history."""
    _setup()
    _record_price("TESTUSDT", 100.0)
    assert len(_price_history["TESTUSDT"]) == 1
    _record_price("TESTUSDT", 99.0)
    assert len(_price_history["TESTUSDT"]) == 2


def test_record_price_ring_buffer():
    """History should be capped at MAX_HISTORY_POINTS."""
    _setup()
    from watchlist_scanner import MAX_HISTORY_POINTS
    for i in range(MAX_HISTORY_POINTS + 20):
        _record_price("TESTUSDT", 100.0 + i)
    assert len(_price_history["TESTUSDT"]) == MAX_HISTORY_POINTS
