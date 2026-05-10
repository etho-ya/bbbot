import asyncio
import json
import httpx
from typing import Optional, Any
from app.core.logger import logger
from app.core.config import settings

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Global lock to prevent concurrent LLM calls hitting 429
_llm_lock = asyncio.Lock()

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=20),
    retry=retry_if_exception_type(httpx.HTTPStatusError),
    reraise=True
)
async def _call_openrouter(prompt: str, api_key: str, model: str) -> Optional[str]:
    """Call OpenRouter API and return response content with retry and rate-limiting lock."""
    async with _llm_lock:
        # Increase delay between requests to avoid rate limits
        await asyncio.sleep(2.0)
        
        url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/midas-trading-bot",
        "X-Title": "Midas Trading Bot",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            content = data['choices'][0]['message']['content'].strip()

            if content.startswith("```json"):
                content = content.replace("```json", "").replace("```", "").strip()
            elif content.startswith("```"):
                content = content.replace("```", "").strip()

            return content
    except httpx.HTTPStatusError as e:
        logger.warning(f"LLM: OpenRouter HTTP error {e.response.status_code}. Retrying...")
        raise e # Reraise for tenacity
    except Exception as e:
        logger.error(f"LLM: OpenRouter API error: {e}")
        return None

def _parse_json_response(content: str) -> Optional[dict]:
    """Parse JSON from LLM response with fallback regex."""
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None
