from fastapi import FastAPI, Request, Form, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from app.core.config import settings, get_dumalka_upload_root
from app.services.telegram_listener import (
    client as tg_client, load_channels, load_signal_bot_config,
    get_channel_title, start_telegram_client, re_timeout_checker
)
from app.services.trade_manager import trade_manager
from app.services.dumalka_artifacts import (
    ensure_upload_root,
    save_context_bundle,
    read_latest_manifest,
    read_recent_manifests,
)
from app.services.bybit_client import bybit_client
from app.services.telegram_notifier import notifier
from app.models.trade_state import TradeStage
from app.models.db_models import TradeDB, ChannelDB, SignalDB, SettingsDB, UserDB
from app.core.database import async_session
from app.core.logger import logger
from app.core.bot_state import bot_state
from app.core.security import encrypt_key, decrypt_key
from telethon.tl.types import Channel, Chat
import asyncio
import os
import json
import time
from datetime import datetime
from sqlalchemy import delete, select, desc, update
from typing import List, Literal, Optional

app = FastAPI(title="Midas Trading Bot")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ─── Startup ──────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    os.makedirs("logs", exist_ok=True)
    ensure_upload_root(get_dumalka_upload_root())
    await trade_manager.init_db()

    async with async_session() as session:
        # 1. Default admin
        result = await session.execute(select(UserDB).where(UserDB.username == "admin"))
        admin = result.scalar_one_or_none()
        if not admin:
            logger.info("Main: Creating admin user...")
            admin = UserDB(
                username="admin",
                password_hash="single_user_no_auth",
                bybit_api_key=encrypt_key(settings.BYBIT_API_KEY) if settings.BYBIT_API_KEY else None,
                bybit_api_secret=encrypt_key(settings.BYBIT_API_SECRET) if settings.BYBIT_API_SECRET else None,
                is_testnet=settings.BYBIT_TESTNET,
            )
            session.add(admin)
        else:
            # Всегда подставляем ключи из .env в БД, чтобы баланс и торговля работали
            updated = False
            if settings.BYBIT_API_KEY:
                new_key = encrypt_key(settings.BYBIT_API_KEY)
                if admin.bybit_api_key != new_key:
                    admin.bybit_api_key = new_key
                    updated = True
            if settings.BYBIT_API_SECRET:
                new_secret = encrypt_key(settings.BYBIT_API_SECRET)
                if admin.bybit_api_secret != new_secret:
                    admin.bybit_api_secret = new_secret
                    updated = True
            if admin.is_testnet != settings.BYBIT_TESTNET:
                admin.is_testnet = settings.BYBIT_TESTNET
                updated = True
            if updated:
                logger.info("Main: Updated admin Bybit credentials from .env")

        await session.commit()
        if admin:
            await session.refresh(admin)

        # 2. Bind orphan records
        await session.execute(update(TradeDB).where(TradeDB.user_id == None).values(user_id=admin.id))
        await session.execute(update(ChannelDB).where(ChannelDB.user_id == None).values(user_id=admin.id))
        await session.execute(update(SignalDB).where(SignalDB.user_id == None).values(user_id=admin.id))
        await session.execute(update(SettingsDB).where(SettingsDB.user_id == None).values(user_id=admin.id))
        await session.commit()

        # 3. Init or sync admin settings
        result = await session.execute(select(SettingsDB).where(SettingsDB.user_id == admin.id))
        existing_settings = result.scalar_one_or_none()
        if not existing_settings:
            logger.info("Main: Creating admin settings...")
            new_settings = SettingsDB(
                user_id=admin.id,
                deposit_percent=settings.DEPOSIT_PERCENT,
                leverage=settings.DEFAULT_LEVERAGE,
                max_price_deviation=settings.MAX_PRICE_DEVIATION,
                default_stop_loss_percent=settings.DEFAULT_STOP_LOSS_PERCENT,
                max_open_positions=settings.MAX_OPEN_POSITIONS,
                max_daily_loss_percent=settings.MAX_DAILY_LOSS_PERCENT,
                openrouter_api_key=encrypt_key(settings.OPENROUTER_API_KEY) if settings.OPENROUTER_API_KEY else None,
                openrouter_model=settings.OPENROUTER_MODEL,
                llm_validation_enabled=settings.LLM_VALIDATION_ENABLED,
                signal_bot_id=settings.SIGNAL_BOT_ID,
                signal_bot_username=settings.SIGNAL_BOT_USERNAME,
                telegram_notify_chat_id=settings.TELEGRAM_NOTIFY_CHAT_ID,
            )
            session.add(new_settings)
            await session.commit()
        else:
            # v0.10.2: Sync critical trade parameters from .env to DB on every startup.
            # Fixes: changing MAX_DAILY_LOSS_PERCENT in .env had no effect because
            # the old value was already persisted in DB (e.g. 10% vs 15%).
            synced_fields = []
            if existing_settings.deposit_percent != settings.DEPOSIT_PERCENT:
                existing_settings.deposit_percent = settings.DEPOSIT_PERCENT
                synced_fields.append(f"deposit_percent={settings.DEPOSIT_PERCENT}")
            if existing_settings.leverage != settings.DEFAULT_LEVERAGE:
                existing_settings.leverage = settings.DEFAULT_LEVERAGE
                synced_fields.append(f"leverage={settings.DEFAULT_LEVERAGE}")
            if existing_settings.max_price_deviation != settings.MAX_PRICE_DEVIATION:
                existing_settings.max_price_deviation = settings.MAX_PRICE_DEVIATION
                synced_fields.append(f"max_price_deviation={settings.MAX_PRICE_DEVIATION}")
            if existing_settings.default_stop_loss_percent != settings.DEFAULT_STOP_LOSS_PERCENT:
                existing_settings.default_stop_loss_percent = settings.DEFAULT_STOP_LOSS_PERCENT
                synced_fields.append(f"default_sl_pct={settings.DEFAULT_STOP_LOSS_PERCENT}")
            if existing_settings.max_open_positions != settings.MAX_OPEN_POSITIONS:
                existing_settings.max_open_positions = settings.MAX_OPEN_POSITIONS
                synced_fields.append(f"max_positions={settings.MAX_OPEN_POSITIONS}")
            if existing_settings.max_daily_loss_percent != settings.MAX_DAILY_LOSS_PERCENT:
                existing_settings.max_daily_loss_percent = settings.MAX_DAILY_LOSS_PERCENT
                synced_fields.append(f"max_daily_loss={settings.MAX_DAILY_LOSS_PERCENT}%")
            if synced_fields:
                await session.commit()
                logger.info(f"Main: Synced .env → DB settings: {', '.join(synced_fields)}")

        # 4. Configure notifier (uses Telethon client, configured after client starts)
        # Will be configured in start_telegram_client callback

    await load_channels()
    await load_signal_bot_config()

    # Restore active trades
    async with async_session() as session:
        result = await session.execute(select(TradeDB).where(TradeDB.status == "active"))
        rows = result.scalars().all()
        for db_trade in rows:
            from app.models.trade_state import TradeState, TradeSide
            # Look up signal_hash from the linked signal
            sig_hash = None
            if db_trade.signal_id:
                sig_result = await session.execute(
                    select(SignalDB.signal_hash).where(SignalDB.id == db_trade.signal_id)
                )
                sig_hash = sig_result.scalar_one_or_none()
            trade = TradeState(
                id=db_trade.id,
                user_id=db_trade.user_id,
                symbol=db_trade.symbol,
                side=TradeSide(db_trade.side),
                entry_price=db_trade.entry_price,
                size=db_trade.size,
                leverage=db_trade.leverage,
                tp1=db_trade.tp1, tp2=db_trade.tp2, tp3=db_trade.tp3,
                sl=db_trade.sl,
                trailing_stop_pct=db_trade.trailing_stop_pct,
                signal_hash=sig_hash,
                stage=TradeStage(db_trade.stage),
                position_id=db_trade.position_id,
                created_at=db_trade.created_at,
                updated_at=db_trade.updated_at,
            )
            trade_manager.active_trades[trade.id] = trade
            asyncio.create_task(trade_manager.monitor_trade(trade.id))
            logger.info(f"Main: Restored trade {trade.symbol} (stage={trade.stage.name})")

    async def _start_tg_and_configure_notifier():
        if not tg_client.is_connected():
            await tg_client.connect()
        
        if await tg_client.is_user_authorized():
            async with async_session() as session:
                db_s = (await session.execute(select(SettingsDB).where(SettingsDB.user_id == 1))).scalar_one_or_none()
                chat_id = ""
                if db_s and db_s.telegram_notify_chat_id:
                    chat_id = db_s.telegram_notify_chat_id
                elif settings.TELEGRAM_NOTIFY_CHAT_ID:
                    chat_id = settings.TELEGRAM_NOTIFY_CHAT_ID
            notifier.configure(tg_client, chat_id)
            logger.info("Main: Telegram client started, notifier configured")
            active_count = len(trade_manager.active_trades)
            await notifier.notify_bot_restarted(active_count)
            await tg_client.run_until_disconnected()
        else:
            logger.warning("Main: Telegram session NOT authorized. Awaiting authorization via Web UI.")

    asyncio.create_task(_start_tg_and_configure_notifier())
    asyncio.create_task(re_timeout_checker())
    asyncio.create_task(_dumalka_watchdog())


_DUMALKA_WATCHDOG_INTERVAL = 60   # check every 60 seconds
_DUMALKA_TIMEOUT_SEC = 300        # 5 minutes without a poll = unavailable

async def _dumalka_watchdog():
    global _dumalka_last_poll, _dumalka_unavailable_alerted
    _dumalka_last_poll = time.time()
    while True:
        await asyncio.sleep(_DUMALKA_WATCHDOG_INTERVAL)
        if _dumalka_last_poll == 0:
            continue
        gap = time.time() - _dumalka_last_poll
        if gap > _DUMALKA_TIMEOUT_SEC and not _dumalka_unavailable_alerted:
            _dumalka_unavailable_alerted = True
            logger.warning(f"Dumalka watchdog: no poll for {gap:.0f}s — alerting")
            await notifier.notify_dumalka_unavailable(gap)
        elif gap <= _DUMALKA_TIMEOUT_SEC and _dumalka_unavailable_alerted:
            _dumalka_unavailable_alerted = False
            logger.info("Dumalka watchdog: polling resumed")


# ─── Pages ────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard(request: Request):
    async with async_session() as session:
        result = await session.execute(select(UserDB).where(UserDB.id == 1))
        user = result.scalar_one_or_none()
        if not user:
            return RedirectResponse(url="/settings")

        api_key = None
        api_secret = None
        balance = 0.0
        balance_error = None
        # Если в БД нет ключей, но они есть в .env — сохраняем в БД (удобно после первого запуска в Docker)
        if (not user.bybit_api_key or not user.bybit_api_secret) and settings.BYBIT_API_KEY and settings.BYBIT_API_SECRET:
            user.bybit_api_key = encrypt_key(settings.BYBIT_API_KEY)
            user.bybit_api_secret = encrypt_key(settings.BYBIT_API_SECRET)
            user.is_testnet = settings.BYBIT_TESTNET
            await session.commit()
            await session.refresh(user)
            logger.info("Main: Synced Bybit keys from .env to DB (dashboard)")
        try:
            api_key = decrypt_key(user.bybit_api_key) if user.bybit_api_key else None
            api_secret = decrypt_key(user.bybit_api_secret) if user.bybit_api_secret else None
        except Exception as e:
            logger.warning(f"Dashboard: Failed to decrypt Bybit keys: {e}")
            balance_error = "Ошибка ключей (проверьте ENCRYPTION_KEY)"
        if api_key and api_secret and not balance_error:
            try:
                balance = await bybit_client.get_wallet_balance(api_key, api_secret, user.is_testnet)
            except Exception as e:
                logger.warning(f"Dashboard: Balance request failed: {e}")
                balance_error = str(e)[:200]
        elif not api_key or not api_secret:
            balance_error = "Укажите API ключи в Настройках или в .env и перезапустите приложение"

        user_active_trades = [t for t in trade_manager.active_trades.values() if t.user_id == 1]

        result = await session.execute(select(ChannelDB).where(ChannelDB.user_id == 1))
        channels_list = result.scalars().all()

        stmt = select(TradeDB).where(TradeDB.user_id == 1).order_by(desc(TradeDB.created_at)).limit(50)
        history = (await session.execute(stmt)).scalars().all()

    available_channels = []
    selected_channel_ids = {str(ch.channel_id) for ch in channels_list}

    try:
        if tg_client.is_connected():
            async for dialog in tg_client.iter_dialogs():
                if isinstance(dialog.entity, (Channel, Chat)) and not dialog.entity.left:
                    ch_id = str(dialog.entity.id)
                    available_channels.append({
                        "id": ch_id,
                        "title": dialog.entity.title or f"ID: {ch_id}",
                        "username": getattr(dialog.entity, 'username', None),
                    })
    except Exception:
        pass

    return templates.TemplateResponse("index.html", {
        "request": request,
        "balance": balance,
        "balance_error": balance_error,
        "history": history,
        "active_trades": user_active_trades,
        "testnet": user.is_testnet,
        "channels_list": channels_list,
        "available_channels": available_channels,
        "selected_channel_ids": selected_channel_ids,
        "status": bot_state.get_status(),
        "active_tab": "history",
    })


@app.get("/signals")
async def signals_page(request: Request):
    async with async_session() as session:
        user = (await session.execute(select(UserDB).where(UserDB.id == 1))).scalar_one_or_none()
        stmt = select(SignalDB).where(SignalDB.user_id == 1).order_by(desc(SignalDB.created_at)).limit(50)
        signals = (await session.execute(stmt)).scalars().all()

    user_active_trades = [t for t in trade_manager.active_trades.values() if t.user_id == 1]
    api_key = decrypt_key(user.bybit_api_key) if user and user.bybit_api_key else None
    api_secret = decrypt_key(user.bybit_api_secret) if user and user.bybit_api_secret else None
    balance = await bybit_client.get_wallet_balance(api_key, api_secret, user.is_testnet) if api_key else 0.0

    return templates.TemplateResponse("index.html", {
        "request": request,
        "balance": balance,
        "signals": signals,
        "active_tab": "signals",
        "active_trades": user_active_trades,
        "status": bot_state.get_status(),
        "testnet": user.is_testnet if user else False,
    })


@app.get("/positions")
async def positions_page(request: Request):
    async with async_session() as session:
        user = (await session.execute(select(UserDB).where(UserDB.id == 1))).scalar_one_or_none()
        user_active_trades = [t for t in trade_manager.active_trades.values() if t.user_id == 1]
        stmt = select(TradeDB).where(TradeDB.user_id == 1, TradeDB.status == "closed").order_by(desc(TradeDB.closed_at)).limit(20)
        history = (await session.execute(stmt)).scalars().all()

    api_key = decrypt_key(user.bybit_api_key) if user and user.bybit_api_key else None
    api_secret = decrypt_key(user.bybit_api_secret) if user and user.bybit_api_secret else None
    balance = await bybit_client.get_wallet_balance(api_key, api_secret, user.is_testnet) if api_key else 0.0

    return templates.TemplateResponse("index.html", {
        "request": request,
        "balance": balance,
        "active_trades": user_active_trades,
        "history": history,
        "active_tab": "history",
        "status": bot_state.get_status(),
        "testnet": user.is_testnet if user else False,
    })


@app.get("/logs")
async def logs_page(request: Request):
    logs_content = ""
    try:
        if os.path.exists("logs/bot.log"):
            with open("logs/bot.log", "r", encoding="utf-8") as f:
                logs_content = f.read()[-10000:]
    except Exception:
        logs_content = "Error reading logs"

    async with async_session() as session:
        user = (await session.execute(select(UserDB).where(UserDB.id == 1))).scalar_one_or_none()

    user_active_trades = [t for t in trade_manager.active_trades.values() if t.user_id == 1]
    api_key = decrypt_key(user.bybit_api_key) if user and user.bybit_api_key else None
    api_secret = decrypt_key(user.bybit_api_secret) if user and user.bybit_api_secret else None
    balance = await bybit_client.get_wallet_balance(api_key, api_secret, user.is_testnet) if api_key else 0.0

    return templates.TemplateResponse("index.html", {
        "request": request,
        "balance": balance,
        "logs_content": logs_content,
        "active_tab": "logs",
        "active_trades": user_active_trades,
        "status": bot_state.get_status(),
        "testnet": user.is_testnet if user else False,
    })


# ─── Settings ─────────────────────────────────────────────────────────

@app.get("/settings")
async def settings_page(request: Request):
    async with async_session() as session:
        user = (await session.execute(select(UserDB).where(UserDB.id == 1))).scalar_one_or_none()
        db_settings = (await session.execute(select(SettingsDB).where(SettingsDB.user_id == 1))).scalar_one_or_none()

    user_active_trades = [t for t in trade_manager.active_trades.values() if t.user_id == 1]
    balance = 0.0
    balance_error = None
    try:
        api_key = decrypt_key(user.bybit_api_key) if user and user.bybit_api_key else None
        api_secret = decrypt_key(user.bybit_api_secret) if user and user.bybit_api_secret else None
        if api_key and api_secret:
            balance = await bybit_client.get_wallet_balance(api_key, api_secret, user.is_testnet)
    except Exception as e:
        balance_error = str(e)[:200]
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "balance": balance,
        "balance_error": balance_error,
        "active_trades": user_active_trades,
        "status": bot_state.get_status(),
        "settings_obj": db_settings,
        "user": user,
        "active_tab": "settings",
    })


@app.post("/settings")
async def update_settings(
    deposit_percent: float = Form(...),
    leverage: int = Form(...),
    max_price_deviation: float = Form(...),
    default_stop_loss_percent: float = Form(10.0),
    max_open_positions: int = Form(3),
    max_daily_loss_percent: float = Form(10.0),
    bybit_api_key: str = Form(None),
    bybit_api_secret: str = Form(None),
    is_testnet: bool = Form(False),
    signal_bot_id: str = Form(""),
    signal_bot_username: str = Form(""),
    llm_validation_enabled: bool = Form(False),
    openrouter_api_key: str = Form(""),
    openrouter_model: str = Form("qwen/qwen3-coder:free"),
    telegram_notify_chat_id: str = Form(""),
):
    async with async_session() as session:
        user = await session.get(UserDB, 1)
        if not user:
            return RedirectResponse(url="/settings", status_code=303)

        if bybit_api_key:
            user.bybit_api_key = encrypt_key(bybit_api_key)
        if bybit_api_secret:
            user.bybit_api_secret = encrypt_key(bybit_api_secret)
        user.is_testnet = is_testnet

        db_settings = (await session.execute(select(SettingsDB).where(SettingsDB.user_id == 1))).scalar_one_or_none()
        if db_settings:
            db_settings.deposit_percent = deposit_percent
            db_settings.leverage = leverage
            db_settings.max_price_deviation = max_price_deviation
            db_settings.default_stop_loss_percent = default_stop_loss_percent
            db_settings.max_open_positions = max_open_positions
            db_settings.max_daily_loss_percent = max_daily_loss_percent
            db_settings.signal_bot_id = signal_bot_id or None
            db_settings.signal_bot_username = signal_bot_username or None
            db_settings.llm_validation_enabled = llm_validation_enabled
            if openrouter_api_key:
                db_settings.openrouter_api_key = encrypt_key(openrouter_api_key)
            db_settings.openrouter_model = openrouter_model
            db_settings.telegram_notify_chat_id = telegram_notify_chat_id or None

        await session.commit()
        logger.info(f"Settings: Updated (leverage={leverage}, llm={llm_validation_enabled}, model={openrouter_model})")

    # Reconfigure notifier with updated chat_id
    notifier.configure(tg_client, telegram_notify_chat_id or "")

    # Reload bot filter
    await load_signal_bot_config()

    return RedirectResponse(url="/settings", status_code=303)


# ─── Channels ─────────────────────────────────────────────────────────

@app.post("/")
async def save_channels_form(channel_ids: List[str] = Form([])):
    async with async_session() as session:
        await session.execute(delete(ChannelDB).where(ChannelDB.user_id == 1))
        for ch_data in channel_ids:
            if "|" in ch_data:
                ch_id, title = ch_data.split("|", 1)
            else:
                ch_id = ch_data
                try:
                    title = await get_channel_title(int(ch_id))
                except Exception:
                    title = f"ID: {ch_id}"
            session.add(ChannelDB(user_id=1, channel_id=ch_id, title=title))
        await session.commit()

    await load_channels()
    return RedirectResponse(url="/", status_code=303)


@app.get("/channel/{channel_id}/settings")
async def get_channel_settings(request: Request, channel_id: str):
    async with async_session() as session:
        result = await session.execute(
            select(ChannelDB).where(ChannelDB.channel_id == channel_id, ChannelDB.user_id == 1)
        )
        channel = result.scalar_one_or_none()
        if not channel:
            title = f"ID: {channel_id}"
            try:
                if tg_client.is_connected():
                    entity = await tg_client.get_entity(int(channel_id))
                    if isinstance(entity, (Channel, Chat)):
                        title = entity.title
            except Exception:
                pass
            channel = {"channel_id": channel_id, "title": title, "signal_keywords": ""}

    return templates.TemplateResponse("channel_settings.html", {
        "request": request,
        "channel": channel,
    })


@app.post("/channel/{channel_id}/settings")
async def save_channel_settings(channel_id: str, keywords: str = Form("")):
    async with async_session() as session:
        result = await session.execute(
            select(ChannelDB).where(ChannelDB.channel_id == channel_id, ChannelDB.user_id == 1)
        )
        channel = result.scalar_one_or_none()
        if channel:
            channel.signal_keywords = keywords
            await session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/channels/delete/{id}")
async def delete_channel_by_db_id(id: int):
    async with async_session() as session:
        await session.execute(delete(ChannelDB).where(ChannelDB.id == id, ChannelDB.user_id == 1))
        await session.commit()
    await load_channels()
    return RedirectResponse(url="/", status_code=303)


# ─── Bot API ──────────────────────────────────────────────────────────

@app.post("/api/bot/start")
async def api_start_bot():
    bot_state.start()
    asyncio.create_task(notifier.notify_bot_started())
    return JSONResponse({"success": True, "status": bot_state.get_status()})


@app.post("/api/bot/stop")
async def api_stop_bot():
    bot_state.stop()
    return JSONResponse({"success": True, "status": bot_state.get_status()})


@app.get("/api/bot/status")
async def api_get_status():
    return JSONResponse(bot_state.get_status())

# ─── Telegram Web Auth ────────────────────────────────────────────────

from pydantic import BaseModel, model_validator
from telethon import types
from telethon.tl.functions.auth import SendCodeRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError

class TgAuthSendCode(BaseModel):
    phone: str

class TgAuthVerifyCode(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str = ""

@app.get("/api/telegram/status")
async def api_telegram_status():
    if not tg_client.is_connected():
        await tg_client.connect()
    is_auth = await tg_client.is_user_authorized()
    return JSONResponse({"is_authorized": is_auth})

@app.post("/api/telegram/send_code")
async def api_telegram_send_code(req: TgAuthSendCode):
    if not tg_client.is_connected():
        await tg_client.connect()
    
    settings_obj = types.CodeSettings(
        allow_flashcall=True,
        current_number=True,
        allow_app_hash=True,
        allow_missed_call=True,
    )
    try:
        result = await tg_client(SendCodeRequest(
            req.phone, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH, settings_obj
        ))
        
        tg_client._phone_code_hash[req.phone] = result.phone_code_hash
        tg_client._phone = req.phone
        
        return JSONResponse({"success": True, "phone_code_hash": result.phone_code_hash, "type": type(result.type).__name__})
    except FloodWaitError as e:
        return JSONResponse({"success": False, "error": f"Слишком много попыток. Подождите {e.seconds} секунд."})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.post("/api/telegram/verify_code")
async def api_telegram_verify_code(req: TgAuthVerifyCode):
    if not tg_client.is_connected():
        await tg_client.connect()
        
    try:
        await tg_client.sign_in(req.phone, code=req.code, phone_code_hash=req.phone_code_hash)
    except SessionPasswordNeededError:
        if not req.password:
            return JSONResponse({"success": False, "requires_password": True})
        try:
            await tg_client.sign_in(password=req.password)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})
        
    is_auth = await tg_client.is_user_authorized()
    if is_auth:
        # Start background listener
        async def _run_listener():
            async with async_session() as session:
                db_s = (await session.execute(select(SettingsDB).where(SettingsDB.user_id == 1))).scalar_one_or_none()
                chat_id = db_s.telegram_notify_chat_id if db_s and db_s.telegram_notify_chat_id else settings.TELEGRAM_NOTIFY_CHAT_ID
            notifier.configure(tg_client, chat_id or "")
            logger.info("Main: Telegram authorized via Web UI. Listener started.")
            await tg_client.run_until_disconnected()
        asyncio.create_task(_run_listener())
        return JSONResponse({"success": True})
    else:
        return JSONResponse({"success": False, "error": "Unknown error"})


# ─── DUMALKA (Risk Engine Position Management) ────────────────────────

_dumalka_processed_closes: dict = {}
_dumalka_last_poll: float = 0.0
_dumalka_unavailable_alerted: bool = False

class DumalkaCommand(BaseModel):
    action: Literal["move_sl", "move_tp", "full_close", "partial_close", "place_limit_tp"]
    symbol: str
    trace_id: Optional[str] = None
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    reason: str = ""
    zone: Optional[int] = None
    diagnostics: Optional[dict] = None
    fraction: Optional[float] = None
    target_price: Optional[float] = None

    @model_validator(mode="after")
    def validate_params_for_action(self):
        if self.action == "move_sl" and self.new_sl is None:
            raise ValueError("move_sl requires new_sl")
        if self.action == "move_tp" and self.new_tp is None:
            raise ValueError("move_tp requires new_tp")
        return self

@app.post("/dumalka/command")
async def dumalka_command(cmd: DumalkaCommand, request: Request):
    """Accept position management commands from Risk Engine Думалка."""
    global _dumalka_last_poll, _dumalka_unavailable_alerted
    token = request.headers.get("X-Dumalka-Token", "")
    if settings.DUMALKA_TOKEN and token != settings.DUMALKA_TOKEN:
        logger.warning(f"Dumalka: Unauthorized command attempt for {cmd.symbol}")
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    _dumalka_last_poll = time.time()
    _dumalka_unavailable_alerted = False

    if settings.DUMALKA_MODE == "off":
        return JSONResponse({"ok": False, "error": "dumalka_mode is off"}, status_code=400)

    # Map deprecated partial_close / place_limit_tp to full_close
    if cmd.action in ("partial_close", "place_limit_tp"):
        logger.warning(f"Dumalka: Mapping deprecated '{cmd.action}' to 'full_close' for {cmd.symbol}")
        cmd.action = "full_close"

    is_keepalive = (cmd.reason or "").lower() == "keepalive"
    log_fn = logger.debug if is_keepalive else logger.info
    log_fn(f"Dumalka: Received command: {cmd.action} {cmd.symbol} "
           f"(trace={cmd.trace_id}, new_sl={cmd.new_sl}, "
           f"new_tp={cmd.new_tp}, zone={cmd.zone}, reason={cmd.reason})")

    if cmd.action == "full_close" and cmd.trace_id:
        dedup_key = f"{cmd.symbol}:{cmd.trace_id}"
        if dedup_key in _dumalka_processed_closes:
            logger.info(f"Dumalka: Duplicate full_close for {cmd.symbol} (trace={cmd.trace_id}), skipping")
            return JSONResponse({"ok": True, "deduplicated": True})

    # Find active trade for this symbol
    target_trade = None
    target_trade_id = None
    for trade_id, trade in list(trade_manager.active_trades.items()):
        if trade.symbol == cmd.symbol and trade.stage != TradeStage.CLOSED:
            target_trade = trade
            target_trade_id = trade_id
            break

    if not target_trade:
        logger.warning(f"Dumalka: No active trade found for {cmd.symbol}")
        return JSONResponse({"ok": False, "error": f"no active trade for {cmd.symbol}"}, status_code=404)

    # Register dedup key only AFTER confirming trade exists
    if cmd.action == "full_close" and cmd.trace_id:
        dedup_key = f"{cmd.symbol}:{cmd.trace_id}"
        _dumalka_processed_closes[dedup_key] = True
        if len(_dumalka_processed_closes) > 200:
            oldest = list(_dumalka_processed_closes.keys())[:100]
            for k in oldest:
                del _dumalka_processed_closes[k]

    if settings.DUMALKA_MODE == "shadow":
        logger.info(f"Dumalka [SHADOW]: Would execute {cmd.action} on {cmd.symbol} "
                     f"(new_sl={cmd.new_sl}, zone={cmd.zone}, reason={cmd.reason})")
        return JSONResponse({"ok": True, "mode": "shadow", "action": cmd.action,
                             "message": "logged only (shadow mode)"})

    # Active mode: execute
    result = await trade_manager.execute_dumalka_command(target_trade_id, target_trade, cmd)
    return JSONResponse(result)


@app.get("/dumalka/positions")
async def dumalka_positions(request: Request):
    """Return open positions in RE-compatible format for Думалка polling."""
    global _dumalka_last_poll, _dumalka_unavailable_alerted
    token = request.headers.get("X-Dumalka-Token", "")
    if settings.DUMALKA_TOKEN and token != settings.DUMALKA_TOKEN:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    _dumalka_last_poll = time.time()
    _dumalka_unavailable_alerted = False
    positions = await trade_manager.get_positions_for_re()
    return JSONResponse({"ok": True, "positions": positions})


@app.post("/dumalka/context-upload")
async def dumalka_context_upload(
    request: Request,
    artifact: Optional[UploadFile] = File(None),
    documentation: str = Form(default=""),
    changelog: str = Form(default=""),
    version: str = Form(default=""),
    build_id: str = Form(default=""),
):
    """
    Accept a context bundle from Risk Engine / Думалка: optional binary artifact,
    documentation (e.g. Markdown), and changelog for operator context.
    Same auth as other Dumalka endpoints: X-Dumalka-Token.
    """
    token = request.headers.get("X-Dumalka-Token", "")
    if settings.DUMALKA_TOKEN and token != settings.DUMALKA_TOKEN:
        logger.warning("Dumalka: Unauthorized context-upload attempt")
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    artifact_bytes = None
    artifact_filename = None
    if artifact is not None:
        body = await artifact.read()
        if body:
            artifact_bytes = body
            artifact_filename = (artifact.filename or "").strip() or "artifact.bin"

    max_bin = int(settings.DUMALKA_UPLOAD_MAX_BINARY_MB) * 1024 * 1024
    max_txt = int(settings.DUMALKA_UPLOAD_MAX_TEXT_MB) * 1024 * 1024

    manifest, err = save_context_bundle(
        get_dumalka_upload_root(),
        max_bin,
        max_txt,
        artifact_bytes,
        artifact_filename,
        documentation or "",
        changelog or "",
        version or "",
        build_id or "",
    )
    if err:
        code = 413 if "exceeds" in err else 400
        logger.warning(f"Dumalka: context-upload rejected: {err}")
        return JSONResponse({"ok": False, "error": err}, status_code=code)

    logger.info(
        f"Dumalka: context-upload saved bundle_id={manifest.get('bundle_id')} "
        f"version={manifest.get('version')!r} build_id={manifest.get('build_id')!r} "
        f"artifact={manifest.get('artifact_present')}"
    )
    return JSONResponse({"ok": True, "manifest": manifest})


@app.get("/dumalka/context-upload/latest")
async def dumalka_context_upload_latest(request: Request):
    """Return manifest JSON for the most recent context bundle (no binary body)."""
    token = request.headers.get("X-Dumalka-Token", "")
    if settings.DUMALKA_TOKEN and token != settings.DUMALKA_TOKEN:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    m = read_latest_manifest(get_dumalka_upload_root())
    if not m:
        return JSONResponse({"ok": True, "manifest": None, "message": "no bundles yet"})
    return JSONResponse({"ok": True, "manifest": m})


@app.get("/dumalka/context-upload/recent")
async def dumalka_context_upload_recent(request: Request, limit: int = 10):
    """Return manifest list for the N most recent bundles (newest first)."""
    token = request.headers.get("X-Dumalka-Token", "")
    if settings.DUMALKA_TOKEN and token != settings.DUMALKA_TOKEN:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    lim = max(1, min(50, limit))
    items = read_recent_manifests(get_dumalka_upload_root(), lim)
    return JSONResponse({"ok": True, "count": len(items), "manifests": items})


# ─── RE Callback (DEPRECATED — kept for backward compat) ────────────

@app.post("/api/re/callback")
async def re_callback():
    """Deprecated: Telegram Bridge callback. All approval now flows through
    synchronous POST /tv-webhook response. See docs/BOT_API_CONTRACT.md."""
    logger.info("RE Callback: endpoint deprecated (API-first migration)")
    return JSONResponse({"ok": False, "error": "deprecated", "message": "Use /tv-webhook sync response"}, status_code=410)


@app.get("/dumalka/status")
async def dumalka_status():
    """Health check and status for Risk Engine."""
    active_symbols = [t.symbol for t in list(trade_manager.active_trades.values())
                      if t.stage != TradeStage.CLOSED]
    return JSONResponse({
        "ok": True,
        "mode": settings.DUMALKA_MODE,
        "active_trades": len(active_symbols),
        "symbols": active_symbols,
    })


@app.get("/health")
async def health_check():
    """Liveness probe for REDU and monitoring systems."""
    active = [t for t in trade_manager.active_trades.values()
              if t.stage != TradeStage.CLOSED]
    return JSONResponse({
        "status": "healthy",
        "version": "0.10.3",
        "bot_running": bot_state.is_running,
        "active_trades": len(active),
        "dumalka_mode": settings.DUMALKA_MODE,
    })
