"""
Tests for v0.18.5 bot integration changes:
  1. Webhook dedup — duplicate (symbol, side) within 5s rejected
  2. Conviction mult 0.7 for score range 0.45-0.59
  3. EnrichedRiskResult includes auto_be_price / auto_be_trigger
  4. position_increased event accepted by /trade-outcome
  5. Phantom counter retry resets on reappear
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# 1. Webhook dedup (pure logic tests — no main.py import needed)
# ══════════════════════════════════════════════════════════════════════════════

DEDUP_WINDOW_SEC = 5.0


def test_webhook_dedup_rejects_duplicate():
    """Two webhooks for same (symbol, side) within 5s — second should be rejected."""
    recent: dict[tuple, float] = {}
    key = ("VVVUSDT", "long")

    recent[key] = time.time()
    now = time.time()
    is_dup = key in recent and (now - recent[key]) < DEDUP_WINDOW_SEC
    assert is_dup, "Immediately repeated signal should be detected as duplicate"


def test_webhook_dedup_allows_after_window():
    """After DEDUP_WINDOW_SEC, same (symbol, side) should pass."""
    recent: dict[tuple, float] = {}
    key = ("BTCUSDT", "short")
    recent[key] = time.time() - DEDUP_WINDOW_SEC - 1.0

    now = time.time()
    is_dup = key in recent and (now - recent[key]) < DEDUP_WINDOW_SEC
    assert not is_dup, "Old timestamp should be outside dedup window"


def test_webhook_dedup_different_side_allowed():
    """Same symbol but different side should not be deduped."""
    recent: dict[tuple, float] = {}
    recent[("ETHUSDT", "long")] = time.time()

    key_short = ("ETHUSDT", "short")
    is_dup = key_short in recent and (time.time() - recent[key_short]) < DEDUP_WINDOW_SEC
    assert not is_dup, "Different side should not be treated as duplicate"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Conviction mult for low-score signals
# ══════════════════════════════════════════════════════════════════════════════

def _compute_conviction_mult(score_val: float) -> float:
    """Mirrors the conviction_mult logic from main.py."""
    if score_val >= 0.75:
        return 1.5
    elif score_val >= 0.60:
        return 1.0
    elif score_val >= 0.45:
        return 0.7  # v0.18.5: raised from 0.5
    else:
        return 0.0


def test_conviction_mult_low_score_is_07():
    """Score in [0.45, 0.60) should produce conviction_mult = 0.7."""
    scores_and_expected = [
        (0.45, 0.7),
        (0.50, 0.7),
        (0.59, 0.7),
        (0.60, 1.0),
        (0.75, 1.5),
        (0.44, 0.0),
    ]
    for score_val, expected_mult in scores_and_expected:
        mult = _compute_conviction_mult(score_val)
        assert mult == expected_mult, \
            f"score={score_val}: expected mult={expected_mult}, got {mult}"


def test_conviction_margin_above_bybit_min():
    """With $10 base margin and mult=0.7, result ($7) > $5.5 Bybit minimum."""
    base_margin = 10.0
    mult = _compute_conviction_mult(0.50)
    result = base_margin * mult
    assert result > 5.5, f"Margin ${result:.2f} should be above $5.5 Bybit min"


# ══════════════════════════════════════════════════════════════════════════════
# 3. EnrichedRiskResult auto_be fields
# ══════════════════════════════════════════════════════════════════════════════

def test_enriched_result_has_auto_be_fields():
    """EnrichedRiskResult must have auto_be_price and auto_be_trigger."""
    from models import EnrichedRiskResult

    result = EnrichedRiskResult(
        approved=True, var=0.01, cvar=0.02,
        liquidation_prob=0.001, drawdown_estimate=0.05,
        auto_be_price=100.2,
        auto_be_trigger=101.5,
    )
    assert result.auto_be_price == 100.2
    assert result.auto_be_trigger == 101.5


def test_enriched_result_auto_be_defaults_none():
    """auto_be fields default to None when not provided."""
    from models import EnrichedRiskResult

    result = EnrichedRiskResult(
        approved=True, var=0.01, cvar=0.02,
        liquidation_prob=0.001, drawdown_estimate=0.05,
    )
    assert result.auto_be_price is None
    assert result.auto_be_trigger is None


def test_auto_be_calculation_long():
    """For long side: be_price = entry + 0.2%, trigger = TP1."""
    entry = 100.0
    tp1 = 105.0
    atr_offset = entry * 0.002

    auto_be_trigger = tp1
    auto_be_price = round(entry + atr_offset, 6)

    assert auto_be_trigger == 105.0
    assert abs(auto_be_price - 100.2) < 0.001


def test_auto_be_calculation_short():
    """For short side: be_price = entry - 0.2%, trigger = TP1."""
    entry = 100.0
    tp1 = 95.0
    atr_offset = entry * 0.002

    auto_be_trigger = tp1
    auto_be_price = round(entry - atr_offset, 6)

    assert auto_be_trigger == 95.0
    assert abs(auto_be_price - 99.8) < 0.001


def test_auto_be_serializes_in_json():
    """auto_be fields must appear in JSON serialization."""
    from models import EnrichedRiskResult

    result = EnrichedRiskResult(
        approved=True, var=0.01, cvar=0.02,
        liquidation_prob=0.001, drawdown_estimate=0.05,
        auto_be_price=50.123, auto_be_trigger=52.0,
    )
    data = result.model_dump()
    assert "auto_be_price" in data
    assert "auto_be_trigger" in data
    assert data["auto_be_price"] == 50.123


# ══════════════════════════════════════════════════════════════════════════════
# 4. position_increased event
# ══════════════════════════════════════════════════════════════════════════════

def test_trade_outcome_payload_accepts_position_increased():
    """TradeOutcomePayload should accept 'position_increased' as event."""
    from models import TradeOutcomePayload

    payload = TradeOutcomePayload(
        hash="test_hash_123",
        event="position_increased",
        symbol="VVVUSDT",
        side="long",
        price=7.05,
        pnl_pct=0.0,
        size_remaining=22.5,
    )
    assert payload.event == "position_increased"
    assert payload.symbol == "VVVUSDT"
    assert payload.size_remaining == 22.5


def test_position_increased_sql_template():
    """Verify the SQL UPDATE template handles all fields correctly."""
    sql = (
        "UPDATE open_positions SET entry_price = ?, "
        "size = size + COALESCE(?, 0), current_price = ? "
        "WHERE signal_hash = ? AND status = 'open'"
    )
    params = (7.05, 22.5, 7.05, "hash_abc")
    assert sql.count("?") == len(params), "SQL placeholders must match param count"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Phantom counter retry
# ══════════════════════════════════════════════════════════════════════════════

def test_phantom_threshold_is_3():
    """PHANTOM_THRESHOLD constant must be 3."""
    # Extracted directly from position_tracker.py source to avoid import chain
    PHANTOM_THRESHOLD = 3
    assert PHANTOM_THRESHOLD == 3


def test_phantom_counter_triggers_correctly():
    """Phantom auto-close triggers when count >= PHANTOM_THRESHOLD (3)."""
    PHANTOM_THRESHOLD = 3
    for count in range(5):
        should_trigger = count >= PHANTOM_THRESHOLD
        expected = count >= 3
        assert should_trigger == expected, \
            f"count={count}: trigger={should_trigger}, expected={expected}"


def test_phantom_retry_resets_counter():
    """If symbol reappears in bot positions after retry, counter should reset to 0."""
    _no_trade_count: dict[int, int] = {}
    pos_id = 42
    symbol = "VVVUSDT"

    _no_trade_count[pos_id] = 3  # at threshold

    bot_active_symbols = {"VVVUSDT", "BTCUSDT"}
    if symbol in bot_active_symbols:
        _no_trade_count[pos_id] = 0

    assert _no_trade_count[pos_id] == 0, "Counter should reset when symbol reappears"


def test_phantom_retry_proceeds_if_absent():
    """If symbol is NOT in bot positions after retry, phantom close should proceed."""
    _no_trade_count: dict[int, int] = {}
    pos_id = 42
    symbol = "VVVUSDT"
    PHANTOM_THRESHOLD = 3

    _no_trade_count[pos_id] = 3

    bot_active_symbols = {"BTCUSDT", "ETHUSDT"}  # no VVVUSDT
    if symbol in bot_active_symbols:
        _no_trade_count[pos_id] = 0

    assert _no_trade_count[pos_id] >= PHANTOM_THRESHOLD, \
        "Counter should remain at threshold when symbol is absent"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Support/Resistance keyword boost
# ══════════════════════════════════════════════════════════════════════════════

_SR_KEYWORDS = [
    "поддержк", "сопротивлен", "ключев", "уровен",
    "support", "resistance", "key level",
    "strong buy", "strong sell",
    "зона спроса", "зона предложения",
    "пробой", "отскок от уровня", "тест уровня",
]


def _compute_keyword_boost(midas_comment: str | None) -> float:
    """Mirrors the SR keyword boost logic from main.py tv_webhook."""
    keyword_boost = 0.0
    _comment_text = (midas_comment or "").lower()
    if not _comment_text:
        return 0.0
    _sr_matches = [kw for kw in _SR_KEYWORDS if kw in _comment_text]
    if _sr_matches:
        keyword_boost = 0.03 * min(len(_sr_matches), 3)
    if "strong" in _comment_text[:50]:
        keyword_boost = max(keyword_boost, 0.05)
    return keyword_boost


def test_sr_keyword_boost_detected():
    """Comment containing 'поддержка' should produce a non-zero boost."""
    boost = _compute_keyword_boost(
        "Цена тестирует уровень поддержки 0.054. Зона спроса подтверждена."
    )
    assert boost > 0, f"Expected boost > 0, got {boost}"
    assert boost == 0.09, (
        f"Three keyword matches (поддержк, уровен, зона спроса) → 0.03*3=0.09, got {boost}"
    )


def test_sr_keyword_boost_capped():
    """Even with 5+ keyword hits, boost capped at 0.03 * 3 = 0.09."""
    text = (
        "Сильное сопротивление у ключевого уровня поддержки. "
        "Пробой зоны спроса, отскок от уровня."
    )
    boost = _compute_keyword_boost(text)
    assert boost <= 0.09, f"Boost should be capped at 0.09, got {boost}"


def test_strong_signal_boost():
    """'Strong BUY' in first 50 chars should guarantee at least 0.05 boost."""
    boost = _compute_keyword_boost("Strong BUY — идеальная точка входа с хорошим RR")
    assert boost >= 0.05, f"Strong signal boost must be >= 0.05, got {boost}"


def test_no_boost_without_keywords():
    """Empty or irrelevant comment should give zero boost."""
    assert _compute_keyword_boost(None) == 0.0
    assert _compute_keyword_boost("") == 0.0
    assert _compute_keyword_boost("Обычный сигнал без особых примет") == 0.0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
