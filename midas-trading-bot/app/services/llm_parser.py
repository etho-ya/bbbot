import json
from typing import Optional, Dict, Any
from app.core.logger import logger
from app.core.config import settings
from app.services.llm_base import _call_openrouter, _parse_json_response

SIGNAL_EXTRACTION_PROMPT = """Ты — эксперт мирового уровня по анализу профессиональных крипто-сигналов. Твоя миссия: деконструировать текст сообщения и извлечь КАЖДЫЙ торговый параметр с математической точностью.

ТЕКСТ СООБЩЕНИЯ:
\"\"\"
{text}
\"\"\"

Извлеки следующие данные и верни СТРОГО в формате JSON:
{{
  "symbol": "Монета (например, ETHUSDT). Если в тексте только 'ETH' или 'ETHUSDT.P', преврати это в 'ETHUSDT'",
  "side": "'Buy' (для Long) или 'Sell' (для Short)",
  "direction": "'LONG' или 'SHORT'",
  "entry_price": Число (средняя цена входа. Если указан диапазон, посчитай среднее арифметическое)",
  "entry_high": Число (верхняя граница входа. Если диапазона нет, то же что entry_price)",
  "entry_low": Число (нижняя граница входа. Если диапазона нет, то же что entry_price)",
  "tp1": Число (обязательная первая цель)",
  "tp2": Число или null (вторая цель)",
  "tp3": Число или null (третья цель)",
  "sl": Число (обязательный стоп-лосс. Если не указан, поставь null)",
  "trailing_stop_pct": Число или null (если указан трейлинг, например 'Трейлинг: 0.05%', извлеки только 0.05)",
  "timeframe": "Таймфрейм (например, 1M, 5M, 1H, 4H)",
  "leverage": Число или null (плечо, например из '10x' извлеки 10)"
}}

### Критические правила:
1. **Диапазон входа**: Если вход '1988.34-1987.25', то entry_high=1988.34, entry_low=1987.25, entry_price=1987.795.
2. **Символ**: Обязательно добавляй USDT в конец. Убирай любые '.P', '/', '#' и другие спецсимволы.
3. **Направление**: Ориентируйся на ключевые слова (LONG, SHORT, BUY, SELL, ЛОНГ, ШОРТ, КУПИТЬ, ПРОДАТЬ) и эмодзи (🟢/🔴).
4. **Числа**: Используй только точки (.) как десятичные разделители. Не используй запятые.
5. **JSON**: Не добавляй никакого текста до или после JSON объекта.
"""

async def parse_signal_with_llm(text: str, api_key: str = None, model: str = None) -> Optional[Dict[str, Any]]:
    """Extract trade parameters from text using LLM."""
    api_key = api_key or settings.OPENROUTER_API_KEY
    model = model or settings.OPENROUTER_MODEL

    if not api_key:
        logger.warning("LLM Parser: No API key provided")
        return None

    prompt = SIGNAL_EXTRACTION_PROMPT.format(text=text)
    try:
        content = await _call_openrouter(prompt, api_key, model)
    except Exception as e:
        logger.error(f"LLM Parser: OpenRouter call failed after retries: {e}")
        return None
        
    result = _parse_json_response(content)

    if not result or "symbol" not in result:
        logger.warning(f"LLM Parser: Failed to extract signal data. Raw: {content[:200] if content else 'None'}")
        return None

    # Post-processing: ensure symbol format
    symbol = result.get("symbol")
    if symbol:
        symbol = str(symbol).upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
    result["symbol"] = symbol

    logger.info(f"LLM Parser: Successfully extracted {symbol} {result.get('side')}")
    return result
