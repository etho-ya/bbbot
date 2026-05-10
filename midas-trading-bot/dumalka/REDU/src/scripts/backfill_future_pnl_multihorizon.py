#!/usr/bin/env python3
"""
Backfill future_pnl_12h / future_pnl_max_24h — v0.19.2 2026-04-04
===================================================================

Retroactively fills the new multi-horizon ML columns for ALL position snapshots
using snapshot-to-snapshot deltas (PostgreSQL).

For snapshot S at time T with pnl_pct P:
  future_pnl_12h    = pnl_pct(T+12h) - P   (point-in-time, closest snapshot in ±2h window)
  future_pnl_max_24h = max(pnl_pct[T..T+24h]) - P   (peak forward PnL within 24h)

If no snapshot exists at T+12h (position closed earlier), uses realized_pnl_pct.
For future_pnl_max_24h, walks all forward snapshots within 24h and takes the max.

Also backfills future_pnl_1h/4h if still NULL (same logic as original backfill,
ported to PostgreSQL).

Usage:
  cd /opt/risk-engine/src && python3 scripts/backfill_future_pnl_multihorizon.py
"""

import sys
import os
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_adapter import (
    _sync_fetch_all, _sync_execute, _sync_executemany,
    PG_DB, PG_USER, PG_HOST,
)
import db_adapter
import psycopg2
import psycopg2.pool
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill-multihorizon")

HORIZONS = [
    ("future_pnl_1h", timedelta(hours=1), timedelta(minutes=30)),
    ("future_pnl_4h", timedelta(hours=4), timedelta(hours=2)),
    ("future_pnl_12h", timedelta(hours=12), timedelta(hours=2)),
]


def _ensure_pool():
    if db_adapter._pool is None:
        pg_options = "-c jit=off -c statement_timeout=120000 -c work_mem=64MB"
        db_adapter._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
            dbname=PG_DB, user=PG_USER, host=PG_HOST,
            options=pg_options,
        )
        logger.info(f"PostgreSQL pool initialized (DB={PG_DB})")


def parse_ts(ts_val):
    if ts_val is None:
        return None
    if isinstance(ts_val, datetime):
        return ts_val
    try:
        return datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
    except Exception:
        return None


def backfill():
    _ensure_pool()

    positions = _sync_fetch_all("""
        SELECT DISTINCT ps.pos_id, op.status, op.realized_pnl_pct,
               op.closed_at, op.max_pnl_pct
        FROM position_snapshots ps
        LEFT JOIN open_positions op ON ps.pos_id = op.id
        ORDER BY ps.pos_id
    """)
    logger.info(f"Found {len(positions)} positions with snapshots")

    stats = {col: 0 for col, _, _ in HORIZONS}
    stats["future_pnl_max_24h"] = 0
    total_positions = 0

    for pos in positions:
        pos_id = pos["pos_id"]
        status = pos["status"] or "unknown"
        final_pnl = pos["realized_pnl_pct"]
        max_pnl_pct = pos["max_pnl_pct"]
        closed_at = parse_ts(pos["closed_at"])

        snaps = _sync_fetch_all("""
            SELECT id, snapshot_at, pnl_pct, max_pnl_pct as snap_max_pnl,
                   future_pnl_1h, future_pnl_4h, future_pnl_12h, future_pnl_max_24h
            FROM position_snapshots
            WHERE pos_id = %s
            ORDER BY snapshot_at ASC
        """, (pos_id,))

        if len(snaps) < 2:
            continue

        snap_data = []
        for s in snaps:
            ts = parse_ts(s["snapshot_at"])
            if ts:
                snap_data.append({
                    "id": s["id"],
                    "ts": ts,
                    "pnl": s["pnl_pct"] or 0.0,
                    "snap_max_pnl": s["snap_max_pnl"] or 0.0,
                    "future_pnl_1h": s["future_pnl_1h"],
                    "future_pnl_4h": s["future_pnl_4h"],
                    "future_pnl_12h": s["future_pnl_12h"],
                    "future_pnl_max_24h": s["future_pnl_max_24h"],
                })

        if not snap_data:
            continue

        batches = {col: [] for col, _, _ in HORIZONS}
        batch_max_24h = []

        for i, snap in enumerate(snap_data):
            snap_ts = snap["ts"]
            snap_pnl = snap["pnl"]

            # Fixed-horizon columns (1h, 4h, 12h)
            for col_name, target_delta, tolerance in HORIZONS:
                if snap[col_name] is not None:
                    continue

                target_ts = snap_ts + target_delta
                best = None
                best_delta = timedelta(hours=999)

                for j in range(i + 1, len(snap_data)):
                    delta = snap_data[j]["ts"] - target_ts
                    abs_delta = abs(delta)
                    if abs_delta < best_delta:
                        best_delta = abs_delta
                        best = snap_data[j]
                    elif delta > tolerance:
                        break

                if best and best_delta < tolerance:
                    val = best["pnl"] - snap_pnl
                    batches[col_name].append((val, snap["id"]))
                elif status == "closed" and final_pnl is not None:
                    close_delta = target_delta * 1.5
                    if closed_at and (closed_at - snap_ts) < close_delta:
                        val = final_pnl - snap_pnl
                        batches[col_name].append((val, snap["id"]))

            # Max 24h: walk all forward snapshots within 24h, find peak PnL
            if snap["future_pnl_max_24h"] is not None:
                continue

            max_forward_pnl = snap_pnl  # at minimum, current PnL
            window_end = snap_ts + timedelta(hours=24)

            for j in range(i + 1, len(snap_data)):
                if snap_data[j]["ts"] > window_end:
                    break
                if snap_data[j]["pnl"] > max_forward_pnl:
                    max_forward_pnl = snap_data[j]["pnl"]
                if snap_data[j]["snap_max_pnl"] > max_forward_pnl:
                    max_forward_pnl = snap_data[j]["snap_max_pnl"]

            # Also consider position-level max_pnl if available
            if max_pnl_pct is not None and max_pnl_pct > max_forward_pnl:
                max_forward_pnl = max_pnl_pct

            val = max_forward_pnl - snap_pnl
            batch_max_24h.append((val, snap["id"]))

        for col_name, _, _ in HORIZONS:
            if batches[col_name]:
                _sync_executemany(
                    f"UPDATE position_snapshots SET {col_name} = %s WHERE id = %s",
                    batches[col_name],
                )
                stats[col_name] += len(batches[col_name])

        if batch_max_24h:
            _sync_executemany(
                "UPDATE position_snapshots SET future_pnl_max_24h = %s WHERE id = %s",
                batch_max_24h,
            )
            stats["future_pnl_max_24h"] += len(batch_max_24h)

        total_positions += 1
        if total_positions % 50 == 0:
            logger.info(f"  Progress: {total_positions}/{len(positions)} positions")

    # Final stats
    total_snaps = _sync_fetch_all(
        "SELECT count(*) as n FROM position_snapshots"
    )[0]["n"]

    fill_rates = {}
    for col in ["future_pnl_1h", "future_pnl_4h", "future_pnl_12h", "future_pnl_max_24h"]:
        filled = _sync_fetch_all(
            f"SELECT count(*) as n FROM position_snapshots WHERE {col} IS NOT NULL"
        )[0]["n"]
        fill_rates[col] = (filled, total_snaps)

    logger.info(f"Backfill complete across {total_positions} positions:")
    for col, count in stats.items():
        logger.info(f"  Updated {col}: {count}")
    logger.info(f"Fill rates:")
    for col, (filled, total) in fill_rates.items():
        pct = filled * 100 / total if total > 0 else 0
        logger.info(f"  {col}: {filled}/{total} ({pct:.1f}%)")


if __name__ == "__main__":
    t0 = time.time()
    backfill()
    logger.info(f"Total time: {time.time() - t0:.1f}s")
