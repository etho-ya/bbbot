import re
import hashlib
from typing import Optional, Dict, Any, List
from app.core.logger import logger


def generate_signal_hash(text: str) -> str:
    clean_text = " ".join(text.split()).lower()
    return hashlib.md5(clean_text.encode()).hexdigest()


def classify_message(text: str) -> str:
    """
    Classify message as 'trade', 'auxiliary', or 'ignore'.
    """
    text_upper = text.upper()

    has_entry = bool(re.search(r'ВХОД[:\s]', text_upper))
    has_targets = bool(re.search(r'ЦЕЛ[ИЬ][:\s]', text_upper))
    has_stop = bool(re.search(r'СТОП[:\s]', text_upper))

    if has_entry and has_targets and has_stop:
        return "trade"

    auxiliary_keywords = [
        'ДЕНЕЖНЫЙ ПОТОК', 'ИМПУЛЬС РАЗВОРОТА', 'ЛЕНТА MIDAS',
        'КИТЫ ЗАКОНЧИЛИ', 'WHALE', 'MONEY FLOW',
    ]
    if any(kw in text_upper for kw in auxiliary_keywords):
        return "auxiliary"

    return "ignore"


def parse_auxiliary_signal(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse auxiliary signal (money flow, impulse, etc.).
    Returns dict with symbol, message type, sentiment, timeframe.
    """
    try:
        text_clean = re.sub(r'[*_`~]', '', text)
        lines = [l.strip() for l in text_clean.strip().split('\n') if l.strip()]
        if not lines:
            return None

        first_line = lines[0]

        # Extract symbol: ETHUSDT.P or similar
        sym_match = re.search(r'([A-Z0-9]{2,10}USDT)(?:\.P)?', first_line.upper())
        if not sym_match:
            return None
        symbol = sym_match.group(1)

        # Extract timeframe
        timeframe = None
        for line in lines:
            tf_match = re.search(r'Таймфрейм[:\s]+(\S+)', line, re.IGNORECASE)
            if tf_match:
                timeframe = tf_match.group(1)
                break

        # Determine signal type and sentiment
        text_upper = text_clean.upper()
        signal_type = "unknown"
        sentiment = "neutral"

        if 'ДЕНЕЖНЫЙ ПОТОК РАСТЕТ' in text_upper or 'ДЕНЕЖНЫЙ ПОТОК РАСТЁТ' in text_upper:
            signal_type = "money_flow_up"
            sentiment = "bullish"
        elif 'ДЕНЕЖНЫЙ ПОТОК ПАДАЕТ' in text_upper:
            signal_type = "money_flow_down"
            sentiment = "bearish"
        elif 'ИМПУЛЬС РАЗВОРОТА' in text_upper:
            signal_type = "reversal_impulse"
            sentiment = "reversal"
        elif 'ЛЕНТА MIDAS' in text_upper and ('ПАДАТЬ' in text_upper or 'МЕДВЕЖИЙ' in text_upper):
            signal_type = "midas_band_bearish"
            sentiment = "bearish"
        elif 'ЛЕНТА MIDAS' in text_upper and ('РАСТИ' in text_upper or 'БЫЧИЙ' in text_upper):
            signal_type = "midas_band_bullish"
            sentiment = "bullish"
        elif 'КИТЫ ЗАКОНЧИЛИ РАСПРЕДЕЛЕНИЕ' in text_upper or ('РАЗВОРОТ' in text_upper and 'ВНИЗ' in text_upper):
            signal_type = "whale_distribution"
            sentiment = "bearish"
        elif 'КИТЫ ЗАКОНЧИЛИ НАКОПЛЕНИЕ' in text_upper or ('РАЗВОРОТ' in text_upper and 'ВВЕРХ' in text_upper):
            signal_type = "whale_accumulation"
            sentiment = "bullish"

        return {
            "symbol": symbol,
            "signal_type": signal_type,
            "sentiment": sentiment,
            "timeframe": timeframe,
            "raw_message": first_line,
        }
    except Exception as e:
        logger.error(f"Signal: Auxiliary parse error: {e}")
        return None


def parse_trade_signal(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse a trade signal in the new format:

    ETHUSDT.P 1M - SELL/BUY
    Вход: 1988.34-1987.25
    Цели: 1982.37, 1980.05, 1975.78
    Стоп: 1988.13, Трейлинг: 0.03%
    + metadata (risk/reward, probability, win-rate, etc.)
    + market situation text
    + recommendation text
    """
    raw_text = text
    try:
        text_clean = re.sub(r'[*_`~]', '', text)
        lines = [l.strip() for l in text_clean.strip().split('\n') if l.strip()]
        if not lines:
            return None

        full_text = text_clean
        full_upper = full_text.upper()

        # 1. SYMBOL - match uppercase symbols like ETHUSDT, ETHUSDC or ETHUSDT.P
        # Support various formats: ONDOUSDC.P, BTCUSDT, ETH, etc.
        sym_match = re.search(r'\b([A-Z0-9]{2,10})(?:USDT|USDC)?(?:\.P)?\b', lines[0].upper())
        if not sym_match:
            sym_match = re.search(r'\b([A-Z0-9]{2,10})(?:USDT|USDC)?(?:\.P)?\b', full_upper)

        if not sym_match:
            logger.warning("Signal: Symbol not found")
            return None
        symbol = sym_match.group(1).upper()
        # Normalize to USDT if it's just 'ONDO' or 'ONDOUSDC'
        if not symbol.endswith("USDT"):
            if symbol.endswith("USDC"):
                symbol = symbol[:-4] + "USDT"
            else:
                symbol = symbol + "USDT"

        # 2. TIMEFRAME
        timeframe = None
        tf_match = re.search(r'(\d+[MHDmhd]|[14]\s*[Чч])', lines[0])
        if tf_match:
            timeframe = tf_match.group(1).strip()

        # 3. DIRECTION
        first_line_upper = lines[0].upper()
        if any(x in first_line_upper for x in ["SELL", "SHORT", "ШОРТ"]) or '🔴' in lines[0]:
            direction = "SHORT"
            side = "Sell"
        elif any(x in first_line_upper for x in ["BUY", "LONG", "ЛОНГ"]) or '🟢' in lines[0]:
            direction = "LONG"
            side = "Buy"
        else:
            if '▼' in lines[0] or '🔻' in lines[0]:
                direction = "SHORT"
                side = "Sell"
            elif '▲' in lines[0] or '🔺' in lines[0]:
                direction = "LONG"
                side = "Buy"
            else:
                logger.warning("Signal: Direction not found")
                return None

        # 4. ENTRY RANGE
        entry_match = re.search(
            r'Вход[:\s]+(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)',
            full_text, re.IGNORECASE
        )
        if entry_match:
            entry_high = float(entry_match.group(1).replace(',', '.'))
            entry_low = float(entry_match.group(2).replace(',', '.'))
            if entry_low > entry_high:
                entry_high, entry_low = entry_low, entry_high
            entry_price = (entry_high + entry_low) / 2
        else:
            # Fallback: single entry price
            entry_single = re.search(
                r'Вход[:\s]+(\d+(?:[.,]\d+)?)',
                full_text, re.IGNORECASE
            )
            if entry_single:
                entry_price = float(entry_single.group(1).replace(',', '.'))
                entry_high = entry_price
                entry_low = entry_price
            else:
                logger.warning("Signal: Entry price not found")
                return None

        # 5. TARGETS
        targets = []
        targets_match = re.search(
            r'Цел[иь][:\s]+([\d.,\s]+)',
            full_text, re.IGNORECASE
        )
        if targets_match:
            raw = targets_match.group(1)
            targets = [float(x.replace(',', '.')) for x in re.findall(r'\d+(?:[.,]\d+)?', raw)]

        if not targets:
            for m in re.finditer(r'(?:Цель|TP|Тейк)\s*\d*\s*[:\-–]\s*(\d+(?:[.,]\d+)?)', full_text, re.IGNORECASE):
                targets.append(float(m.group(1).replace(',', '.')))

        if not targets:
            logger.warning("Signal: Targets not found")
            return None

        targets = sorted(list(set(targets)), reverse=(direction == "SHORT"))
        tp1 = targets[0]
        tp2 = targets[1] if len(targets) > 1 else tp1
        tp3 = targets[2] if len(targets) > 2 else tp2

        # 6. STOP LOSS
        sl = 0.0
        sl_match = re.search(
            r'Стоп[:\s]+(\d+(?:[.,]\d+)?)',
            full_text, re.IGNORECASE
        )
        if sl_match:
            sl = float(sl_match.group(1).replace(',', '.'))
        else:
            logger.warning(f"Signal: SL not found for {symbol}, will use default")

        # 7. TRAILING STOP
        trailing_pct = None
        trail_match = re.search(
            r'Трейлинг[:\s]+(\d+(?:[.,]\d+)?)\s*%',
            full_text, re.IGNORECASE
        )
        if trail_match:
            trailing_pct = float(trail_match.group(1).replace(',', '.'))

        # 8. METADATA
        metadata = {}

        rr_match = re.search(r'Риск\s*к\s*прибыли[:\s]+1\s*к\s*(\d+(?:[.,]\d+)?)', full_text, re.IGNORECASE)
        if rr_match:
            metadata["risk_reward"] = float(rr_match.group(1).replace(',', '.'))

        prob_match = re.search(r'Вероятность[:\s]+(\d+(?:[.,]\d+)?)\s*%', full_text, re.IGNORECASE)
        if prob_match:
            metadata["probability"] = float(prob_match.group(1).replace(',', '.'))

        wr_match = re.search(r'Win-?Rate[^:]*[:\s]+(\d+(?:[.,]\d+)?)\s*\.?\s*%', full_text, re.IGNORECASE)
        if wr_match:
            metadata["win_rate"] = float(wr_match.group(1).replace(',', '.'))

        vol_match = re.search(r'Волатильность\s+(\S+)', full_text, re.IGNORECASE)
        if vol_match:
            metadata["volatility"] = vol_match.group(1)

        volume_match = re.search(r'Объем\s+(\S+)', full_text, re.IGNORECASE)
        if volume_match:
            metadata["volume"] = volume_match.group(1)

        # 9. SITUATION & RECOMMENDATION TEXT
        situation = ""
        sit_match = re.search(r'Ситуация по рынку[:\s]+(.*?)(?=🧠|$)', full_text, re.DOTALL | re.IGNORECASE)
        if sit_match:
            situation = sit_match.group(1).strip()

        recommendation = ""
        rec_match = re.search(r'Что делать сейчас[:\s]+(.*?)$', full_text, re.DOTALL | re.IGNORECASE)
        if rec_match:
            recommendation = rec_match.group(1).strip()

        result = {
            "symbol": symbol,
            "side": side,
            "direction": direction,
            "entry_price": round(entry_price, 8),
            "entry_high": round(entry_high, 8),
            "entry_low": round(entry_low, 8),
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "trailing_stop_pct": trailing_pct,
            "timeframe": timeframe,
            "metadata": metadata,
            "situation": situation,
            "recommendation": recommendation,
        }

        logger.info(f"Signal: Parsed {symbol} {side} @ {entry_price}, TP1={tp1}, SL={sl}, trailing={trailing_pct}%")
        return result

    except Exception as e:
        logger.error(f"Signal: Critical parse error: {e}")
        return None


def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    """Main entry point: classify and parse a signal message."""
    msg_type = classify_message(text)

    if msg_type == "trade":
        return parse_trade_signal(text)
    elif msg_type == "auxiliary":
        return parse_auxiliary_signal(text)

    return None
