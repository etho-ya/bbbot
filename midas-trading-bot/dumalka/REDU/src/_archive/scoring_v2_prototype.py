"""
Scoring Engine v2 Prototype (v0.9.x Roadmap)
Professional Refactoring into Pluggable Strategy Pattern.

Current pain points addressed:
1. `compute_signal_score` is a 350-line procedural mega-function.
2. Mixing of string parsing (regex/translation) with math logic.
3. Hardcoded heuristic weights (wr: 0.25, prob: 0.22) makes it hard to safely A/B test or adopt ML.

In V2:
- Separation of Concerns: `DataNormalizer` (cleans strings) -> `BaseScoringStrategy` -> `EnsembleEvaluator`.
- Ready for ML Pipeline integration (`XGBoostScoringStrategy`).
"""

import logging
from typing import Dict, Optional, Protocol, List
from pydantic import BaseModel, Field

logger = logging.getLogger("risk-engine.scoring.v2")

# ============================================================================
# 1. Standardized Domain Inputs
# ============================================================================
class SignalFeatures(BaseModel):
    """Strictly typed, normalized inputs for any scoring strategy."""
    side: str
    risk_reward: float
    probability: float
    win_rate: float
    trend_align_score: float  # Normalized 0.0 to 1.0 (replaces string parsing)
    market_vol: float
    spread_pct: Optional[float] = None
    slippage_ratio: Optional[float] = None
    funding_rate: Optional[float] = None
    oi_change_pct: Optional[float] = None
    is_countertrend: bool

class ScoringResult(BaseModel):
    score: float  # 0.0 to 1.0
    recommendation: str  # approve, reduce, reject
    kelly_f: float
    reasoning: List[str] = Field(default_factory=list)

# ============================================================================
# 2. Strategy Protocol (Pluggable logic)
# ============================================================================
class ScoringStrategy(Protocol):
    def evaluate(self, features: SignalFeatures) -> float:
        """Returns a raw score between 0.0 and 1.0"""
        ...

class HeuristicScoringStrategy:
    """The legacy rule-based mathematical scoring, now cleanly encapsulated."""
    
    def __init__(self, weights: Dict[str, float] = None):
        self.weights = weights or {
            "wr": 0.25, "prob": 0.22, "rr": 0.17, 
            "trend": 0.14, "vol": 0.07, "liq": 0.08, "fund_oi": 0.07
        }

    def evaluate(self, f: SignalFeatures) -> float:
        # Example calculation (clean math, no string parsing)
        wr_norm = min(f.win_rate / 100.0, 1.0)
        prob_norm = min(f.probability / 100.0, 1.0)
        rr_norm = min(f.risk_reward / 5.0, 1.0)
        # ... logic
        
        raw_score = (
            self.weights["wr"] * wr_norm +
            self.weights["prob"] * prob_norm +
            self.weights["rr"] * rr_norm
            # + ...
        )
        return min(max(raw_score, 0.0), 1.0)

class MLXGBoostScoringStrategy:
    """Future v0.9.x strategy powered by optimal_action labels."""
    def __init__(self, model_path: str):
        # self.model = xgb.Booster()
        # self.model.load_model(model_path)
        pass

    def evaluate(self, f: SignalFeatures) -> float:
        # return float(self.model.predict([...])[0])
        return 0.85

# ============================================================================
# 3. Ensemble Evaluator (The Orchestrator)
# ============================================================================
class EnsembleEvaluator:
    """Manages multiple strategies and applies hard safety limits."""
    
    def __init__(self, primary_strategy: ScoringStrategy, kelly_cap: float = 0.10):
        self.primary = primary_strategy
        self.kelly_cap = kelly_cap
        self.hard_limits = []

    def evaluate_signal(self, features: SignalFeatures) -> ScoringResult:
        # 1. Base Score
        score = self.primary.evaluate(features)
        
        reasons = []
        rec = "approve"
        
        # 2. Hard Limits (Force Reject) - gracefully handle Optional fields
        if features.slippage_ratio and features.slippage_ratio > 0.5:
            rec = "reject"
            reasons.append("Slippage exceeds 50% of Stop Loss distance")
        elif features.is_countertrend and features.probability < 40:
            rec = "reject"
            reasons.append("Dangerous countertrend with low probability")
            
        # 3. Tiering
        if rec != "reject":
            if score < 0.45:
                rec = "reject"
            elif score < 0.60:
                rec = "reduce"
                
        # 4. Kelly Sizing
        p = features.win_rate / 100.0
        b = features.risk_reward
        f_star = (p * b - (1 - p)) / b if b > 0 else 0
        kelly = max(0.0, f_star) * self.kelly_cap  # Dynamic Kelly Cap Limits
        
        return ScoringResult(
            score=round(score, 4),
            recommendation=rec,
            kelly_f=round(kelly, 4),
            reasoning=reasons
        )

if __name__ == "__main__":
    print("✅ Scoring V2 Prototype Architecture designed successfully.")
