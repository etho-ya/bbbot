from sqlalchemy import Column, String, Float, Integer, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class UserDB(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    bybit_api_key = Column(String, nullable=True)
    bybit_api_secret = Column(String, nullable=True)
    is_testnet = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TradeDB(Base):
    __tablename__ = "trades"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String)
    side = Column(String)
    entry_price = Column(Float)
    size = Column(Float)
    leverage = Column(Integer)
    tp1 = Column(Float)
    tp2 = Column(Float)
    tp3 = Column(Float)
    sl = Column(Float)
    trailing_stop_pct = Column(Float, nullable=True)
    stage = Column(Integer, default=0)
    position_id = Column(String)
    status = Column(String, default="active")
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    profit_loss = Column(Float, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SettingsDB(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    deposit_percent = Column(Float, default=10.0)
    leverage = Column(Integer, default=20)
    max_price_deviation = Column(Float, default=1.0)
    default_stop_loss_percent = Column(Float, default=10.0)
    max_open_positions = Column(Integer, default=10)
    max_daily_loss_percent = Column(Float, default=10.0)

    # Signal source
    signal_bot_id = Column(String, nullable=True)
    signal_bot_username = Column(String, nullable=True)

    # LLM settings
    openrouter_api_key = Column(String, nullable=True)
    openrouter_model = Column(String, default="qwen/qwen3-coder:free")
    llm_validation_enabled = Column(Boolean, default=True)

    # Telegram notifications (sent via the same Telethon account)
    telegram_notify_chat_id = Column(String, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChannelDB(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    channel_id = Column(String, nullable=False)
    title = Column(String)
    signal_keywords = Column(String, nullable=True)


class SignalDB(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    raw_text = Column(Text, nullable=False)
    parsed_data = Column(Text)
    channel_id = Column(String, nullable=True)
    channel_title = Column(String, nullable=True)
    signal_hash = Column(String, nullable=True)
    signal_type = Column(String, default="trade")  # "trade" or "auxiliary"
    is_processed = Column(Boolean, default=False)

    # LLM validation results
    llm_decision = Column(String, nullable=True)  # "approve" / "reject"
    llm_reason = Column(Text, nullable=True)
    llm_model = Column(String, nullable=True)

    # Risk Engine results
    re_status = Column(String, nullable=True)  # "pending" / "approved" / "rejected" / "timeout"
    re_score = Column(Float, nullable=True)
    re_reason = Column(Text, nullable=True)
    re_responded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
