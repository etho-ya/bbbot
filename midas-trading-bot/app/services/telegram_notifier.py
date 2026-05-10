from typing import Optional
from datetime import datetime, timezone
from app.core.logger import logger


class TelegramNotifier:
    """Send notifications via the same Telethon user account that reads signals."""

    def __init__(self):
        self._client = None
        self._chat_id: Optional[str] = None

    def configure(self, telethon_client, chat_id: str):
        self._client = telethon_client
        self._chat_id = chat_id
        if chat_id:
            logger.info(f"TG Notifier: Configured (chat_id={chat_id})")

    @property
    def is_configured(self) -> bool:
        return bool(self._client and self._chat_id)

    async def send(self, text: str):
        if not self.is_configured:
            return
        try:
            if not self._client.is_connected():
                logger.warning("TG Notifier: Client not connected, skipping")
                return
            raw = (self._chat_id or "").strip()
            if not raw:
                return
            # Support numeric id (e.g. -100123456) or public username (e.g. uebot_report or @uebot_report)
            if raw.lstrip("-").isdigit():
                peer = int(raw)
            else:
                username = raw.lstrip("@")
                peer = await self._client.get_entity(username)
            await self._client.send_message(peer, text, parse_mode='html')
        except Exception as e:
            logger.error(f"TG Notifier: Send error: {e}")

    async def notify_trade_opened(self, symbol: str, side: str, entry: float, size: float, leverage: int,
                                   order_id: str = None, signal_hash: str = None):
        direction = "LONG" if side == "Buy" else "SHORT"
        emoji = "\u2705" if side == "Buy" else "\U0001f534"
        text = (
            f"{emoji} <b>Сделка открыта</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Вход: <code>{entry}</code>\n"
            f"Размер: <code>{size:.2f} USDT</code>\n"
            f"Плечо: <code>{leverage}x</code>"
        )
        if order_id:
            text += f"\nOrder: <code>{order_id}</code>"
        if signal_hash:
            text += f"\nHash: <code>{signal_hash}</code>"
        await self.send(text)

    async def notify_trade_closed(self, symbol: str, side: str, pnl: float, reason: str,
                                   close_price: float = None, entry_price: float = None,
                                   order_id: str = None, signal_hash: str = None):
        direction = "LONG" if side == "Buy" else "SHORT"
        emoji = "\u2705" if pnl >= 0 else "\u274c"
        text = (
            f"{emoji} <b>Сделка закрыта</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Направление: <b>{direction}</b>\n"
            f"P/L: <code>{pnl:+.2f} USDT</code>\n"
            f"Причина: {reason}"
        )
        if entry_price is not None and close_price is not None:
            text += f"\nВход: <code>{entry_price}</code> → Выход: <code>{close_price}</code>"
        if order_id:
            text += f"\nOrder: <code>{order_id}</code>"
        if signal_hash:
            text += f"\nHash: <code>{signal_hash}</code>"
        await self.send(text)

    async def notify_tp_reached(self, symbol: str, tp_level: int, closed_pct: int, new_sl: float = None):
        text = (
            f"\U0001f3af <b>TP{tp_level} достигнут</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Закрыто: <code>{closed_pct}%</code>"
        )
        if new_sl is not None:
            text += f"\nSL перенесён: <code>{new_sl}</code>"
        await self.send(text)

    async def notify_sl_hit(self, symbol: str, side: str, pnl: float,
                             close_price: float = None, entry_price: float = None,
                             order_id: str = None, signal_hash: str = None):
        direction = "LONG" if side == "Buy" else "SHORT"
        text = (
            f"\U0001f6d1 <b>Stop Loss сработал</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Направление: <b>{direction}</b>\n"
            f"P/L: <code>{pnl:+.2f} USDT</code>"
        )
        if entry_price is not None and close_price is not None:
            text += f"\nВход: <code>{entry_price}</code> → Выход: <code>{close_price}</code>"
        if order_id:
            text += f"\nOrder: <code>{order_id}</code>"
        if signal_hash:
            text += f"\nHash: <code>{signal_hash}</code>"
        await self.send(text)

    async def notify_llm_rejected(self, symbol: str, side: str, reason: str):
        direction = "LONG" if side == "Buy" else "SHORT"
        text = (
            f"\U0001f916 <b>LLM отклонила сигнал</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Причина: {reason}"
        )
        await self.send(text)

    async def notify_auxiliary_action(self, symbol: str, action: str, reason: str):
        action_text = {"close": "Закрытие позиции", "tighten_sl": "Подтяжка SL"}.get(action, action)
        text = (
            f"\u26a0\ufe0f <b>Auxiliary signal</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Действие: <b>{action_text}</b>\n"
            f"Причина: {reason}"
        )
        await self.send(text)

    async def notify_error(self, message: str):
        text = f"\u26a0\ufe0f <b>Ошибка</b>\n\n{message}"
        await self.send(text)

    async def notify_kill_switch(self, reason: str):
        text = (
            f"\U0001f6a8 <b>KILL SWITCH</b>\n\n"
            f"Причина: {reason}\n"
            f"Новые сделки приостановлены."
        )
        await self.send(text)

    async def send_to_chat(self, chat_id: str, text: str):
        """Send message to a specific chat (not the default notify chat)."""
        if not self._client:
            return
        try:
            if not self._client.is_connected():
                logger.warning("TG Notifier: Client not connected, skipping")
                return
            raw = (chat_id or "").strip()
            if not raw:
                return
            if raw.lstrip("-").isdigit():
                peer = int(raw)
            else:
                username = raw.lstrip("@")
                peer = await self._client.get_entity(username)
            await self._client.send_message(peer, text, parse_mode='html')
        except Exception as e:
            logger.error(f"TG Notifier: send_to_chat error ({chat_id}): {e}")

    async def notify_re_request(self, chat_id: str, symbol: str, side: str,
                                 entry: float, tp1: float, tp2: float, tp3: float,
                                 sl: float, sig_hash: str, raw_text: str = "",
                                 risk_reward: float = None, probability: float = None,
                                 win_rate: float = None, trend: str = None):
        """Send signal approval request to @uebot_report with Midas metadata."""
        direction = "LONG" if side == "Buy" else "SHORT"
        text = (
            f"\U0001f514 <b>Request for approval</b>\n"
            f"hash: <code>{sig_hash}</code>\n\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Side: <b>{side} ({direction})</b>\n"
            f"Entry: <code>{entry}</code>\n"
            f"TP1: <code>{tp1}</code> | TP2: <code>{tp2}</code> | TP3: <code>{tp3}</code>\n"
            f"SL: <code>{sl}</code>"
        )
        # Midas metadata for RE scoring
        if risk_reward is not None:
            text += f"\nRiskReward: <code>{risk_reward}</code>"
        if probability is not None:
            text += f"\nProbability: <code>{probability}</code>"
        if win_rate is not None:
            text += f"\nWinRate: <code>{win_rate}</code>"
        if trend:
            text += f"\nTrend: <code>{trend}</code>"

        if raw_text:
            text += f"\n\n--- Raw Signal ---\n{raw_text}"

        await self.send_to_chat(chat_id, text)

    async def notify_signal_report(self, chat_id: str, symbol: str, side: str,
                                    entry: float, tp1: float, tp2: float, tp3: float,
                                    sl: float, sig_hash: str,
                                    re_decision: str = None, re_score: float = None,
                                    conviction_usd: float = None, rejection_reason: str = None,
                                    metadata: dict = None,
                                    situation: str = None, recommendation: str = None):
        """Send full signal report to the report group for human observability."""
        direction = "LONG" if side == "Buy" else "SHORT"
        emoji = {"approve": "\u2705", "reject": "\u274c", "timeout": "\u23f3"}.get(
            re_decision or "", "\u2753"
        )

        text = (
            f"{emoji} <b>{symbol}</b> {direction}\n"
            f"<code>{sig_hash}</code>\n"
            f"{'━' * 24}\n"
            f"Entry: <code>{entry}</code>\n"
            f"TP1: <code>{tp1}</code>\n"
            f"TP2: <code>{tp2}</code>\n"
            f"TP3: <code>{tp3}</code>\n"
            f"SL:  <code>{sl}</code>"
        )

        if metadata:
            rr = metadata.get("risk_reward")
            prob = metadata.get("probability")
            wr = metadata.get("win_rate")
            vol = metadata.get("volume") or metadata.get("volatility")
            parts = []
            if rr is not None:
                parts.append(f"R:R={rr}")
            if prob is not None:
                parts.append(f"Prob={prob}")
            if wr is not None:
                parts.append(f"WR={wr}")
            if vol is not None:
                parts.append(f"Vol={vol}")
            if parts:
                text += f"\n{' | '.join(parts)}"

        if re_decision:
            text += f"\n{'━' * 24}\n"
            text += f"RE: <b>{re_decision.upper()}</b>"
            if re_score is not None:
                text += f" (score={re_score:.2f})"
            if conviction_usd is not None:
                text += f"\nSize: <code>${conviction_usd:.1f}</code>"
            if rejection_reason:
                text += f"\nReason: {rejection_reason}"

        if situation:
            text += f"\n{'━' * 24}\n"
            text += f"<b>Ситуация:</b> {situation}"

        if recommendation:
            text += f"\n\n<b>Рекомендация:</b> {recommendation}"

        await self.send_to_chat(chat_id, text)

    async def notify_re_decision(self, symbol: str, decision: str, reason: str = ""):
        """Notify user about RE decision."""
        emoji = "\u2705" if decision == "approve" else "\u274c"
        text = (
            f"{emoji} <b>Risk Engine: {decision.upper()}</b>\n\n"
            f"Символ: <code>{symbol}</code>"
        )
        if reason:
            text += f"\nПричина: {reason}"
        await self.send(text)

    async def notify_bot_restarted(self, active_trades: int = 0):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"\U0001f504 <b>Бот перезапущен</b>\n\n"
            f"Время: <code>{ts}</code>\n"
            f"Восстановлено сделок: <code>{active_trades}</code>"
        )
        await self.send(text)

    async def notify_bot_started(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"\u25b6\ufe0f <b>Бот запущен (Web UI)</b>\n\n"
            f"Время: <code>{ts}</code>"
        )
        await self.send(text)

    async def notify_dumalka_unavailable(self, last_seen_sec: float):
        minutes = int(last_seen_sec // 60)
        text = (
            f"\U0001f6a8 <b>Думалка недоступна</b>\n\n"
            f"Последний опрос: <code>{minutes} мин. назад</code>\n"
            f"Управление SL/TP не работает!"
        )
        await self.send(text)

    async def notify_stale_signal(self, symbol: str, side: str, age_seconds: float):
        direction = "LONG" if side == "Buy" else "SHORT"
        text = (
            "⏱️ <b>Dropped stale signal</b>\n\n"
            f"Символ: <code>{symbol}</code>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Возраст сигнала: <code>{age_seconds:.1f}s</code>"
        )
        await self.send(text)


notifier = TelegramNotifier()
