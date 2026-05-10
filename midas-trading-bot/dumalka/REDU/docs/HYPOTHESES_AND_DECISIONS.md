# Hypotheses, Validation & Decision Log

> Tracker of all hypotheses tested during the strategy optimization session (2026-04-14).
> Each hypothesis includes the reasoning, data evidence, and outcome.

---

## H1: Tight Stop-Losses + Catch Rockets

**Hypothesis**: If we use short/tight stop-losses (2% instead of 3.5%), even with 5-6 consecutive losers, total loss is small. One TP3 hit covers them all.

**Evidence**:
- What-if on 162 trades: capping losses at 2% saves +99.11% PnL
- 37 trades closed worse than -2.0%, losing an extra 1-18% each beyond the cap
- Worst offender: STOUSDT at -20.39% (would have been -2.0%)
- 4 of 30 "rocket" trades dip below -2% before recovering (costing ~31.86%), but this is far outweighed by the +99.11% saved

**Decision**: **APPROVED**. Change MAX_LOSS_PCT from 3.5 to 2.0 in .env.

---

## H2: Trailing Stop as Primary Exit

**Hypothesis**: A trailing stop (e.g., 2% from peak) would let winners run while protecting gains.

**Evidence**:
- Simulated trail-2% on all April data: **-95.71% total PnL** (catastrophic)
- Volatile coins have natural 1-3% pullbacks every few minutes that trigger trailing stops
- Hybrid approach (trail only after TP1): still underperforms simple -2% hard cap
- REDU's zone system already IS an adaptive trailing stop, and a much smarter one

**Decision**: **REJECTED**. Trailing stops are counterproductive for volatile crypto assets. Zone system stays.

---

## H3: Change Zone 2 Action to "Hold"

**Hypothesis**: Zone 2 full_close exits too early. Changing to "hold" would let trends continue.

**Evidence**:
- 24 Zone 2 exits analyzed: average capture rate 83%
- Only 1 of 24 showed significant further profit after closing
- Zone 2 exits protect against real reversals (SIRENUSDT worst_4h after: -7.7% to -16.5%)

**Decision**: **REJECTED**. Zone 2 exits are mostly correct. The problem is the threshold compression, not the action type.

---

## H4: Per-Symbol Zone Configuration

**Hypothesis**: Different coins need different zone thresholds. RIVERUSDT (0% correct exits) vs SIRENUSDT (42% correct exits) have fundamentally different behavior.

**Evidence**:
- RIVERUSDT: 6/7 zone exits are ROCKET CONTINUED, price barely dips after close
- SIRENUSDT: 5/12 zone exits are correct, deep post-close reversals (-5% to -16%)
- The distinction is clear in data but REDU has no per-symbol config infrastructure

**Decision**: **DEFERRED**. Valid hypothesis, but requires REDU architecture change. Flag for developer.

---

## H5: Adaptive Factor Floor is Too Aggressive

**Hypothesis**: The af floor of 0.5 halves all zone thresholds for volatile coins where MC predicts p_tp≈0, causing premature exits.

**Evidence**:
- ALL 7 RIVERUSDT zone exits: af=0.5 (floor hit). avg raw_af = 0.084
- ALL 12 SIRENUSDT zone exits: af=0.5 (floor hit). avg raw_af = 0.113
- NOMUSDT: only 2/5 at floor. avg raw_af = 1.903 (healthy)
- MC random walk model cannot capture momentum/trend behavior
- Zone 3 effective = 15% × 0.5 = 7.5%. A +5.56% position closes on 0.42% pullback

Simulation with af_floor=0.7:
- RIVERUSDT: 5 of 7 exits saved (trades went +7.7% to +21.4% after)
- SIRENUSDT: 2 correct exits would not trigger zones, caught by hard SL cap at -2%
- Net positive: +20-40% additional PnL from RIVERUSDT alone

**Decision**: **APPROVED**. Raise af floor from 0.5 to 0.7.

---

## H6: Trending Regime Tightens Zones (Bug)

**Hypothesis**: `regime_dd_sensitivity=0.85` for "trending" mode is counterproductive. It tightens zone thresholds by 15% in trending markets — exactly when we want wider thresholds.

**Evidence**:
- RIVERUSDT #616 was in "trending" regime: threshold compressed from 10.0% to 8.5%
- The adaptive_factor cap for trending (1.5 vs 1.2 for normal) suggests the INTENT was to let trends run, but dd_sensitivity contradicts this
- "Ranging" mode has dd_sensitivity=1.2 (widens zones), which makes sense — in ranging, you want wider zones to survive noise

**Decision**: **APPROVED**. Change trending dd_sensitivity from 0.85 to 1.15.

---

## H7: Zone 3 Calibrated Too Tight

**Hypothesis**: Zone 3 threshold calibrated at 15% (from 33 positions) is too aggressive. Combined with af=0.5, effective threshold becomes 7.5%.

**Evidence**:
- Calibration sample: only 33 positions (wide confidence interval)
- Default was 25%. Calibration reduced by 40%.
- With af=0.7 (recommended): 15% × 0.7 = 10.5% (still tight)
- With Z3=20% and af=0.7: 20% × 0.7 = 14% (reasonable, still below default)
- #754 triggered by 0.1% margin at 7.5% threshold. With Z3=20%: 14% threshold, 7.6% DD — would not trigger

**Decision**: **APPROVED**. Raise Z3 from 15% to 20% in zone_calibration table.

---

## H8: CYCLE_INTERVAL_SEC Reduction (30→15)

**Hypothesis**: Faster monitoring cycle improves SL execution timing without causing overlap.

**Evidence**:
- Processing one cycle takes ~12 seconds
- Current 30s interval leaves 18s idle
- With 15s: still 3s buffer for 1-2 positions
- Hard SL cap at -2% benefits from faster detection (price can move 0.5-1% in 15s on volatile coins)

**Decision**: **APPROVED**. Reduce CYCLE_INTERVAL_SEC from 30 to 15.

---

## H9: Wider Breakeven Offset After TP1

**Hypothesis**: Current BE offset is too tight after TP1 hit, causing shakeout on normal noise.

**Evidence**:
- 4 trades identified as "rocket rescue" candidates
- ATR-adaptive BE sometimes sets offset at 0.2-0.3%, which is within normal noise
- TP1_BE_OFFSET_MULT=1.0 and TP1_BE_MAX_PCT=0.05 give more room
- What-if: +20.14% PnL recovered from wider BE

**Decision**: **APPROVED**. Set TP1_BE_OFFSET_MULT=1.0, TP1_BE_MAX_PCT=0.05 in .env.

---

## Summary of All Changes

| # | Change | File | Risk | Impact |
|---|---|---|---|---|
| 1 | MAX_LOSS_PCT=2.0 | .env | Low | +99.11% PnL |
| 2 | TP1_BE params | .env | Low | +20.14% PnL |
| 3 | CYCLE_INTERVAL_SEC=15 | position_tracker.py | Low | Faster SL reaction |
| 4 | af floor 0.5→0.7 | position_tracker.py | Low-Med | Saves 5/7 RIVER exits |
| 5 | trending dd_sensitivity 0.85→1.15 | regime_detector.py | Low | Fixes conceptual bug |
| 6 | Zone 3 thresh 15→20 | zone_calibration DB | Low | Z3 effective 7.5%→14% |

All changes are single-line edits. No architectural modifications.

---

## Future Ideas (Not Yet Validated)

1. **Per-symbol zone thresholds** — RIVERUSDT needs wider zones, SIRENUSDT current ones OK
2. **Momentum-aware MC model** — replace random walk with model that captures serial correlation
3. **Dynamic af floor based on volatility** — if annualized vol > 2.0, raise af floor to 0.8
4. **Re-calibrate zones with more data** — current calibration on 33 positions is statistically weak
5. **Zone 1 as re-entry signal** — when RIVERUSDT zone exits, price continues; could re-enter

---

*Log maintained: 2026-04-14*
