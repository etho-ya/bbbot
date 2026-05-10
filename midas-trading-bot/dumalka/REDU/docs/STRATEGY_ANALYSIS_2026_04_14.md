# Strategy Analysis: Micro-Loss + Catch Rockets (2026-04-14)

> Full context of the analysis session between the operator and AI assistant.
> Data source: REDU PostgreSQL (`riskengine_db`), 162 closed positions since 2026-04-01.

---

## 1. Problem Statement

The bot was experiencing:
- Low win rate (~20% success, ~80% loss)
- Early exits at TP1-TP2, almost never hitting TP3
- Full stop-loss hits on losing trades (-3.5%)
- Missing large moves ("rockets") that continued after close

Desired strategy: **tight stop-losses** to minimize frequent small losses, hold winners longer to catch TP3 hits that cover multiple small losses.

---

## 2. Key Discoveries

### 2.1 MAX_LOSS_PCT Override (.env vs config.py)

**Critical bug**: `/opt/risk-engine/.env` had `MAX_LOSS_PCT=3.5` which overrode the code change to `2.0` in `config.py`. Systemd loads `.env` values, and Pydantic `BaseSettings` gives env vars priority.

**Impact**: Trades were closing at -3.35% to -3.61% instead of the intended -2.0%.

**What-if (2.0% cap applied to all April data)**:
- 37 trades affected (closed worse than -2.0%)
- Saved: **+99.11% PnL** (the single biggest improvement)
- Example: STOUSDT closed at -20.39% would have been capped at -2.0%

### 2.2 Trailing Stop Analysis

Comprehensive simulation showed standalone trailing stops **perform catastrophically**:
- Trail 2% from peak: **-95.71% total PnL** (vs +12.00% actual)
- Hybrid trailing: also underperformed simple hard -2% cap
- Reason: volatile coins have frequent natural pullbacks that trigger trailing stops prematurely

**Conclusion**: Trailing stops are NOT the solution. The zone system outperforms any trailing stop variant.

### 2.3 Zone System Efficiency

The zone system captures 83-87% of peak profits on average. Analysis of "rocket" trades:
- NOMUSDT #623: closed at +12.93%, peak was +15.58% (83% capture)
- SIRENUSDT #580: survived -6.18% dip in Zone 0, closed at +9.46% from +12.05% peak
- Zone 0 ("patience zone") allows trades to recover from deep early dips

### 2.4 Volatile Coins & 2% Hard Stop

Simulation: 2% hard stop would prematurely close 4 of 30 "rocket" trades that temporarily dipped below -2% before recovering (costing ~31.86% PnL). But this is **significantly outweighed** by the +99.11% saved from capping major losses. Net effect strongly positive.

---

## 3. RIVERUSDT Deep Analysis

### 3.1 Trade Profile (19 trades, April 1-14)

| Close Reason | Trades | Avg PnL | Total PnL | Avg Peak |
|---|---|---|---|---|
| zone_full_exit | 7 | +3.08% | +21.59% | 3.67% |
| phantom_sl_hit | 3 | +0.95% | +2.84% | 1.47% |
| sl_hit | 2 | -0.89% | -1.77% | 2.59% |
| bot_sync_desync | 2 | -0.09% | -0.19% | 1.00% |
| dumalka_close | 1 | -0.75% | -0.75% | 1.65% |
| hard_sl_cap | 3 | -3.56% | -10.69% | 0.42% |

**Overall RIVERUSDT PnL: +11.03%**
With MAX_LOSS_PCT=2.0%: **+19.76%** (+8.73% saved)

### 3.2 Post-Close Behavior: The Smoking Gun

**16 of 18 RIVERUSDT trades** showed the price continuing in the predicted direction after closing:

| Trade | Close Reason | Closed PnL | 24h After Peak | Verdict |
|---|---|---|---|---|
| #541 | phantom_sl_hit | +0.52% | +25.43% | ROCKET |
| #545 | phantom_sl_hit | +1.78% | +25.50% | ROCKET |
| #546 | phantom_sl_hit | +0.54% | +25.02% | ROCKET |
| #567 | sl_hit | -3.35% | +4.42% | ROCKET |
| #577 | zone_full_exit | +2.51% | +3.79% | more |
| #585 | bot_sync_desync | +0.55% | +4.58% | ROCKET |
| #590 | sl_hit | +1.58% | +8.05% | ROCKET |
| #602 | bot_sync_desync | -0.73% | -0.80% | more |
| #616 | zone_full_exit | +2.32% | +14.74% | ROCKET |
| #621 | hard_sl_cap | -3.55% | +9.19% | ROCKET |
| #633 | hard_sl_cap | -3.53% | +0.90% | ROCKET |
| #636 | zone_full_exit | +2.13% | +7.69% | ROCKET |
| #639 | hard_sl_cap | -3.61% | +1.40% | ROCKET |
| #651 | zone_full_exit | +3.16% | +21.43% | ROCKET |
| #685 | dumalka_close | -0.75% | +3.96% | ROCKET |
| #746 | zone_full_exit | +4.29% | +15.06% | ROCKET |
| #754 | zone_full_exit | +5.13% | +15.58% | ROCKET |
| #761 | zone_full_exit | +2.05% | +10.32% | ROCKET |

**ZERO correct exits** for RIVERUSDT zone_full_exit trades.

### 3.3 Post-Close Drawdown: RIVERUSDT vs SIRENUSDT

RIVERUSDT after close barely dips before continuing:
- #616: worst 4h = +2.16% (no dip at all)
- #636: worst 4h = +2.05% (no dip)
- #746: worst 4h = +3.52% (no dip)
- #754: worst 4h = +8.39% (accelerated!)

SIRENUSDT after close often dips deeply:
- #609: worst 4h = -16.46% (catastrophic reversal)
- #611: worst 4h = -7.70%
- #618: worst 4h = -5.19%

**Conclusion**: RIVERUSDT is a momentum/trend coin. SIRENUSDT has deep corrections. The zone system correctly protects SIRENUSDT but prematurely kills RIVERUSDT.

---

## 4. Root Cause Analysis: "Triple Compression"

### 4.1 Zone Exit Formula

```
adjusted_threshold = base_thresh × af × vol_mod × regime_dd × heat
```

### 4.2 Calibrated Thresholds (from DB, zone_calibration id=2)

| Zone | Default | Calibrated (active) |
|---|---|---|
| Z1 | 40% | 30% |
| Z2 | 30% | 25% |
| Z3 | 25% | **15%** |
| Z4 | 15% | **10%** |

Calibrated on 33 positions only.

### 4.3 Adaptive Factor Always at Floor (0.5)

Monte Carlo predicts for RIVERUSDT:
- p_tp = 0.000 - 0.184 (near-zero probability of hitting TP3)
- p_sl = 0.255 - 0.553

`af = clamp(p_tp / p_sl, 0.5, cap)` → always hits 0.5 floor.

MC uses random walk model, which doesn't capture RIVERUSDT's momentum behavior.

**All 7 of 7** RIVERUSDT zone exits have af=0.5 (floor).
**All 12 of 12** SIRENUSDT zone exits have af=0.5 (floor).
NOMUSDT: only 2 of 5. XNYUSDT: 1 of 3.

### 4.4 Regime DD Sensitivity Bug

```python
"trending": {"dd_sensitivity": 0.85}  # TIGHTENS zones in trends!
```

Counterproductive: trending mode should WIDEN thresholds to let trends run.

### 4.5 Reconstructed Effective Thresholds for RIVERUSDT Exits

| Trade | Zone | Base | AF | Vol_mod | Regime | Effective | DD% | Margin |
|---|---|---|---|---|---|---|---|---|
| #616 | Z2 | 25% | 0.5 | 0.8 | 0.85 | **8.5%** | 11.6% | triggered |
| #636 | Z2 | 25% | 0.5 | 1.0 | 1.0 | **12.5%** | 16.6% | triggered |
| #651 | Z2 | 25% | 0.5 | 1.0 | 1.0 | **12.5%** | 14.7% | triggered |
| #746 | Z3 | 15% | 0.5 | 1.0 | 1.0 | **7.5%** | 8.7% | triggered |
| #754 | Z3 | 15% | 0.5 | 1.0 | 1.0 | **7.5%** | 7.6% | **by 0.1%!** |
| #761 | Z2 | 25% | 0.5 | 0.8 | 1.0 | **10.0%** | 18.4% | triggered |
| #577 | Z2 | 25% | 0.5 | 1.0 | 1.2 | **15.0%** | 38.3% | triggered |

**#754 was killed by a 0.1% margin.** Peak was +5.56%, closed at +5.13%. Price then went to +15.58%.

---

## 5. All Volatile Coins: Post-Close Summary

| Coin | Zone Exits | Rockets | Correct | Avg Closed | Avg 24h After | Left on Table |
|---|---|---|---|---|---|---|
| RIVERUSDT | 7 | **6 (86%)** | 0 (0%) | +3.08% | +12.66% | +9.58% |
| NOMUSDT | 5 | 3 (60%) | 1 (20%) | +8.50% | +21.97% | +13.47% |
| SIRENUSDT | 12 | 7 (58%) | 5 (42%) | +4.41% | +27.47% | +23.06% |
| XNYUSDT | 3 | 2 (67%) | 0 (0%) | +3.42% | +8.82% | +5.40% |
| STOUSDT | 2 | 1 (50%) | 1 (50%) | +5.04% | +7.68% | +2.64% |
| **TOTAL** | **29** | **19 (66%)** | **7 (24%)** | **+4.74%** | **+19.65%** | **+14.92%** |

On average, **14.92% PnL is left on the table** per zone_full_exit trade.

---

## 6. Approved Recommendations

### 6.1 Priority 1: Fix MAX_LOSS_PCT in .env

**File**: `/opt/risk-engine/.env`
**Change**: `MAX_LOSS_PCT=3.5` → `MAX_LOSS_PCT=2.0`
**Impact**: +99.11% PnL saved across 37 trades (single biggest improvement)

### 6.2 Priority 2: Widen BE Offset After TP1

**File**: `/opt/risk-engine/.env`
**Add**:
```
TP1_BE_OFFSET_MULT=1.0
TP1_BE_MAX_PCT=0.05
```
**Impact**: Prevents shakeout on breakeven moves after TP1 hit

### 6.3 Priority 3: Accelerate Position Tracker Cycle

**File**: `/opt/risk-engine/src/position_tracker.py`
**Change**: `CYCLE_INTERVAL_SEC = 30` → `CYCLE_INTERVAL_SEC = 15`
**Impact**: 2x faster SL reaction, processing takes ~12s so 15s fits

### 6.4 Priority 4: Raise Adaptive Factor Floor (0.5 → 0.7)

**File**: `/opt/risk-engine/src/position_tracker.py`, function `compute_adaptive_factor`
**Change**: `return max(0.5, min(cap, factor))` → `return max(0.7, min(cap, factor))`
**Impact**: Saves 5 of 7 RIVERUSDT zone exits. Zone 3 effective threshold 7.5% → 10.5%.

Simulation for RIVERUSDT with af=0.7:
- #616: threshold 8.5% → 11.9%, dd=11.6% → **SAVED** (went to +14.7%)
- #636: threshold 12.5% → 17.5%, dd=16.6% → **SAVED** (went to +7.7%)
- #651: threshold 12.5% → 17.5%, dd=14.7% → **SAVED** (went to +21.4%)
- #746: threshold 7.5% → 10.5%, dd=8.7% → **SAVED** (went to +15.1%)
- #754: threshold 7.5% → 10.5%, dd=7.6% → **SAVED** (went to +15.6%)

Risk: 2 SIRENUSDT correct exits would not trigger by zones, but caught by hard SL cap at -2.0%.

### 6.5 Priority 5: Fix Trending Regime DD Sensitivity

**File**: `/opt/risk-engine/src/regime_detector.py`
**Change**: `"trending": {"dd_sensitivity": 0.85}` → `"trending": {"dd_sensitivity": 1.15}`
**Impact**: In trending markets, zone thresholds become 15% wider instead of 15% tighter.

### 6.6 Priority 6: Raise Calibrated Zone 3 Threshold

**SQL**: `UPDATE zone_calibration SET zone_3_dd_thresh = 20 WHERE id = 2;`
**Change**: Zone 3 from 15% to 20%
**Impact**: Zone 3 effective = 20% × 0.7 = 14% (vs current 7.5%). Still conservative vs default 25%.

### 6.7 Keep Zone System Unchanged (Architecture)

Zone actions (hold/full_close) and zone boundaries remain as-is. The zone system is an intelligent adaptive trailing stop that outperforms any external trailing stop variant.

---

## 7. What-If Simulation: Combined Impact

### Before (April 1-14 actual):
- Total PnL all coins: +12.00%
- RIVERUSDT: +11.03%

### After MAX_LOSS_PCT=2.0% only:
- Total PnL: +115.55% (+103.55% improvement)
- 95% of improvement comes from capping catastrophic losses

### After all recommendations combined (estimated):
- Hard SL cap saves: +99.11%
- Wider BE saves: +20.14% (4 "rocket rescue" trades)
- af=0.7 saves: +20-40% (5 RIVERUSDT + several SIRENUSDT/NOMUSDT)
- Faster cycle: improves SL execution timing
- Total estimated improvement: **+140-160%** vs actual +12%

---

## 8. Not Recommended (With Reasoning)

| Idea | Why Not |
|---|---|
| Per-symbol zone thresholds | No architecture support in REDU, requires major refactor |
| Disable af for volatile coins | Removes protection entirely, bad for SIRENUSDT correct exits |
| Standalone trailing stop | Simulated at -95.71% PnL, catastrophic for volatile coins |
| Hybrid trailing stop | Underperforms simple -2% hard cap |
| Change Zone 2 to "hold" | 83% capture rate shows Zone 2 exits are mostly correct |
| Reduce MAX_TP_FOR_ZONES_PCT | Already at 10%, any lower breaks zone progression |

---

## 9. Data Queries Reference

All analysis queries ran against:
- Host: 100.87.26.107 (REDU / rsk-eng via Tailscale)
- Database: riskengine_db
- User: riskengine
- Tables: `open_positions`, `position_snapshots`, `dumalka_audit_log`, `klines_history`, `zone_calibration`

Key column mappings:
- `realized_pnl_pct` — actual PnL at close
- `max_pnl_pct` — peak PnL during trade
- `drawdown_pct` — absolute drawdown from peak
- `tp_progress_pct` — progress toward TP3 (capped by MAX_TP_FOR_ZONES_PCT=10%)
- `mc_p_tp`, `mc_p_sl` — Monte Carlo probabilities
- `zone` — zone at close time (0-4)
- `close_reason` — what triggered the close

---

## 10. Architecture Notes

### Zone Policy (position_tracker.py line 640)

```python
DEFAULT_ZONE_POLICY = [
    {"name": "Zone 0", "min_tp": 0,  "max_tp": 3,   "dd_thresh": None, "action": "hold"},
    {"name": "Zone 1", "min_tp": 3,  "max_tp": 20,  "dd_thresh": 40,   "action": "hold"},
    {"name": "Zone 2", "min_tp": 20, "max_tp": 40,  "dd_thresh": 30,   "action": "full_close"},
    {"name": "Zone 3", "min_tp": 40, "max_tp": 70,  "dd_thresh": 25,   "action": "full_close"},
    {"name": "Zone 4", "min_tp": 70, "max_tp": 999, "dd_thresh": 15,   "action": "full_close"},
]
```

### Threshold Compression Chain

```
adjusted = base × adaptive_factor × vol_modifier × regime_dd × heat_modifier
                   ^^^^^^^^^^^^^^^^   ^^^^^^^^^^^   ^^^^^^^^^   ^^^^^^^^^^^^^
                   MC: p_tp/p_sl      volume<0.5    trending    BTC corr>0.8
                   floor=0.5→0.7      → 0.8         0.85→1.15   → 0.7
                   (kills volatile    (minor)       (bug fix)   (aggressive)
                    coins)
```

### Key Files on REDU Server

- `/opt/risk-engine/src/position_tracker.py` — zone policy, adaptive_factor, cycle loop
- `/opt/risk-engine/src/regime_detector.py` — regime classification, dd_sensitivity
- `/opt/risk-engine/src/config.py` — MAX_LOSS_PCT, TP1_BE_* params
- `/opt/risk-engine/.env` — runtime overrides (loaded by systemd)
- `/opt/risk-engine/src/scoring_v2.py` — signal scoring, R:R filter

---

*Analysis conducted 2026-04-14. Data covers 2026-04-01 to 2026-04-14.*
*162 closed positions analyzed. 29 zone_full_exit trades deep-dived.*
*RIVERUSDT: 19 trades, 7 zone exits, 3 hard SL cap, 3 phantom SL, 2 sl_hit, 2 desync, 1 dumalka close, 1 re-entry.*
