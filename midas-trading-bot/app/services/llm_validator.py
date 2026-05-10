import json
import httpx
from typing import Optional, Dict, Any, List
from app.core.logger import logger
from app.core.config import settings
from app.services.llm_base import _call_openrouter, _parse_json_response


TRADE_VALIDATION_PROMPT = """Ты — ведущий аналитик хедж-фонда, специализирующийся на крипторынках. Твоя задача — провести глубокую валидацию торгового сигнала, пришедшего от опытного трейдера.

ТЕКСТ СИГНАЛА:
\"\"\"
{raw_text}
\"\"\"

ИЗВЛЕЧЕННЫЕ ДАННЫЕ (JSON):
{parsed_json}

ТЕКУЩАЯ ЦЕНА: {current_price}

### Критерии анализа:
1. **Качество парсинга**: Сличай сырой текст и JSON. Если цены перепутаны или символ не тот — это критическая ошибка.
2. **Контекст опытного трейдера**: Если в тексте есть упоминания рыночной структуры (order blocks, fvg, liquidity), объемов или сантимента — проверь, отражены ли они в логике сигнала.
3. **Риск-менеджмент**: Оцени соотношение Risk/Reward (R:R). Если R:R меньше 1:2, отметь это.
4. **Актуальность**: Если текущая цена уже прошла первую цель (TP1) или ушла далеко от зоны входа — входить может быть поздно.
5. **Рекомендация автора**: Если трейдер пишет "ждем подтверждения" или "не входить при пробое" — строго следуй этому.

### Возможные решения:
- **approve**: Сигнал качественный, парсинг верный, условия входа отличные.
- **reject**: Ошибки парсинга, слишком большой риск, или сигнал уже не актуален.

Ответь СТРОГО в формате JSON:
{{
  "decision": "approve" или "reject",
  "reason": "Детальное объяснение на русском (обязательно упомяни почему это экспертный сигнал или почему он отклонен)",
  "parsing_correct": true или false,
  "corrections": {{}} или {{"поле": "значение"}} если нужно исправить парсинг
}}"""


AUXILIARY_MONITOR_PROMPT = """Ты — торговый аналитик, мониторящий вспомогательные сигналы.

У нас есть ОТКРЫТЫЕ ПОЗИЦИИ:
{positions_json}

Пришёл вспомогательный сигнал:
\"\"\"
{raw_text}
\"\"\"

Данные сигнала:
{signal_json}

Проанализируй:
1. Связан ли сигнал с символом наших открытых позиций?
2. Опасен ли этот сигнал для наших позиций? (например: мы в Long, а пришёл сигнал "денежный поток падает")
3. Нужно ли предпринять действие?

Правила:
- "Денежный поток падает" + у нас LONG → рассмотри закрытие
- "Денежный поток растет" + у нас SHORT → рассмотри закрытие
- "Импульс разворота цены" → рассмотри закрытие если позиция в прибыли
- "Киты закончили распределение" + LONG → рассмотри закрытие
- "Лента Midas начала падать" + LONG → рассмотри подтяжку SL
- Если сигнал не связан с нашими позициями → do_nothing

Ответь СТРОГО в формате JSON (без markdown):
{{
  "action": "close" или "tighten_sl" или "do_nothing",
  "symbol": "SYMBOL" или null,
  "reason": "Объяснение на русском",
  "urgency": "high" или "medium" или "low"
}}"""


async def validate_trade_signal(
    raw_text: str,
    parsed_data: dict,
    current_price: float,
    api_key: str = None,
    model: str = None,
) -> Dict[str, Any]:
    """
    Validate a parsed trade signal using LLM.
    Returns: {"decision": "approve"/"reject", "reason": str, "parsing_correct": bool, "corrections": dict}
    """
    api_key = api_key or settings.OPENROUTER_API_KEY
    model = model or settings.OPENROUTER_MODEL

    if not api_key:
        logger.warning("LLM: No API key, auto-approving signal")
        return {"decision": "approve", "reason": "LLM unavailable (no API key)", "parsing_correct": True, "corrections": {}}

    parsed_json = json.dumps(parsed_data, ensure_ascii=False, indent=2)
    prompt = TRADE_VALIDATION_PROMPT.format(
        raw_text=raw_text,
        parsed_json=parsed_json,
        current_price=current_price,
    )

    content = await _call_openrouter(prompt, api_key, model)
    result = _parse_json_response(content)

    if not result:
        logger.warning(f"LLM: Failed to parse validation response, auto-approving. Raw: {content[:200] if content else 'None'}")
        return {"decision": "approve", "reason": "LLM response parse error", "parsing_correct": True, "corrections": {}}

    decision = result.get("decision", "reject").lower()
    if decision not in ("approve", "reject"):
        decision = "reject"

    return {
        "decision": decision,
        "reason": result.get("reason", "No reason provided"),
        "parsing_correct": result.get("parsing_correct", True),
        "corrections": result.get("corrections", {}),
    }


async def monitor_auxiliary_signal(
    raw_text: str,
    signal_data: dict,
    open_positions: List[Dict[str, Any]],
    api_key: str = None,
    model: str = None,
) -> Dict[str, Any]:
    """
    Analyze auxiliary signal against open positions.
    Returns: {"action": "close"/"tighten_sl"/"do_nothing", "symbol": str|None, "reason": str, "urgency": str}
    """
    api_key = api_key or settings.OPENROUTER_API_KEY
    model = model or settings.OPENROUTER_MODEL

    if not api_key:
        return {"action": "do_nothing", "symbol": None, "reason": "LLM unavailable", "urgency": "low"}

    if not open_positions:
        return {"action": "do_nothing", "symbol": None, "reason": "No open positions", "urgency": "low"}

    positions_json = json.dumps(open_positions, ensure_ascii=False, indent=2)
    signal_json = json.dumps(signal_data, ensure_ascii=False, indent=2)

    prompt = AUXILIARY_MONITOR_PROMPT.format(
        positions_json=positions_json,
        raw_text=raw_text,
        signal_json=signal_json,
    )

    content = await _call_openrouter(prompt, api_key, model)
    result = _parse_json_response(content)

    if not result:
        logger.warning(f"LLM: Failed to parse monitor response. Raw: {content[:200] if content else 'None'}")
        return {"action": "do_nothing", "symbol": None, "reason": "LLM response parse error", "urgency": "low"}

    action = result.get("action", "do_nothing").lower()
    if action not in ("close", "tighten_sl", "do_nothing"):
        action = "do_nothing"

    return {
        "action": action,
        "symbol": result.get("symbol"),
        "reason": result.get("reason", "No reason"),
        "urgency": result.get("urgency", "low"),
    }
