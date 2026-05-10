"""
init_db.py — Создать все таблицы синхронно (SQLAlchemy 2.0)
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, Integer, DateTime, Boolean, ForeignKey
from datetime import datetime
import os

class Base(DeclarativeBase):
    pass

class UserDB(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    bybit_api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    bybit_api_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class TradeDB(Base):
    __tablename__ = "trades"
    
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String)
    side: Mapped[str | None] = mapped_column(String)
    entry_price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    leverage: Mapped[int | None] = mapped_column(Integer)
    tp1: Mapped[float | None] = mapped_column(Float)
    tp2: Mapped[float | None] = mapped_column(Float)
    tp3: Mapped[float | None] = mapped_column(Float)
    sl: Mapped[float | None] = mapped_column(Float)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    position_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    signal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SettingsDB(Base):
    __tablename__ = "settings"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True)
    deposit_percent: Mapped[float] = mapped_column(Float, default=10.0)
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    max_price_deviation: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ChannelDB(Base):
    __tablename__ = "channels"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"))
    channel_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    signal_keywords: Mapped[str | None] = mapped_column(String, nullable=True)

class SignalDB(Base):
    __tablename__ = "signals"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    raw_text: Mapped[str] = mapped_column(String, nullable=False)
    parsed_data: Mapped[str | None] = mapped_column(String)
    channel_id: Mapped[str | None] = mapped_column(String, nullable=True)
    channel_title: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_hash: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

if __name__ == "__main__":
    DATABASE_URL = "postgresql+asyncpg://botuser:botpass@localhost/trading_bot"
    
    sync_engine = create_engine(DATABASE_URL.replace("+asyncpg", ""), echo=True)
    
    print("Создание таблиц...")
    Base.metadata.create_all(sync_engine)
    print("Все таблицы созданы")
    
    with sync_engine.connect() as conn:
        result = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
        tables = [row[0] for row in result]
        print(f"Созданы таблицы: {tables}")
    
    sync_engine.dispose()
