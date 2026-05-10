# app/core/cache.py
import time
from typing import Any, Optional

class TTLCache:
    def __init__(self):
        self._cache = {}
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, expires = self._cache[key]
            if time.time() < expires:
                return value
            # Если срок истек, удаляем из кэша
            del self._cache[key]
        return None
    
    def set(self, key: str, value: Any, ttl: int = 30):
        self._cache[key] = (value, time.time() + ttl)
    
    def delete(self, key: str):
        if key in self._cache:
            del self._cache[key]

    def clear(self):
        self._cache = {}

# Глобальный инстанс кэша
cache = TTLCache()
