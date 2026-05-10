#!/usr/bin/env python3
"""
Optimal Action Labeler — v0.19.2 (Multi-Horizon + Hard SL Cap Guard)

Retroactively labels position_snapshots with optimal_action for ML training.

For each CLOSED position:
  - Takes all snapshots ordered by time
  - Uses future_pnl_1h/4h/12h/max_24h to determine what action WOULD HAVE been optimal

Labels:
  - "close"          → future PnL drops significantly and never recovers within cap
  - "partial_close"  → moderate decline from profit zone (should have taken partial)
  - "hold"           → future PnL is flat/positive, or temporary dip before recovery

Hard SL Cap Guard (v0.19.2):
  If drawdown from snapshot exceeds MAX_LOSS_PCT (Hard SL Cap), position would be
  force-closed in practice. Label must be "close" regardless of future_pnl_max_24h,
  because the system would never reach the recovery. Prevents ML from learning to
  hold through impossible drawdowns.

Multi-Horizon Logic (v0.19.2):
  When short-term PnL is negative but future_pnl_max_24h shows significant recovery
  (and drawdown stays within Hard SL Cap), label is "hold" — teaches ML that temporary
  dips before parabolic moves should be tolerated.

Usage:
  cd /opt/risk-engine/src && python3 scripts/label_optimal_actions.py

v0.19.2 2026-04-04: Multi-horizon labels (12h, max_24h), Hard SL Cap guard
v0.13.1: Ported from SQLite to PostgreSQL (db_adapter sync functions)
v0.8.4:  Original SQLite version
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_adapter import (
    _sync_fetch_all, _sync_fetch_val, _sync_execute, _sync_executemany,
    PG_DB, PG_USER, PG_HOST,
)
import db_adapter
import psycopg2
import psycopg2.pool
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("labeler")

MAX_LOSS_CAP = 3.5  # must match config.MAX_LOSS_PCT (Hard SL Cap)
RECOVERY_THRESHOLD = 3.0  # min future_pnl_max_24h to override short-term drop


def _ensure_pool():
    """Initialize psycopg2 pool for standalone script (no asyncio needed)."""
    if db_adapter._pool is None:
        pg_options = "-c jit=off -c statement_timeout=60000 -c work_mem=32MB"
        db_adapter._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
            dbname=PG_DB, user=PG_USER, host=PG_HOST,
            options=pg_options,
        )
        logger.info(f"PostgreSQL pool initialized for labeler (DB={PG_DB})")


def label_actions():
    """Main labeling function — labels optimal_action for all closed positions.

    v0.19.2: Multi-horizon awareness with Hard SL Cap reality check.
    """
    _ensure_pool()

    closed_positions = _sync_fetch_all(
        "SELECT id, realized_pnl_pct FROM open_positions WHERE status = 'closed' ORDER BY id"
    )
    logger.info(f"Found {len(closed_positions)} closed positions")

    total_labeled = 0
    total_skipped = 0
    total_positions_processed = 0
    label_counts = {"close": 0, "partial_close": 0, "hold": 0}

    for pos in closed_positions:
        pos_id = pos["id"]

        snapshots = _sync_fetch_all(
            """SELECT id, pnl_pct, max_pnl_pct, tp_progress_pct,
                      drawdown_pct, future_pnl_1h, future_pnl_4h,
                      future_pnl_12h, future_pnl_max_24h, zone
               FROM position_snapshots
               WHERE pos_id = %s
               ORDER BY id ASC""",
            (pos_id,)
        )

        if not snapshots:
            continue

        batch_updates = []
        for snap in snapshots:
            snap_id = snap["id"]
            future_1h = snap["future_pnl_1h"]
            future_4h = snap["future_pnl_4h"]
            future_12h = snap["future_pnl_12h"]
            future_max_24h = snap["future_pnl_max_24h"]
            tp_progress = snap["tp_progress_pct"] or 0
            zone = snap["zone"] or 0
            drawdown = abs(snap["drawdown_pct"] or 0)

            future_pnl = future_1h if future_1h is not None else future_4h

            if future_pnl is None:
                total_skipped += 1
                continue

            # Hard SL Cap guard: if drawdown exceeds cap, position is force-closed.
            # No recovery is possible — label must reflect reality.
            if drawdown > MAX_LOSS_CAP:
                label = "close"

            elif future_pnl < -1.0:
                # Short-term drop — but check if long-term recovery happens
                # AND is reachable (drawdown stays within Hard SL Cap)
                if (future_max_24h is not None
                        and future_max_24h >= RECOVERY_THRESHOLD
                        and drawdown <= MAX_LOSS_CAP):
                    label = "hold"
                else:
                    label = "close"

            elif future_pnl < -0.3 and tp_progress > 50:
                label = "partial_close"

            elif future_pnl < -0.3 and zone >= 2:
                label = "partial_close"

            elif future_pnl > 0.5:
                label = "hold"

            else:
                label = "hold"

            batch_updates.append((label, snap_id))
            label_counts[label] = label_counts.get(label, 0) + 1

        if batch_updates:
            _sync_executemany(
                "UPDATE position_snapshots SET optimal_action = %s WHERE id = %s",
                batch_updates,
            )
            total_labeled += len(batch_updates)

        total_positions_processed += 1
        if total_positions_processed % 50 == 0:
            logger.info(
                f"  Progress: {total_positions_processed}/{len(closed_positions)} "
                f"positions, {total_labeled} labeled"
            )

    logger.info(f"Labeled {total_labeled} snapshots across {total_positions_processed} positions")
    logger.info(f"Skipped {total_skipped} snapshots (no future_pnl data)")
    logger.info(f"Hard SL Cap guard: MAX_LOSS_CAP={MAX_LOSS_CAP}%, RECOVERY_THRESHOLD={RECOVERY_THRESHOLD}%")

    dist = _sync_fetch_all(
        """SELECT optimal_action, COUNT(*) as cnt
           FROM position_snapshots
           WHERE optimal_action IS NOT NULL
           GROUP BY optimal_action
           ORDER BY cnt DESC"""
    )

    logger.info("Label distribution:")
    for row in dist:
        logger.info(f"  {row['optimal_action']:15s} -> {row['cnt']:,}")


if __name__ == "__main__":
    t0 = time.time()
    label_actions()
    logger.info(f"Total time: {time.time() - t0:.1f}s")
