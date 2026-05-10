from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional


class TradeSide(str, Enum):
    LONG = "Buy"
    SHORT = "Sell"


class TradeStage(int, Enum):
    PENDING = -1     # Limit order placed, not yet filled
    OPEN = 0
    TP1_REACHED = 1
    TP2_REACHED = 2
    CLOSED = 3


class TradeState(BaseModel):
    id: str
    user_id: int
    symbol: str
    side: TradeSide
    entry_price: float
    size: float
    leverage: int
    tp1: float
    tp2: float
    tp3: float
    sl: float
    trailing_stop_pct: Optional[float] = None
    signal_hash: Optional[str] = None
    stage: TradeStage = TradeStage.OPEN
    position_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    dumalka_closing: bool = False
    entry_order_expires_at: Optional[datetime] = None
    entry_order_type: Optional[str] = None
    entry_order_price: Optional[float] = None
    auto_be_price: Optional[float] = None
    auto_be_trigger: Optional[float] = None

    def to_dict(self):
        d = self.model_dump(exclude={
            "signal_hash", "dumalka_closing", "auto_be_price", "auto_be_trigger",
            "entry_order_expires_at", "entry_order_type", "entry_order_price",
        })
        d["side"] = self.side.value
        d["stage"] = self.stage.value
        return d
