#!/usr/bin/env python3
"""
Backfill future_pnl_1h / future_pnl_4h — v0.8.5
═════════════════════════════════════════════════

Retroactively fills future_pnl for ALL position snapshots using
snapshot-to-snapshot deltas (correct ML label approach).

For snapshot S at time T:
  future_pnl_1h = pnl_pct(T+1h) - pnl_pct(T)
  future_pnl_4h = pnl_pct(T+4h) - pnl_pct(T)

If no snapshot exists at T+Xh (position closed earlier),
uses the final realized_pnl_pct of the position.

Usage:
  cd /opt/risk-engine/src && python3 scripts/backfill_future_pnl.py
"""

import sqlite3
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import config

DB_PATH = config.DB_PATH
BATCH_SIZE = 500


def parse_ts(ts_str):
    """Parse ISO timestamp, handling various formats."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def backfill():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    # Get all positions (both open and closed) that have snapshots
    positions = conn.execute("""
        SELECT DISTINCT ps.pos_id, op.status, op.realized_pnl_pct, op.closed_at
        FROM position_snapshots ps
        LEFT JOIN open_positions op ON ps.pos_id = op.id
        ORDER BY ps.pos_id
    """).fetchall()

    print(f"Found {len(positions)} positions with snapshots")

    total_updated_1h = 0
    total_updated_4h = 0
    total_skipped = 0

    for pos in positions:
        pos_id = pos["pos_id"]
        status = pos["status"] or "unknown"
        final_pnl = pos["realized_pnl_pct"]
        closed_at = parse_ts(pos["closed_at"])

        # Get all snapshots for this position, ordered by time
        snaps = conn.execute("""
            SELECT id, snapshot_at, pnl_pct, future_pnl_1h, future_pnl_4h
            FROM position_snapshots
            WHERE pos_id = ?
            ORDER BY snapshot_at ASC
        """, (pos_id,)).fetchall()

        if len(snaps) < 2:
            total_skipped += 1
            continue

        # Parse timestamps and build lookup
        snap_data = []
        for s in snaps:
            ts = parse_ts(s["snapshot_at"])
            if ts:
                snap_data.append({
                    "id": s["id"],
                    "ts": ts,
                    "pnl": s["pnl_pct"] or 0.0,
                    "has_1h": s["future_pnl_1h"] is not None,
                    "has_4h": s["future_pnl_4h"] is not None,
                })

        if not snap_data:
            total_skipped += 1
            continue

        batch_1h = []
        batch_4h = []

        for i, snap in enumerate(snap_data):
            # Skip if already filled
            need_1h = not snap["has_1h"]
            need_4h = not snap["has_4h"]

            if not need_1h and not need_4h:
                continue

            snap_ts = snap["ts"]

            # Find the snapshot closest to T+1h (within 30min-90min window)
            if need_1h:
                target_1h = snap_ts + timedelta(hours=1)
                best_1h = None
                best_1h_delta = timedelta(hours=999)

                for j in range(i + 1, len(snap_data)):
                    delta = snap_data[j]["ts"] - target_1h
                    abs_delta = abs(delta)
                    if abs_delta < best_1h_delta:
                        best_1h_delta = abs_delta
                        best_1h = snap_data[j]
                    elif delta > timedelta(hours=1):
                        break  # Past the window, stop looking

                if best_1h and best_1h_delta < timedelta(minutes=30):
                    # Good match within ±30 min of target
                    future_pnl_1h = best_1h["pnl"] - snap["pnl"]
                    batch_1h.append((future_pnl_1h, snap["id"]))
                elif status == "closed" and final_pnl is not None:
                    # Position closed before 1h — use final PnL
                    if closed_at and (closed_at - snap_ts) < timedelta(hours=1.5):
                        future_pnl_1h = final_pnl - snap["pnl"]
                        batch_1h.append((future_pnl_1h, snap["id"]))

            # Find the snapshot closest to T+4h (within 2h-6h window)
            if need_4h:
                target_4h = snap_ts + timedelta(hours=4)
                best_4h = None
                best_4h_delta = timedelta(hours=999)

                for j in range(i + 1, len(snap_data)):
                    delta = snap_data[j]["ts"] - target_4h
                    abs_delta = abs(delta)
                    if abs_delta < best_4h_delta:
                        best_4h_delta = abs_delta
                        best_4h = snap_data[j]
                    elif delta > timedelta(hours=3):
                        break

                if best_4h and best_4h_delta < timedelta(hours=2):
                    future_pnl_4h = best_4h["pnl"] - snap["pnl"]
                    batch_4h.append((future_pnl_4h, snap["id"]))
                elif status == "closed" and final_pnl is not None:
                    if closed_at and (closed_at - snap_ts) < timedelta(hours=6):
                        future_pnl_4h = final_pnl - snap["pnl"]
                        batch_4h.append((future_pnl_4h, snap["id"]))

        # Execute batch updates
        if batch_1h:
            conn.executemany(
                "UPDATE position_snapshots SET future_pnl_1h = ? WHERE id = ?",
                batch_1h
            )
            total_updated_1h += len(batch_1h)

        if batch_4h:
            conn.executemany(
                "UPDATE position_snapshots SET future_pnl_4h = ? WHERE id = ?",
                batch_4h
            )
            total_updated_4h += len(batch_4h)

        # Commit in batches
        if (total_updated_1h + total_updated_4h) % (BATCH_SIZE * 2) < 100:
            conn.commit()

    conn.commit()

    # Final stats
    total_snaps = conn.execute("SELECT count(*) FROM position_snapshots").fetchone()[0]
    filled_1h = conn.execute("SELECT count(*) FROM position_snapshots WHERE future_pnl_1h IS NOT NULL").fetchone()[0]
    filled_4h = conn.execute("SELECT count(*) FROM position_snapshots WHERE future_pnl_4h IS NOT NULL").fetchone()[0]

    print(f"\n{'='*50}")
    print(f"✅ Backfill complete!")
    print(f"  Updated future_pnl_1h: {total_updated_1h}")
    print(f"  Updated future_pnl_4h: {total_updated_4h}")
    print(f"  Skipped (too few snaps): {total_skipped}")
    print(f"\n  Fill rates:")
    print(f"    future_pnl_1h: {filled_1h}/{total_snaps} ({filled_1h*100/total_snaps:.1f}%)")
    print(f"    future_pnl_4h: {filled_4h}/{total_snaps} ({filled_4h*100/total_snaps:.1f}%)")

    conn.close()


if __name__ == "__main__":
    backfill()
