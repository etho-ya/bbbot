import asyncio
import json
import re
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User
from app.core.config import settings, get_telegram_session_path
from app.services.trade_manager import trade_manager
from app.core.logger import logger
from app.core.bot_state import bot_state
from app.services.llm_validator import validate_trade_signal, monitor_auxiliary_signal
from app.services.llm_parser import parse_signal_with_llm
from app.services.signal_parser import classify_message, generate_signal_hash, parse_signal
from app.services.telegram_notifier import notifier
from app.services.bybit_client import bybit_client

from app.models.db_models import SignalDB, ChannelDB, SettingsDB, UserDB
from app.core.database import async_session
from app.core.security import decrypt_key
from sqlalchemy import select, update, func
from datetime import datetime, timedelta

# Active channels: normalized_id -> List[{"user_id": int, "title": str, "keywords": str}]
ACTIVE_CHANNELS: dict[str, list[dict]] = {}

# Signal bot filter config (loaded from DB)
SIGNAL_BOT_IDS: set[int] = set()
SIGNAL_BOT_USERNAMES: set[str] = set()

def _build_proxy():
    raw = settings.TELEGRAM_PROXY
    if not raw:
        return None
    try:
        import socks
        # Format: host:port or user:pass@host:port
        if "@" in raw:
            creds, hostport = raw.rsplit("@", 1)
            user, password = creds.split(":", 1)
            host, port = hostport.rsplit(":", 1)
            return (socks.SOCKS5, host, int(port), True, user, password)
        else:
            host, port = raw.rsplit(":", 1)
            return (socks.SOCKS5, host, int(port))
    except Exception:
        logger.warning(f"Telegram: Invalid TELEGRAM_PROXY format, ignoring")
        return None

client = TelegramClient(
    get_telegram_session_path(),
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH,
    proxy=_build_proxy(),
)


async def start_telegram_client():
    """Start Telethon client. If session is not authorized, do not block on input — user must run create_session.py."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        logger.warning(
            "Telegram: Session not authorized. Run from project root: venv/bin/python3 create_session.py "
            "(set TELEGRAM_PHONE in .env so the code is sent to the Telegram app)"
        )
        return
    logger.info("Telegram: Client started")
    await client.run_until_disconnected()


def normalize_channel_id(channel_id: int | str) -> str:
    str_id = str(channel_id)
    if str_id.startswith("-"):
        str_id = str_id[1:]
    if str_id.startswith("100") and len(str_id) > 10:
        str_id = str_id[3:]
    return str_id


async def load_channels():
    global ACTIVE_CHANNELS
    try:
        async with async_session() as session:
            result = await session.execute(select(ChannelDB))
            channels = result.scalars().all()

            ACTIVE_CHANNELS = {}
            for ch in channels:
                normalized = normalize_channel_id(ch.channel_id)
                if normalized not in ACTIVE_CHANNELS:
                    ACTIVE_CHANNELS[normalized] = []
                ACTIVE_CHANNELS[normalized].append({
                    "user_id": ch.user_id,
                    "title": ch.title or f"ID: {ch.channel_id}",
                    "keywords": ch.signal_keywords,
                })

        logger.info(f"Telegram: Loaded {len(ACTIVE_CHANNELS)} monitored channels")
    except Exception as e:
        logger.error(f"Telegram: Channel load error: {e}")


async def load_signal_bot_config():
    """Load signal bot filter from DB settings."""
    global SIGNAL_BOT_IDS, SIGNAL_BOT_USERNAMES
    try:
        async with async_session() as session:
            result = await session.execute(select(SettingsDB))
            all_settings = result.scalars().all()
            SIGNAL_BOT_IDS = set()
            SIGNAL_BOT_USERNAMES = set()
            for s in all_settings:
                if s.signal_bot_id:
                    try:
                        SIGNAL_BOT_IDS.add(int(s.signal_bot_id))
                    except ValueError:
                        pass
                if s.signal_bot_username:
                    SIGNAL_BOT_USERNAMES.add(s.signal_bot_username.lower().lstrip("@"))
        logger.info(f"Telegram: Signal bot filter: IDs={SIGNAL_BOT_IDS}, usernames={SIGNAL_BOT_USERNAMES}")
    except Exception as e:
        logger.error(f"Telegram: Bot config load error: {e}")


async def get_channel_title(channel_id: int) -> str:
    try:
        entity = await client.get_entity(channel_id)
        if isinstance(entity, (Channel, Chat)):
            return entity.title
    except Exception as e:
        logger.warning(f"Cannot get channel title for {channel_id}: {e}")
    return f"ID: {channel_id}"


def is_channel_active(chat_id: int | str) -> tuple[bool, str | None]:
    normalized = normalize_channel_id(chat_id)
    if normalized in ACTIVE_CHANNELS:
        return True, normalized
    return False, None


def is_from_signal_bot(sender) -> bool:
    """Check if the message sender is a configured signal bot."""
    if not sender:
        return False

    # Must be a bot
    is_bot = getattr(sender, 'bot', False)
    if not is_bot:
        return False

    # If no specific bot configured, accept any bot
    if not SIGNAL_BOT_IDS and not SIGNAL_BOT_USERNAMES:
        return True

    sender_id = getattr(sender, 'id', None)
    sender_username = getattr(sender, 'username', None)

    if sender_id and sender_id in SIGNAL_BOT_IDS:
        return True
    if sender_username and sender_username.lower() in SIGNAL_BOT_USERNAMES:
        return True

    return False


async def get_llm_settings(user_id: int):
    """Get LLM API key, model, and enabled status from user settings or global config."""
    async with async_session() as session:
        result = await session.execute(select(SettingsDB).where(SettingsDB.user_id == user_id))
        settings_db = result.scalar_one_or_none()
        
        # Use DB settings if available and have a key, otherwise fallback to .env
        api_key = decrypt_key(settings_db.openrouter_api_key) if settings_db and settings_db.openrouter_api_key else settings.OPENROUTER_API_KEY
        model = settings_db.openrouter_model if settings_db and settings_db.openrouter_model else settings.OPENROUTER_MODEL
        enabled = settings_db.llm_validation_enabled if settings_db and settings_db.llm_validation_enabled is not None else settings.LLM_VALIDATION_ENABLED
        
        # If still no key, LLM cannot be used, regardless of 'enabled' flag
        if not api_key:
            return None, None, False
            
        return api_key, model, enabled


def _parse_signal_generated_at(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value
    return None


@client.on(events.NewMessage())
async def handle_signal(event):
    """Main handler for all incoming Telegram messages."""
    chat_id = event.chat_id
    sender = await event.get_sender()

    # Check if message is from DM with a bot OR from an active channel
    is_dm = event.is_private
    is_active_channel, normalized_id = is_channel_active(chat_id)

    # For DMs: must be from a signal bot
    if is_dm:
        if not is_from_signal_bot(sender):
            return
        # Use chat_id as channel identifier for DMs
        normalized_id = str(getattr(sender, 'id', chat_id))
    elif is_active_channel:
        # For channels: also check if sender is a bot (if configured)
        if SIGNAL_BOT_IDS or SIGNAL_BOT_USERNAMES:
            if not is_from_signal_bot(sender):
                return
    else:
        return

    message_text = event.message.text
    if not message_text:
        return

    sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'title', '')
    sender_username = getattr(sender, 'username', '')
    logger.info(f"Telegram: Message from [{sender_name}/@{sender_username}]: {message_text[:100]}...")

    msg_type = classify_message(message_text)
    if msg_type == "ignore":
        logger.debug(f"Telegram: Message classified as 'ignore', skipping")
        return

    sig_hash = generate_signal_hash(message_text)

    # Determine which users to process for
    if is_dm:
        # DM: process for all users that have this bot configured
        async with async_session() as session:
            result = await session.execute(select(SettingsDB))
            all_settings = result.scalars().all()
            user_configs = [{"user_id": s.user_id, "title": f"Bot: @{sender_username}", "keywords": None} for s in all_settings]
            if not user_configs:
                user_configs = [{"user_id": 1, "title": f"Bot: @{sender_username}", "keywords": None}]
    else:
        user_configs = ACTIVE_CHANNELS.get(normalized_id, [])

    for user_config in user_configs:
        user_id = user_config["user_id"]
        keywords_raw = user_config.get("keywords")

        # Keyword filter (if configured)
        if keywords_raw:
            keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
            if keywords and not any(k in message_text.lower() for k in keywords):
                continue

        async with async_session() as session:
            # Dedup
            stmt = select(SignalDB).where(SignalDB.user_id == user_id, SignalDB.signal_hash == sig_hash)
            result = await session.execute(stmt)
            if result.scalar_one_or_none():
                continue

            # Try LLM parsing if enabled
            api_key, model, llm_user_enabled = await get_llm_settings(user_id)
            parsed_data = None
            if settings.LLM_ENABLED and llm_user_enabled and api_key:
                parsed_data = await parse_signal_with_llm(message_text, api_key, model)
                if parsed_data:
                    logger.info(f"Signal [User {user_id}]: Parsed using LLM ({model})")
                else:
                    logger.warning(f"Signal [User {user_id}]: LLM parsing failed, falling back to regex")

            # Fallback to regex parsing
            if not parsed_data:
                parsed_data = parse_signal(message_text)

        signal_generated_at = datetime.utcnow()
        if parsed_data and not parsed_data.get("signal_generated_at"):
            parsed_data["signal_generated_at"] = signal_generated_at.isoformat()

        # Save signal
        if parsed_data:
            db_signal = SignalDB(
                user_id=user_id,
                raw_text=message_text,
                parsed_data=json.dumps(parsed_data, ensure_ascii=False) if parsed_data else None,
                channel_id=normalized_id,
                channel_title=user_config.get("title", f"Bot: @{sender_username}"),
                signal_hash=sig_hash,
                signal_type=msg_type,
                is_processed=False,
            )
            session.add(db_signal)
            await session.commit()
            await session.refresh(db_signal)

        if not parsed_data or not bot_state.is_running:
            continue

        bot_state.increment_signals()

        if msg_type == "trade":
            await _handle_trade_signal(user_id, parsed_data, message_text, db_signal)
        elif msg_type == "auxiliary":
            await _handle_auxiliary_signal(user_id, parsed_data, message_text, db_signal)


async def _handle_auxiliary_signal(user_id: int, parsed_data: dict, raw_text: str, db_signal: SignalDB):
    """Process auxiliary signal. When Dumalka is active, it owns all position
    management -- auxiliary signals are logged but not acted upon by the bot."""
    symbol = parsed_data.get("symbol", "???")
    logger.info(f"Signal [User {user_id}]: Auxiliary signal for {symbol}")

    # When Dumalka manages positions, don't let LLM-driven auxiliary actions
    # interfere -- Dumalka already has zone policy, MC oracle, time-decay etc.
    if settings.DUMALKA_MODE == "active":
        logger.info(f"Signal [User {user_id}]: Auxiliary signal for {symbol} ignored (DUMALKA_MODE=active)")
        return

    api_key, model, llm_user_enabled = await get_llm_settings(user_id)
    if not (settings.LLM_ENABLED and llm_user_enabled and api_key):
        logger.debug(f"Signal [User {user_id}]: LLM disabled, skipping auxiliary")
        return

    open_positions = trade_manager.get_open_positions_for_llm()
    if not open_positions:
        return

    try:
        result = await monitor_auxiliary_signal(
            raw_text=raw_text,
            signal_data=parsed_data,
            open_positions=open_positions,
            api_key=api_key,
            model=model,
        )
        action = result.get("action", "do_nothing")
        reason = result.get("reason", "")

        async with async_session() as session:
            from sqlalchemy import update as sql_update
            await session.execute(
                sql_update(SignalDB).where(SignalDB.id == db_signal.id).values(
                    llm_decision=action,
                    llm_reason=reason,
                    llm_model=model,
                )
            )
            await session.commit()

        if action != "do_nothing":
            await trade_manager.handle_auxiliary_action(action, symbol, reason)
    except Exception as e:
        logger.error(f"Auxiliary [User {user_id}]: Error: {e}", exc_info=True)


async def _handle_trade_signal(user_id: int, parsed_data: dict, raw_text: str, db_signal: SignalDB):
    """Process a trade signal: query RE for approval, then open if approved."""
    symbol = parsed_data.get("symbol", "???")
    side = parsed_data.get("side", "???")

    logger.info(f"Signal [User {user_id}]: Trade signal: {symbol} {side}")

    from app.main import manager
    asyncio.create_task(manager.broadcast({
        "type": "signal",
        "user_id": user_id,
        "symbol": symbol,
        "side": side,
        "entry": parsed_data.get("entry_price", 0),
    }))

    conviction_size_usd = None
    re_score = None
    auto_be_price = None
    auto_be_trigger = None
    entry_order_type = "Market"
    entry_order_price = None
    entry_order_ttl = settings.ENTRY_LIMIT_ORDER_TTL_SECONDS

    signal_generated_at = _parse_signal_generated_at(parsed_data.get("signal_generated_at")) if parsed_data else None
    if signal_generated_at:
        signal_age_sec = (datetime.utcnow() - signal_generated_at).total_seconds()
    else:
        signal_age_sec = 0.0

    if signal_age_sec > settings.SIGNAL_STALE_TTL_SECONDS:
        await notifier.notify_stale_signal(symbol, side, signal_age_sec)
        async with async_session() as session:
            await session.execute(
                update(SignalDB).where(SignalDB.id == db_signal.id).values(
                    re_status="timeout",
                    re_reason="signal stale",
                    re_responded_at=datetime.utcnow(),
                )
            )
            await session.commit()
        return

    # === RISK ENGINE: Query RE for approval + conviction sizing ===
    # Dumalka must always be enabled. If RISK_ENGINE_ENABLED is somehow off,
    # block all trades — the bot never trades without RE approval.
    if not settings.RISK_ENGINE_ENABLED:
        logger.error(f"Signal [User {user_id}]: RISK_ENGINE_ENABLED=false, trade blocked for {symbol}")
        await notifier.notify_error(f"RISK_ENGINE_ENABLED=false: {symbol} {side} заблокирован. Включите RE.")
        return

    sig_hash = db_signal.signal_hash
    entry = parsed_data.get("entry_price", 0)
    tp1 = parsed_data.get("tp1", 0)
    tp2 = parsed_data.get("tp2", 0)
    tp3 = parsed_data.get("tp3", 0)
    sl = parsed_data.get("sl", 0)
    metadata = parsed_data.get("metadata", {})
    trend = "bullish" if side == "Buy" else "bearish"

    async with async_session() as session:
        await session.execute(
            update(SignalDB).where(SignalDB.id == db_signal.id).values(re_status="pending")
        )
        await session.commit()

    re_rec = None

    try:
        import httpx
        re_side = "long" if side == "Buy" else "short"

        # Calculate intended token count so RE can compute proper conviction_size_usd
        api_key, api_secret, testnet = await trade_manager.get_user_credentials(user_id)
        db_settings = await trade_manager.get_db_settings(user_id)
        leverage = db_settings.leverage if db_settings else settings.DEFAULT_LEVERAGE
        balance = 0.0
        if api_key:
            balance = await bybit_client.get_wallet_balance(api_key, api_secret, testnet)
        base_margin = 10.0
        base_notional = base_margin * leverage
        intended_size = base_notional / entry if entry > 0 else 1.0
        logger.info(
            f"Signal [User {user_id}]: RE sizing input: {symbol} "
            f"balance=${balance:.2f} base_margin=${base_margin:.2f} "
            f"lev={leverage}x notional=${base_notional:.2f} "
            f"entry={entry} → size={intended_size:.6f} tokens"
        )

        situation = parsed_data.get("situation", "")
        recommendation_text = parsed_data.get("recommendation", "")
        midas_comment = f"{situation}\n{recommendation_text}".strip() or None

        re_payload = {
            "symbol": symbol, "side": re_side,
            "size": round(intended_size, 6),
            "source": "bot_direct",
            "signal_hash": sig_hash,
            "signal_generated_at": parsed_data.get("signal_generated_at"),
            "entry_low": entry,
            "entry_high": entry,
            "stop_loss": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "risk_reward": metadata.get("risk_reward"),
            "probability": metadata.get("probability"),
            "win_rate": metadata.get("win_rate"),
            "trend": trend,
            "volume_level": metadata.get("volume_level"),
            "midas_comment": midas_comment,
            "leverage": leverage,
            "equity": round(balance, 2),
        }
        async with httpx.AsyncClient(timeout=25.0, verify=False) as http:
            resp = await http.post(
                settings.RE_URL + "/tv-webhook",
                json=re_payload,
                headers={"X-Webhook-Secret": settings.RE_WEBHOOK_SECRET},
            )
            if resp.status_code == 200:
                re_data = resp.json()
                re_approved = re_data.get("approved", False)
                re_rec = re_data.get("recommendation", "reject" if not re_approved else "approve")
                re_score_val = re_data.get("signal_score", 0)
                conviction_size_usd = re_data.get("conviction_size_usd")
                auto_be_price = re_data.get("auto_be_price")
                auto_be_trigger = re_data.get("auto_be_trigger")
                rejection_reason = re_data.get("rejection_reason")

                re_status = "approved" if re_approved else "rejected"
                async with async_session() as session:
                    await session.execute(
                        update(SignalDB).where(SignalDB.id == db_signal.id).values(
                            re_status=re_status,
                            re_score=re_score_val,
                            re_reason=f"rec={re_rec} conviction=${conviction_size_usd} reason={rejection_reason}",
                            re_responded_at=datetime.utcnow(),
                        )
                    )
                    await session.commit()

                # Send full signal report to the report group (observability)
                asyncio.create_task(notifier.notify_signal_report(
                    chat_id=settings.RE_REPORT_CHAT_ID,
                    symbol=symbol, side=side,
                    entry=entry, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
                    sig_hash=sig_hash,
                    re_decision="approve" if re_approved else "reject",
                    re_score=re_score_val,
                    conviction_usd=conviction_size_usd if re_approved else None,
                    rejection_reason=rejection_reason if not re_approved else None,
                    metadata=metadata,
                    situation=parsed_data.get("situation"),
                    recommendation=parsed_data.get("recommendation"),
                ))

                if not re_approved:
                    logger.info(
                        f"RE [User {user_id}]: REJECTED {symbol} {side} "
                        f"(score={re_score_val:.2f}, reason={rejection_reason})"
                    )
                    await notifier.notify_re_decision(
                        symbol, "reject", f"Score={re_score_val:.2f}, reason={rejection_reason}"
                    )
                    return

                re_score = re_score_val
                logger.info(
                    f"RE [User {user_id}]: {re_rec.upper()} {symbol} "
                    f"score={re_score_val:.2f} conviction=${conviction_size_usd}"
                )
            elif resp.status_code == 403:
                logger.error(f"RE [User {user_id}]: 403 Forbidden for {symbol} — check RE_WEBHOOK_SECRET")
                async with async_session() as session:
                    await session.execute(
                        update(SignalDB).where(SignalDB.id == db_signal.id).values(
                            re_status="error", re_reason="403 Forbidden",
                            re_responded_at=datetime.utcnow(),
                        )
                    )
                    await session.commit()
                await notifier.notify_error(f"RE 403 Forbidden: проверьте RE_WEBHOOK_SECRET")
                return
            else:
                logger.warning(
                    f"RE [User {user_id}]: HTTP {resp.status_code} for {symbol}, "
                    f"fallback 30% sizing"
                )
                re_rec = "approve"
                re_score = 0.3
                conviction_size_usd = None
    except Exception as e:
        logger.warning(f"RE [User {user_id}]: Query failed ({e}) for {symbol}, fallback 30% sizing")
        async with async_session() as session:
            await session.execute(
                update(SignalDB).where(SignalDB.id == db_signal.id).values(
                    re_status="timeout", re_reason=str(e)[:200],
                    re_responded_at=datetime.utcnow(),
                )
            )
            await session.commit()
        re_rec = "approve"
        re_score = 0.3
        conviction_size_usd = None
        await notifier.notify_error(f"RE недоступен: {symbol} {side} открыт с 30% размером ({e})")

    # === LLM validation (secondary gate) ===
    api_key, model, llm_user_enabled = await get_llm_settings(user_id)

    if settings.LLM_ENABLED and llm_user_enabled and api_key:
        current_price = await bybit_client.get_current_price(symbol) or 0
        llm_result = await validate_trade_signal(
            raw_text=raw_text,
            parsed_data=parsed_data,
            current_price=current_price,
            api_key=api_key,
            model=model,
        )

        async with async_session() as session:
            await session.execute(
                update(SignalDB).where(SignalDB.id == db_signal.id).values(
                    llm_decision=llm_result["decision"],
                    llm_reason=llm_result["reason"],
                    llm_model=model,
                )
            )
            await session.commit()

        if not llm_result.get("parsing_correct", True) and llm_result.get("corrections"):
            for field, value in llm_result["corrections"].items():
                if field in parsed_data:
                    try:
                        parsed_data[field] = type(parsed_data[field])(value)
                    except (ValueError, TypeError):
                        pass

        if llm_result["decision"] == "reject":
            logger.info(f"LLM [User {user_id}]: REJECTED {symbol} {side}: {llm_result['reason']}")
            await notifier.notify_llm_rejected(symbol, side, llm_result["reason"])
            return

    # === Drift guard => prefer limit entry on stale zones ===
    entry_low = parsed_data.get("entry_low", entry)
    entry_high = parsed_data.get("entry_high", entry)
    try:
        entry_low = float(entry_low)
        entry_high = float(entry_high)
    except (TypeError, ValueError):
        entry_low = entry
        entry_high = entry

    try:
        current_price = await bybit_client.get_current_price(symbol) or 0.0
        if current_price:
            drift_max = settings.ENTRY_DRIFT_MAX_PCT
            if side == "Buy" and entry_high > 0 and current_price >= entry_high * (1 + drift_max):
                entry_order_type = "Limit"
                entry_order_price = entry_high
                logger.info(
                    f"Signal [User {user_id}]: Drift guard BUY {symbol}: "
                    f"market={current_price:.8f} > {entry_high:.8f}*(1+{drift_max}) — placing LIMIT"
                )
            elif side == "Sell" and entry_low > 0 and current_price <= entry_low * (1 - drift_max):
                entry_order_type = "Limit"
                entry_order_price = entry_low
                logger.info(
                    f"Signal [User {user_id}]: Drift guard SELL {symbol}: "
                    f"market={current_price:.8f} < {entry_low:.8f}*(1-{drift_max}) — placing LIMIT"
                )
    except Exception as e:
        logger.warning(f"Signal [User {user_id}]: Drift guard check failed for {symbol}: {e}")

    # === Open trade with RE-approved sizing ===
    await _execute_trade(user_id, parsed_data, db_signal,
                         re_score=re_score,
                         re_recommendation=re_rec or "approve",
                         conviction_size_usd=conviction_size_usd,
                         auto_be_price=auto_be_price,
                         auto_be_trigger=auto_be_trigger,
                         entry_order_type=entry_order_type,
                         entry_order_price=entry_order_price,
                         entry_order_ttl=entry_order_ttl)


async def _execute_trade(
    user_id: int, parsed_data: dict, db_signal: SignalDB,
    re_score: float = None,
    re_recommendation: str = "approve",
    conviction_size_usd: float = None,
    auto_be_price: float = None,
    auto_be_trigger: float = None,
    entry_order_type: str = "Market",
    entry_order_price: float = None,
    entry_order_ttl: int = 600,
):
    """Actually open the trade on Bybit."""
    symbol = parsed_data.get("symbol", "???")
    try:
        trade_opened = await trade_manager.open_trade(
            user_id, parsed_data, db_signal.id,
            re_score=re_score, signal_hash=db_signal.signal_hash,
            re_recommendation=re_recommendation,
            conviction_size_usd=conviction_size_usd,
            auto_be_price=auto_be_price,
            auto_be_trigger=auto_be_trigger,
            entry_order_type=entry_order_type,
            entry_order_price=entry_order_price,
            entry_order_ttl=entry_order_ttl,
        )
        if trade_opened:
            async with async_session() as session:
                await session.execute(
                    update(SignalDB).where(SignalDB.id == db_signal.id).values(is_processed=True)
                )
                await session.commit()
        else:
            logger.warning(f"Signal [User {user_id}]: Failed to open trade for {symbol}")
    except Exception as e:
        logger.error(f"Trade [User {user_id}]: Critical error opening trade: {e}", exc_info=True)


async def re_timeout_checker():
    """Background task: auto-reject pending signals older than RE_APPROVAL_TIMEOUT."""
    # One-time bulk cleanup of stale pending signals from previous runs
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=settings.RE_APPROVAL_TIMEOUT)
        async with async_session() as session:
            result = await session.execute(
                select(func.count()).select_from(SignalDB).where(
                    SignalDB.re_status == "pending",
                    SignalDB.created_at < cutoff,
                )
            )
            stale_count = result.scalar() or 0
            if stale_count > 0:
                await session.execute(
                    update(SignalDB).where(
                        SignalDB.re_status == "pending",
                        SignalDB.created_at < cutoff,
                    ).values(re_status="timeout", re_responded_at=datetime.utcnow())
                )
                await session.commit()
                logger.info(f"RE Timeout: Cleaned {stale_count} stale pending signals on startup")
    except Exception as e:
        logger.error(f"RE Timeout Checker startup cleanup error: {e}")

    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=settings.RE_APPROVAL_TIMEOUT)
            async with async_session() as session:
                result = await session.execute(
                    select(SignalDB).where(
                        SignalDB.re_status == "pending",
                        SignalDB.created_at < cutoff,
                    )
                )
                expired = result.scalars().all()
                for sig in expired:
                    sig.re_status = "timeout"
                    sig.re_responded_at = datetime.utcnow()
                    parsed = json.loads(sig.parsed_data) if sig.parsed_data else {}
                    symbol = parsed.get("symbol", "???")
                    logger.warning(f"RE Timeout: {symbol} (hash={sig.signal_hash}) — no approval received")
                    await notifier.notify_re_decision(symbol, "timeout", "RE не ответил вовремя")
                if expired:
                    await session.commit()
        except Exception as e:
            logger.error(f"RE Timeout Checker error: {e}")
        await asyncio.sleep(30)
