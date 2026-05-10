"""
Zone Threshold Calibration — Phase 4A
═══════════════════════════════════════
Analyzes closed positions + their snapshots to find optimal zone thresholds
that maximize capture_ratio (realized_pnl / max_possible_pnl).

Usage:
    python3 calibrate_zones.py [--output config]
"""

import sqlite3
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "signals.db"

# Current zone policy (from position_tracker.py)
CURRENT_ZONES = [
    {"name": "Zone 0", "min_tp": 0,  "max_tp": 20,  "dd_thresh": None, "base_close_frac": 0.0},
    {"name": "Zone 1", "min_tp": 20, "max_tp": 40,  "dd_thresh": 50,   "base_close_frac": 0.0},
    {"name": "Zone 2", "min_tp": 40, "max_tp": 60,  "dd_thresh": 40,   "base_close_frac": 0.40},
    {"name": "Zone 3", "min_tp": 60, "max_tp": 80,  "dd_thresh": 30,   "base_close_frac": 0.60},
    {"name": "Zone 4", "min_tp": 80, "max_tp": 999, "dd_thresh": 20,   "base_close_frac": 0.90},
]


def load_positions(conn):
    """Load all closed positions with their lifecycle data."""
    c = conn.cursor()
    c.execute("""
        SELECT id, signal_hash, symbol, side, entry_price,
               max_pnl_pct, realized_pnl_pct, close_reason,
               opened_at, closed_at,
               current_tp1, current_tp2, current_tp3, current_sl,
               initial_signal_score, initial_recommendation
        FROM open_positions
        WHERE status = 'closed'
        ORDER BY id
    """)
    return c.fetchall()


def load_snapshots_for_position(conn, pos_id):
    """Load all snapshots for a given position, ordered by time."""
    c = conn.cursor()
    c.execute("""
        SELECT snapshot_at, pnl_pct, max_pnl_pct, drawdown_pct,
               tp_progress_pct, zone, hours_open,
               volatility, volume_ratio,
               mc_p_tp, mc_p_sl, mc_var,
               action_taken
        FROM position_snapshots
        WHERE pos_id = ?
        ORDER BY snapshot_at
    """, (pos_id,))
    return c.fetchall()


def compute_capture_ratio(realized_pnl, max_pnl):
    """Capture ratio = how much of the peak profit was actually realized."""
    if max_pnl is None or max_pnl <= 0:
        return 0.0
    if realized_pnl is None:
        return 0.0
    return realized_pnl / max_pnl


def simulate_zone_policy(snapshots, zone_thresholds, timeout_hours=12):
    """
    Simulate a zone policy on snapshots and determine when it would trigger exit.
    Returns: (exit_snapshot_idx, exit_pnl, exit_reason)
    """
    for i, snap in enumerate(snapshots):
        pnl_pct = snap[1]       # pnl_pct
        max_pnl = snap[2]       # max_pnl_pct
        dd = snap[3]            # drawdown_pct
        tp_prog = snap[4]       # tp_progress_pct
        hours = snap[6]         # hours_open

        # Timeout check
        if hours > timeout_hours and tp_prog < 20:
            return (i, pnl_pct, "timeout")

        # Zone-based check
        if max_pnl > 0:
            dd_pct_of_max = (dd / max_pnl) * 100 if max_pnl > 0 else 0

            for zone in reversed(zone_thresholds):
                if tp_prog >= zone["min_tp"]:
                    if zone["dd_thresh"] is not None and dd_pct_of_max > zone["dd_thresh"]:
                        return (i, pnl_pct, f"zone_{zone['name']}")
                    break

    # Never triggered → use last snapshot
    if snapshots:
        return (len(snapshots) - 1, snapshots[-1][1], "no_trigger")
    return (0, 0, "no_data")


def run_calibration():
    conn = sqlite3.connect(str(DB_PATH))
    
    print("=" * 70)
    print("ZONE THRESHOLD CALIBRATION — Phase 4A")
    print(f"Database: {DB_PATH}")
    print("=" * 70)

    positions = load_positions(conn)
    print(f"\nTotal closed positions: {len(positions)}")

    # ── 1. Analyze current performance ────────────────────────────
    print("\n" + "─" * 50)
    print("1. CURRENT PERFORMANCE ANALYSIS")
    print("─" * 50)

    position_data = []
    n_with_snapshots = 0
    close_reasons = defaultdict(int)
    capture_ratios = []
    
    for pos in positions:
        pos_id = pos[0]
        symbol = pos[2]
        max_pnl = pos[5]
        realized_pnl = pos[6]
        close_reason = pos[7]
        score = pos[14]
        rec = pos[15]
        
        close_reasons[close_reason or "unknown"] += 1
        
        cr = compute_capture_ratio(realized_pnl, max_pnl)
        
        snapshots = load_snapshots_for_position(conn, pos_id)
        
        if snapshots:
            n_with_snapshots += 1
            # Peak analysis
            max_pnl_in_snaps = max(s[2] or 0 for s in snapshots) if snapshots else 0
            final_pnl = snapshots[-1][1] if snapshots else 0
            n_snaps = len(snapshots)
            hours = snapshots[-1][6] if snapshots else 0
            
            # MC data availability
            has_mc = any(s[9] and s[9] > 0 for s in snapshots)
            
            position_data.append({
                "pos_id": pos_id,
                "symbol": symbol,
                "max_pnl": max_pnl or max_pnl_in_snaps,
                "realized_pnl": realized_pnl,
                "final_snap_pnl": final_pnl,
                "capture_ratio": cr,
                "close_reason": close_reason,
                "n_snapshots": n_snaps,
                "hours": hours,
                "score": score,
                "rec": rec,
                "has_mc": has_mc,
                "snapshots": snapshots,
            })
            
            if max_pnl and max_pnl > 0:
                capture_ratios.append(cr)

    print(f"  Positions with snapshots: {n_with_snapshots}/{len(positions)}")
    print(f"  Positions with positive max_pnl: {len(capture_ratios)}")
    
    print(f"\n  Close reasons:")
    for reason, cnt in sorted(close_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {cnt}")

    if capture_ratios:
        avg_cr = sum(capture_ratios) / len(capture_ratios)
        median_cr = sorted(capture_ratios)[len(capture_ratios) // 2]
        best_cr = max(capture_ratios)
        worst_cr = min(capture_ratios)
        print(f"\n  Capture Ratio Statistics:")
        print(f"    Average:  {avg_cr:.2%}")
        print(f"    Median:   {median_cr:.2%}")
        print(f"    Best:     {best_cr:.2%}")
        print(f"    Worst:    {worst_cr:.2%}")
        print(f"    Positive: {sum(1 for cr in capture_ratios if cr > 0)}/{len(capture_ratios)}")

    # ── 2. MC Data Availability ───────────────────────────────────
    print("\n" + "─" * 50)
    print("2. MC FORWARD DATA AVAILABILITY")
    print("─" * 50)
    
    has_mc_count = sum(1 for p in position_data if p["has_mc"])
    total_snaps = sum(p["n_snapshots"] for p in position_data)
    snaps_with_mc = sum(
        sum(1 for s in p["snapshots"] if s[9] and s[9] > 0) 
        for p in position_data
    )
    print(f"  Positions with MC data: {has_mc_count}/{len(position_data)}")
    print(f"  Snapshots with MC data: {snaps_with_mc}/{total_snaps}")
    
    # ── 3. Zone Distribution Analysis ─────────────────────────────
    print("\n" + "─" * 50)
    print("3. ZONE DISTRIBUTION (from snapshots)")
    print("─" * 50)
    
    zone_stats = defaultdict(lambda: {"count": 0, "pnl_sum": 0, "dd_sum": 0, "triggered": 0})
    for p in position_data:
        for snap in p["snapshots"]:
            zone = snap[5] or 0
            pnl = snap[1] or 0
            dd = snap[3] or 0
            action = snap[12] or "hold"
            zone_stats[zone]["count"] += 1
            zone_stats[zone]["pnl_sum"] += pnl
            zone_stats[zone]["dd_sum"] += dd
            if action != "hold":
                zone_stats[zone]["triggered"] += 1

    print(f"  {'Zone':>6} {'Snapshots':>10} {'Avg PnL':>10} {'Avg DD':>10} {'Triggered':>10}")
    for z in sorted(zone_stats.keys()):
        s = zone_stats[z]
        n = s["count"]
        avg_pnl = s["pnl_sum"] / n if n > 0 else 0
        avg_dd = s["dd_sum"] / n if n > 0 else 0
        print(f"  Zone {z:<2}  {n:>8}  {avg_pnl:>+9.2f}%  {avg_dd:>9.2f}%  {s['triggered']:>9}")

    # ── 4. Threshold Optimization ─────────────────────────────────
    print("\n" + "─" * 50)
    print("4. THRESHOLD OPTIMIZATION")
    print("─" * 50)

    # Only optimize on positions that reached Zone 1+ (had meaningful TP progress)
    optimizable = [p for p in position_data 
                   if p["max_pnl"] and p["max_pnl"] > 0
                   and len(p["snapshots"]) > 5
                   and any(s[4] and s[4] > 20 for s in p["snapshots"])]
    
    print(f"  Optimizable positions (reached Zone 1+): {len(optimizable)}")
    
    if len(optimizable) < 5:
        print(f"  ⚠️ Need at least 5 positions that reached Zone 1+ for optimization.")
        print(f"     Currently only {len(optimizable)} — waiting for more data.")
    else:
        # Grid search over dd_thresh values for zones 1-4
        best_avg_cr = -1
        best_thresholds = None
        
        # Search space
        dd1_range = range(30, 70, 5)    # Zone 1: 30%-65%
        dd2_range = range(25, 55, 5)    # Zone 2: 25%-50%
        dd3_range = range(15, 45, 5)    # Zone 3: 15%-40%
        dd4_range = range(10, 35, 5)    # Zone 4: 10%-30%
        
        n_combos = len(dd1_range) * len(dd2_range) * len(dd3_range) * len(dd4_range)
        print(f"  Grid search: {n_combos} combinations...")
        
        tested = 0
        for dd1 in dd1_range:
            for dd2 in dd2_range:
                if dd2 >= dd1:
                    continue  # Zone 2 should be tighter than Zone 1
                for dd3 in dd3_range:
                    if dd3 >= dd2:
                        continue
                    for dd4 in dd4_range:
                        if dd4 >= dd3:
                            continue
                        
                        test_zones = [
                            {"name": "Zone 0", "min_tp": 0,  "max_tp": 20,  "dd_thresh": None},
                            {"name": "Zone 1", "min_tp": 20, "max_tp": 40,  "dd_thresh": dd1},
                            {"name": "Zone 2", "min_tp": 40, "max_tp": 60,  "dd_thresh": dd2},
                            {"name": "Zone 3", "min_tp": 60, "max_tp": 80,  "dd_thresh": dd3},
                            {"name": "Zone 4", "min_tp": 80, "max_tp": 999, "dd_thresh": dd4},
                        ]
                        
                        crs = []
                        for p in optimizable:
                            idx, exit_pnl, reason = simulate_zone_policy(p["snapshots"], test_zones)
                            max_p = p["max_pnl"]
                            cr = exit_pnl / max_p if max_p > 0 else 0
                            crs.append(cr)
                        
                        avg_cr = sum(crs) / len(crs) if crs else 0
                        tested += 1
                        
                        if avg_cr > best_avg_cr:
                            best_avg_cr = avg_cr
                            best_thresholds = (dd1, dd2, dd3, dd4)
        
        print(f"  Tested: {tested} valid combinations")
        
        # Current performance
        current_crs = []
        for p in optimizable:
            idx, exit_pnl, reason = simulate_zone_policy(
                p["snapshots"],
                [{"name": f"Zone {z['name'][-1]}", "min_tp": z["min_tp"], "max_tp": z["max_tp"], "dd_thresh": z["dd_thresh"]}
                 for z in CURRENT_ZONES]
            )
            cr = exit_pnl / p["max_pnl"] if p["max_pnl"] > 0 else 0
            current_crs.append(cr)
        current_avg_cr = sum(current_crs) / len(current_crs) if current_crs else 0
        
        print(f"\n  ┌─────────────────────────────────────────────────┐")
        print(f"  │ RESULTS                                         │")
        print(f"  ├─────────────────────────────────────────────────┤")
        print(f"  │ Current thresholds: Z1={CURRENT_ZONES[1]['dd_thresh']}% Z2={CURRENT_ZONES[2]['dd_thresh']}% "
              f"Z3={CURRENT_ZONES[3]['dd_thresh']}% Z4={CURRENT_ZONES[4]['dd_thresh']}%  │")
        print(f"  │ Current avg capture_ratio: {current_avg_cr:>+.2%}              │")
        print(f"  │─────────────────────────────────────────────────│")
        if best_thresholds:
            print(f"  │ Optimal thresholds: Z1={best_thresholds[0]}% Z2={best_thresholds[1]}% "
                  f"Z3={best_thresholds[2]}% Z4={best_thresholds[3]}%  │")
            print(f"  │ Optimal avg capture_ratio: {best_avg_cr:>+.2%}              │")
            improvement = best_avg_cr - current_avg_cr
            print(f"  │ Improvement: {improvement:>+.2%}                             │")
        print(f"  └─────────────────────────────────────────────────┘")
        
        if best_thresholds and "--output" in sys.argv:
            print(f"\n  → Update config.py with:")
            print(f"    ZONE_DD_THRESH_1 = {best_thresholds[0]}")
            print(f"    ZONE_DD_THRESH_2 = {best_thresholds[1]}")
            print(f"    ZONE_DD_THRESH_3 = {best_thresholds[2]}")
            print(f"    ZONE_DD_THRESH_4 = {best_thresholds[3]}")

        # ── AUTO-SAVE to zone_calibration table ──────────────────
        if best_thresholds:
            try:
                # Deactivate old calibrations
                conn.execute("UPDATE zone_calibration SET is_active = 0 WHERE is_active = 1")
                # Insert new calibration
                conn.execute("""
                    INSERT INTO zone_calibration (
                        calibrated_at, zone_1_dd_thresh, zone_2_dd_thresh,
                        zone_3_dd_thresh, zone_4_dd_thresh,
                        capture_ratio_avg, n_positions_used, is_active
                    ) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, 1)
                """, (
                    best_thresholds[0], best_thresholds[1],
                    best_thresholds[2], best_thresholds[3],
                    round(best_avg_cr, 4), len(optimizable),
                ))
                conn.commit()
                print(f"\n  ✅ Saved to zone_calibration (is_active=1)")
                print(f"     Thresholds will auto-load on next service restart.")
            except Exception as e:
                print(f"\n  ⚠️ Failed to save to DB: {e}")
                print(f"     Creating table and retrying...")
                try:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS zone_calibration (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            calibrated_at TEXT,
                            zone_1_dd_thresh REAL,
                            zone_2_dd_thresh REAL,
                            zone_3_dd_thresh REAL,
                            zone_4_dd_thresh REAL,
                            capture_ratio_avg REAL,
                            n_positions_used INTEGER,
                            is_active INTEGER DEFAULT 1
                        )
                    """)
                    conn.execute("""
                        INSERT INTO zone_calibration (
                            calibrated_at, zone_1_dd_thresh, zone_2_dd_thresh,
                            zone_3_dd_thresh, zone_4_dd_thresh,
                            capture_ratio_avg, n_positions_used, is_active
                        ) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, 1)
                    """, (
                        best_thresholds[0], best_thresholds[1],
                        best_thresholds[2], best_thresholds[3],
                        round(best_avg_cr, 4), len(optimizable),
                    ))
                    conn.commit()
                    print(f"  ✅ Created table and saved calibration.")
                except Exception as e2:
                    print(f"  ❌ Fatal: {e2}")

    # ── 5. Per-Symbol Analysis ────────────────────────────────────
    print("\n" + "─" * 50)
    print("5. PER-SYMBOL CAPTURE ANALYSIS (top symbols)")
    print("─" * 50)
    
    sym_data = defaultdict(lambda: {"count": 0, "cr_sum": 0, "pnl_sum": 0})
    for p in position_data:
        if p["max_pnl"] and p["max_pnl"] > 0:
            sym_data[p["symbol"]]["count"] += 1
            sym_data[p["symbol"]]["cr_sum"] += p["capture_ratio"]
            sym_data[p["symbol"]]["pnl_sum"] += p["realized_pnl"] or 0
    
    sorted_syms = sorted(sym_data.items(), key=lambda x: -x[1]["count"])
    print(f"  {'Symbol':<15} {'Trades':>7} {'Avg CR':>8} {'Avg PnL':>9}")
    for sym, d in sorted_syms[:15]:
        avg_cr = d["cr_sum"] / d["count"] if d["count"] > 0 else 0
        avg_pnl = d["pnl_sum"] / d["count"] if d["count"] > 0 else 0
        print(f"  {sym:<15} {d['count']:>7} {avg_cr:>+7.2%} {avg_pnl:>+8.2f}%")

    # ── 6. Recommendation Score Effectiveness ─────────────────────
    print("\n" + "─" * 50)
    print("6. RE RECOMMENDATION EFFECTIVENESS")
    print("─" * 50)
    
    rec_data = defaultdict(lambda: {"count": 0, "cr_sum": 0, "pnl_sum": 0, "wins": 0})
    for p in position_data:
        rec = p["rec"] or "unknown"
        rec_data[rec]["count"] += 1
        if p["max_pnl"] and p["max_pnl"] > 0:
            rec_data[rec]["cr_sum"] += p["capture_ratio"]
        rec_data[rec]["pnl_sum"] += p["realized_pnl"] or 0
        if (p["realized_pnl"] or 0) > 0:
            rec_data[rec]["wins"] += 1
    
    print(f"  {'Rec':<10} {'Trades':>7} {'WinRate':>8} {'Avg CR':>8} {'Avg PnL':>9}")
    for rec in ["approve", "reduce", "reject", "unknown"]:
        if rec in rec_data:
            d = rec_data[rec]
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            avg_cr = d["cr_sum"] / d["count"] if d["count"] > 0 else 0
            avg_pnl = d["pnl_sum"] / d["count"] if d["count"] > 0 else 0
            print(f"  {rec:<10} {d['count']:>7} {wr:>7.0f}% {avg_cr:>+7.2%} {avg_pnl:>+8.2f}%")

    print("\n" + "=" * 70)
    print("Calibration complete.")
    conn.close()


if __name__ == "__main__":
    run_calibration()
