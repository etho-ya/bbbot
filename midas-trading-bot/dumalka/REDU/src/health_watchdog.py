"""
Health Watchdog — v0.19.7 System Component Monitor
====================================================

Background loop (60s cycle) that monitors all Risk Engine subsystems and
sends structured Telegram alerts when components go down or the external
trading bot becomes unreachable for longer than BOT_UNREACHABLE_ALERT_SEC.

Monitors:
  - 8 heartbeat-tracked background tasks (position_tracker, kline_collector, etc.)
  - Sentinel (Binance flash-crash monitor)
  - Bybit WebSocket price feed
  - Trading bot connectivity (via position_tracker._bot_last_success_ts)

Features:
  - Alert deduplication with configurable cooldown (WATCHDOG_ALERT_COOLDOWN_SEC)
  - Recovery notifications when a component comes back online
  - Severity tiers: CRITICAL (TG alert), WARNING (TG alert), INFO (log only)
  - Human-readable impact descriptions per component

Config (env vars):
  HEALTH_WATCHDOG_ENABLED  (default true)
  BOT_UNREACHABLE_ALERT_SEC (default 420 = 7 min)
  WATCHDOG_ALERT_COOLDOWN_SEC (default 1800 = 30 min)

2026-04-11
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from config import config
from notifications import send_telegram_message

logger = logging.getLogger("risk-engine.watchdog")

# ── Heartbeat for /health integration ────────────────────────────────────────
_heartbeat: dict[str, float] = {}

WATCHDOG_INTERVAL_SEC = 60

# ── Severity tiers ───────────────────────────────────────────────────────────
# CRITICAL: immediate TG alert — directly affects live trading
# WARNING:  TG alert — supporting systems, degrades functionality
# INFO:     log only — analytics/non-critical, no TG spam
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"

COMPONENT_META: dict[str, dict] = {
    "position_tracker": {
        "severity": SEVERITY_CRITICAL,
        "impact": "Управление позициями остановлено — нет SL/TP движений, нет закрытий",
        "action": "systemctl status risk-engine",
    },
    "bot_connection": {
        "severity": SEVERITY_CRITICAL,
        "impact": "Думалка не может отправлять команды боту (SL, закрытия). Keepalive не работает — бот перейдет на собственную логику",
        "action": "Проверить бот, сеть, Tailscale",
    },
    "kline_collector": {
        "severity": SEVERITY_WARNING,
        "impact": "Данные свечей устаревают — Scout и ML-фичи деградируют",
        "action": "systemctl status risk-engine / логи kline_collector",
    },
    "scout": {
        "severity": SEVERITY_WARNING,
        "impact": "Генерация Scout-сигналов остановлена",
        "action": "systemctl status risk-engine / логи scout",
    },
    "watchlist_scanner": {
        "severity": SEVERITY_WARNING,
        "impact": "Детекция pump/dump остановлена",
        "action": "systemctl status risk-engine",
    },
    "sentinel": {
        "severity": SEVERITY_WARNING,
        "impact": "Мониторинг flash-crash BTC отключен",
        "action": "Проверить Binance WS подключение",
    },
    "bybit_ws": {
        "severity": SEVERITY_WARNING,
        "impact": "WebSocket прайс-фид отключен",
        "action": "Проверить Bybit WS подключение",
    },
    "analytics_precompute": {
        "severity": SEVERITY_INFO,
        "impact": "Аналитика дашборда устарела (не критично)",
        "action": "systemctl status risk-engine",
    },
    "bybit_pnl_refresh": {
        "severity": SEVERITY_INFO,
        "impact": "Обновление PnL приостановлено (не критично)",
        "action": "systemctl status risk-engine",
    },
    "exit_quality": {
        "severity": SEVERITY_INFO,
        "impact": "Анализ качества выходов приостановлен (не критично)",
        "action": "systemctl status risk-engine",
    },
    "ml_labeler": {
        "severity": SEVERITY_INFO,
        "impact": "ML-разметка приостановлена (не критично)",
        "action": "systemctl status risk-engine",
    },
}

# Same stale thresholds as /health endpoint for consistency
STALE_THRESHOLDS: dict[str, int] = {
    "position_tracker": 300,
    "analytics_precompute": 5400,
    "bybit_pnl_refresh": 600,
    "watchlist_scanner": 600,
    "exit_quality": 600,
    "ml_labeler": 6 * 3600 + 600,
    "kline_collector": 300,
    "scout": 1800,
}

# ── Alert state tracking ────────────────────────────────────────────────────
# {component: {"alerted_at": float, "down_since": float}}
_alert_state: dict[str, dict] = {}


def _fmt_duration(seconds: float) -> str:
    """Human-readable duration: '4m 12s', '1h 23m', '2d 5h'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _build_down_alert(component: str, status_detail: str) -> str:
    meta = COMPONENT_META.get(component, {})
    severity = meta.get("severity", SEVERITY_WARNING)
    impact = meta.get("impact", "")
    action = meta.get("action", "")

    icon = "🔴" if severity == SEVERITY_CRITICAL else "🟡"
    sev_label = "CRITICAL" if severity == SEVERITY_CRITICAL else "WARNING"

    msg = (
        f"{icon} *{sev_label}: {component}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Статус: {status_detail}\n"
        f"Время: {_now_utc_str()}\n"
    )
    if impact:
        msg += f"Влияние: {impact}\n"
    if action:
        msg += f"Действие: `{action}`\n"
    return msg


def _build_bot_down_alert(last_ok_ago: float) -> str:
    bot_url = config.DUMALKA_BOT_URL
    msg = (
        f"🔴 *CRITICAL: Связь с ботом потеряна*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"URL: `{bot_url}`\n"
        f"Последний контакт: {_fmt_duration(last_ok_ago)} назад\n"
        f"Порог: {_fmt_duration(config.BOT_UNREACHABLE_ALERT_SEC)}\n"
        f"Время: {_now_utc_str()}\n"
        f"Влияние: {COMPONENT_META['bot_connection']['impact']}\n"
        f"Действие: {COMPONENT_META['bot_connection']['action']}\n"
    )
    return msg


def _build_recovery_alert(component: str, down_duration: float) -> str:
    msg = (
        f"🟢 *ВОССТАНОВЛЕНО: {component}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Было недоступно: {_fmt_duration(down_duration)}\n"
        f"Восстановлено: {_now_utc_str()}\n"
    )
    return msg


def _collect_task_heartbeats() -> dict[str, dict]:
    """Import heartbeats from all tracked modules — same sources as /health."""
    result: dict[str, dict] = {}
    try:
        from position_tracker import _heartbeat as pt_hb
        result["position_tracker"] = pt_hb
    except Exception:
        result["position_tracker"] = {}
    try:
        from watchlist_scanner import _heartbeat as ws_hb
        result["watchlist_scanner"] = ws_hb
    except Exception:
        result["watchlist_scanner"] = {}
    try:
        import exit_quality
        result["exit_quality"] = exit_quality._heartbeat
    except Exception:
        result["exit_quality"] = {}

    try:
        from main import _task_heartbeats
        for key in ("analytics_precompute", "bybit_pnl_refresh", "ml_labeler",
                     "kline_collector", "scout"):
            result[key] = _task_heartbeats.get(key, {})
    except Exception:
        pass

    return result


def _check_task_status(name: str, hb: dict, now: float) -> str | None:
    """Return status string if unhealthy, None if OK."""
    threshold = STALE_THRESHOLDS.get(name)
    if threshold is None:
        return None
    if not hb:
        return "not_started"
    if "last_error" in hb and "last_success" not in hb:
        return f"error: {hb.get('error', 'unknown')}"
    last = hb.get("last_success", 0)
    age = now - last
    if age > threshold:
        return f"STALE ({_fmt_duration(age)}, порог {_fmt_duration(threshold)})"
    return None


def _check_extra_components() -> dict[str, str | None]:
    """Check sentinel and bybit_ws — return status string if unhealthy."""
    results: dict[str, str | None] = {}
    try:
        from core.sentinel import get_sentinel_status
        s = get_sentinel_status()
        if not s.get("active", False):
            results["sentinel"] = s.get("reason", "inactive")
        else:
            results["sentinel"] = None
    except Exception:
        results["sentinel"] = None

    try:
        from core.bybit_ws import get_price_feed_status
        ws = get_price_feed_status()
        if not ws.get("active", False):
            results["bybit_ws"] = ws.get("reason", "inactive")
        else:
            results["bybit_ws"] = None
    except Exception:
        results["bybit_ws"] = None

    return results


async def _maybe_alert(component: str, status_detail: str, now: float):
    """Send TG alert if not already alerted within cooldown window."""
    meta = COMPONENT_META.get(component, {})
    severity = meta.get("severity", SEVERITY_INFO)

    if severity == SEVERITY_INFO:
        if component not in _alert_state:
            logger.info("Watchdog: %s — %s (INFO, без TG-алерта)", component, status_detail)
            _alert_state[component] = {"alerted_at": now, "down_since": now}
        return

    state = _alert_state.get(component)
    if state is None:
        _alert_state[component] = {"alerted_at": now, "down_since": now}
        if component == "bot_connection":
            from position_tracker import _bot_last_success_ts
            last_ok_ago = now - _bot_last_success_ts if _bot_last_success_ts > 0 else 0
            msg = _build_bot_down_alert(last_ok_ago)
        else:
            msg = _build_down_alert(component, status_detail)
        logger.warning("Watchdog ALERT: %s — %s", component, status_detail)
        await send_telegram_message(msg, parse_mode="Markdown")
    else:
        elapsed = now - state["alerted_at"]
        if elapsed >= config.WATCHDOG_ALERT_COOLDOWN_SEC:
            state["alerted_at"] = now
            down_for = _fmt_duration(now - state["down_since"])
            if component == "bot_connection":
                from position_tracker import _bot_last_success_ts
                last_ok_ago = now - _bot_last_success_ts if _bot_last_success_ts > 0 else 0
                msg = _build_bot_down_alert(last_ok_ago)
            else:
                msg = _build_down_alert(component, f"{status_detail} (недоступно {down_for})")
            logger.warning("Watchdog RE-ALERT: %s — %s (down %s)", component, status_detail, down_for)
            await send_telegram_message(msg, parse_mode="Markdown")


async def _maybe_recovery(component: str, now: float):
    """Send recovery TG alert if component was previously down."""
    state = _alert_state.pop(component, None)
    if state is None:
        return
    meta = COMPONENT_META.get(component, {})
    severity = meta.get("severity", SEVERITY_INFO)
    down_duration = now - state["down_since"]
    if severity == SEVERITY_INFO:
        logger.info("Watchdog: %s recovered after %s (INFO)", component, _fmt_duration(down_duration))
        return
    msg = _build_recovery_alert(component, down_duration)
    logger.info("Watchdog RECOVERY: %s after %s", component, _fmt_duration(down_duration))
    await send_telegram_message(msg, parse_mode="Markdown")


_STARTUP_GRACE_SEC = 180  # ignore "not_started" for 3 min after watchdog boot


async def health_watchdog_loop():
    """Main watchdog loop — runs every WATCHDOG_INTERVAL_SEC."""
    await asyncio.sleep(90)  # let all tasks post their first heartbeat
    _boot_time = time.time()
    logger.info(
        "🩺 Health Watchdog started (cycle=%ds, bot_alert=%ds, cooldown=%ds)",
        WATCHDOG_INTERVAL_SEC,
        config.BOT_UNREACHABLE_ALERT_SEC,
        config.WATCHDOG_ALERT_COOLDOWN_SEC,
    )

    while True:
        try:
            now = time.time()
            _in_startup_grace = (now - _boot_time) < _STARTUP_GRACE_SEC

            # 1. Check heartbeat-tracked tasks
            heartbeats = _collect_task_heartbeats()
            for task_name in STALE_THRESHOLDS:
                hb = heartbeats.get(task_name, {})
                status = _check_task_status(task_name, hb, now)
                if status is not None:
                    if _in_startup_grace and status == "not_started":
                        continue
                    await _maybe_alert(task_name, status, now)
                else:
                    await _maybe_recovery(task_name, now)

            # 2. Check sentinel + bybit_ws
            if not _in_startup_grace:
                extra = _check_extra_components()
                for comp, status in extra.items():
                    if status is not None:
                        await _maybe_alert(comp, status, now)
                    else:
                        await _maybe_recovery(comp, now)

            # 3. Check bot connectivity
            try:
                from position_tracker import _bot_last_success_ts, _bot_sync_failed
                if _bot_last_success_ts > 0:
                    bot_age = now - _bot_last_success_ts
                    if bot_age > config.BOT_UNREACHABLE_ALERT_SEC:
                        await _maybe_alert(
                            "bot_connection",
                            f"недоступен {_fmt_duration(bot_age)}",
                            now,
                        )
                    else:
                        await _maybe_recovery("bot_connection", now)
                elif _bot_sync_failed and not _in_startup_grace:
                    await _maybe_alert(
                        "bot_connection",
                        "sync не был успешен с момента запуска",
                        now,
                    )
            except Exception as e:
                logger.debug("Watchdog: bot check error: %s", e)

            _heartbeat["last_success"] = time.time()

        except Exception as e:
            logger.error("Health watchdog cycle error: %s", e, exc_info=True)
            _heartbeat["last_error"] = time.time()
            _heartbeat["error"] = str(e)[:100]

        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
