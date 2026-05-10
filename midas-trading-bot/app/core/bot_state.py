# app/core/bot_state.py
from datetime import datetime
from typing import Optional
from app.core.logger import logger

class BotState:
    def __init__(self):
        self._is_running: bool = False
        self._started_at: Optional[datetime] = None
        self._processed_signals_count: int = 0
        self._active_orders_count: int = 0
        self._loss_reset_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    @property
    def loss_reset_at(self) -> Optional[datetime]:
        return self._loss_reset_at

    @property
    def processed_signals_count(self) -> int:
        return self._processed_signals_count

    @property
    def active_orders_count(self) -> int:
        return self._active_orders_count

    def start(self):
        if not self._is_running:
            self._is_running = True
            self._started_at = datetime.utcnow()
            self._loss_reset_at = datetime.utcnow()
            logger.info("Bot: Started, daily loss counter reset")

    def stop(self):
        if self._is_running:
            self._is_running = False
            # Мы не обнуляем статистику при остановке, только статус
            logger.info("Bot: Stopped")

    def increment_signals(self):
        self._processed_signals_count += 1

    def set_active_orders(self, count: int):
        self._active_orders_count = count

    def get_status(self) -> dict:
        return {
            "is_running": self._is_running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "signals_count": self._processed_signals_count,
            "active_orders_count": self._active_orders_count,
            "uptime_seconds": (datetime.utcnow() - self._started_at).total_seconds() if self._started_at and self._is_running else 0
        }

bot_state = BotState()
