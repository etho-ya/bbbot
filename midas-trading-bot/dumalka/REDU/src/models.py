from datetime import datetime, timezone
from typing import List, Dict, Optional
from pydantic import BaseModel, Field


class Position(BaseModel):
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float


class CandidateTrade(BaseModel):
    symbol: str
    side: str
    size: float
    order_type: str = "market"


class Portfolio(BaseModel):
    equity: float
    positions: List[Position]
    leverage: float = 1.0
    free_margin: float = 0.0


class MarketData(BaseModel):
    """
    Market snapshot.

    volatility values are **annualized** σ (standard deviation of log-returns
    over one year).  The Monte Carlo engine scales them to the requested
    horizon via ``σ_horizon = σ_annual × √(horizon_days / 365)``.
    """
    prices: Dict[str, float]
    volatility: Dict[str, float]
    correlations: Optional[Dict[str, Dict[str, float]]] = None


class RiskLimits(BaseModel):
    max_var: float = 0.05           # 5 % of equity
    max_cvar: float = 0.07          # 7 % of equity
    max_liquidation_prob: float = 0.01  # 1 %


class RiskRequest(BaseModel):
    portfolio: Portfolio
    candidate_trade: Optional[CandidateTrade] = None
    market: MarketData
    risk_limits: RiskLimits
    n_scenarios: int = 100_000
    confidence: float = 0.99        # VaR confidence level


class RiskResult(BaseModel):
    approved: bool
    var: float          # relative to equity
    cvar: float         # relative to equity
    liquidation_prob: float
    drawdown_estimate: float   # worst-case scenario P&L / equity
    latency_ms: float = 0.0    # Monte Carlo computation latencyy
    adjustments: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EnrichedRiskResult(RiskResult):
    """Extended result with signal quality scoring and Midas comparison."""
    signal_hash: Optional[str] = None  # links to trade outcomes
    signal_score: float = 0.0
    is_countertrend: bool = False
    recommendation: str = "approve"  # "approve" / "reduce" / "reject"
    midas_probability: Optional[float] = None
    midas_win_rate: Optional[float] = None
    midas_risk_reward: Optional[float] = None
    computed_volatility: float = 0.0
    score_components: Optional[dict] = None
    kelly_suggested_size_usd: Optional[float] = None
    conviction_size_usd: Optional[float] = None  # v0.10.1: Score-based dynamic lot sizing
    exposure_warning: bool = False
    rejection_reason: Optional[str] = None     # v0.19.8: guard reject reason for bot API consumers
    auto_be_price: Optional[float] = None      # v0.18.5: SL→BE price for bot auto-breakeven
    auto_be_trigger: Optional[float] = None    # v0.18.5: price level that triggers SL→BE move


class WebhookPayload(BaseModel):
    """Payload expected from TradingView / Midas webhooks."""
    symbol: str
    side: str = "long"
    size: float = 1.0
    source: str = "tradingview"
    signal_hash: Optional[str] = None  # unique hash from trading bot / approval flow
    # ── Midas metadata ──
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    trailing_pct: Optional[float] = None
    risk_reward: Optional[float] = None       # e.g. 2.6 means 1:2.6
    probability: Optional[float] = None       # 0-100
    win_rate: Optional[float] = None          # 0-100
    trend: Optional[str] = None               # "strong_bear", "moderate_bull"
    trend_strength: Optional[float] = None    # 0-100
    volume_level: Optional[str] = None        # "high", "medium", "low"
    volatility_level: Optional[str] = None    # "high", "moderate"
    midas_comment: Optional[str] = None       # full text analysis
    setup_master_text: Optional[str] = None    # v0.6.0: Setup Master recommendation text
    # ── Portfolio Data ──
    current_equity: Optional[float] = None
    open_positions: Optional[List[dict]] = None


class SignalRecord(BaseModel):
    """DB Record representation."""
    id: int
    created_at: str
    source: str
    symbol: str
    side: str
    size: float
    signal_hash: Optional[str] = None
    price_at_signal: Optional[float] = None
    volatility_used: Optional[float] = None
    approved: Optional[bool] = None
    var: Optional[float] = None
    cvar: Optional[float] = None
    liquidation_prob: Optional[float] = None
    latency_ms: Optional[float] = None
    # Midas metadata
    risk_reward: Optional[float] = None
    midas_probability: Optional[float] = None
    midas_win_rate: Optional[float] = None
    midas_trend: Optional[str] = None
    is_countertrend: Optional[bool] = None
    # Risk Engine scoring
    re_signal_score: Optional[float] = None
    re_recommendation: Optional[str] = None
    re_annual_vol: Optional[float] = None
    # v0.6.0
    score_components: Optional[dict] = None
    setup_master_text: Optional[str] = None
    # v0.16.0: Shadow PnL — counterfactual outcome tracking
    shadow_pnl_1h: Optional[float] = None
    shadow_pnl_4h: Optional[float] = None
    shadow_outcome: Optional[str] = None
    shadow_resolved_at: Optional[str] = None
    # v0.16.2: close price + event_type from trade_outcomes (same closing row)
    close_price: Optional[float] = None
    close_reason: Optional[str] = None


class TradeOutcomePayload(BaseModel):
    """Payload for recording trade outcomes (SL/TP hits, closes)."""
    hash: Optional[str] = None               # signal hash (optional, graceful fallback)
    event: str                                # open, tp1_hit, tp2_hit, tp3_hit, sl_hit, full_close, timeout, dumalka_close, manual_close, flip_close
    symbol: str
    side: Optional[str] = None
    price: Optional[float] = None
    pnl_pct: Optional[float] = None
    size_remaining: Optional[float] = None
    comment: Optional[str] = None
