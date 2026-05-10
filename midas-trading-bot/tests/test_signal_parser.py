"""
Unit tests for signal_parser.py
Tests all 4 signal formats to ensure reliable parsing.
"""
import pytest
from app.services.signal_parser import parse_signal


class TestSignalParser:
    """Test suite for parse_signal function."""

    def test_format1_hashtag_signal(self):
        """Format 1: #COIN LONG/SHORT with standard structure."""
        text = """
        #SUI SHORT 🔽

        Вход : Рынок ( 1.4549 ) 

        ✔️Тейк - 1.4402
        ✔️Тейк - 1.4196
        ✔️Тейк - 1.3553

        ❌Cтоп: 1.5367

        Маржа: 400$
        Банк: 4 950.02$
        """
        result = parse_signal(text)
        
        assert result is not None
        assert result["symbol"] == "SUIUSDT"
        assert result["side"] == "Sell"
        assert result["direction"] == "SHORT"
        assert result["entry_price"] == 1.4549
        assert result["tp1"] == 1.4402
        assert result["sl"] == 1.5367

    def test_format2_slash_usdt_market_order(self):
        """Format 2: COIN/USDT with market order and Добор price."""
        text = """
        ARB/USDT LONG на 950$ в рамках марафона 📈

        Остаток депозита: 23737$

        Вход: по рынку, Добор: ниже 0.1745

        Тейки: 0.1816 0.1834 0.1980

        😍BINGX — 11000$ бонус
        """
        result = parse_signal(text)
        
        assert result is not None
        assert result["symbol"] == "ARBUSDT"
        assert result["side"] == "Buy"
        assert result["direction"] == "LONG"
        assert result["entry_price"] == 0.1745
        assert result["tp1"] == 0.1816
        assert result["tp2"] == 0.1834
        assert result["tp3"] == 0.1980

    def test_format3_moneta_keyword(self):
        """Format 3: Монета: COIN LONG with Цена входа."""
        text = """
        Монета: NEAR LONG Х25 

        🔵Цена входа: 1.552

        ✅Тэйки: 1.568 1.583 1.669

        🛑Стоп: 1.481

        Входим на 35$
        🏦Банк: 349.2$
        """
        result = parse_signal(text)
        
        assert result is not None
        assert result["symbol"] == "NEARUSDT"
        assert result["side"] == "Buy"
        assert result["direction"] == "LONG"
        assert result["entry_price"] == 1.552
        assert result["tp1"] == 1.568
        assert result["tp2"] == 1.583
        assert result["tp3"] == 1.669
        assert result["sl"] == 1.481

    def test_format4_standalone_with_tvh(self):
        """Format 4: COIN LONG with ТВХ range entry."""
        text = """
        AVAX LONG 🔼

        ➡️Твх - 12.37 - 12.03

        ✔️Тейк 1 - 12.43
        ✔️Тейк 2 - 12.49
        ✔️Тейк 3 - 12.56

        🚩Стоп-лос ставим соблюдая ваш риск-менеджмент.
        """
        result = parse_signal(text)
        
        assert result is not None
        assert result["symbol"] == "AVAXUSDT"
        assert result["side"] == "Buy"
        assert result["direction"] == "LONG"
        assert result["entry_price"] == 12.37  # First value in range
        assert result["tp1"] == 12.43
        assert result["tp2"] == 12.49
        assert result["tp3"] == 12.56
        assert result["sl"] == 0.0  # No SL specified

    def test_non_signal_profit_report(self):
        """Should return None for profit reports."""
        text = """
        Profit ✅

        Забрали 1 цель по #ARB⚡️Закрыл 75% позиции, стоп в б\у
        """
        result = parse_signal(text)
        assert result is None

    def test_non_signal_greeting(self):
        """Should return None for greetings without signal data."""
        text = """
        Всем доброго утра! ☀️

        По NEAR цена улетела вверх и забрала два наших тейка!
        """
        result = parse_signal(text)
        assert result is None

    def test_non_signal_announcement(self):
        """Should return None for 'Захожу в' announcements without prices."""
        text = """
        Захожу в LONG по монете #ARB плечо 30x маржа 400$
        """
        result = parse_signal(text)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
