#!/usr/bin/env python3
"""
Optimal Action Labeler for Position Snapshots.

Backfills the `optimal_action` column based on future PnL data:
  - HOLD:    future_pnl_4h > current_pnl * 1.1 (price keeps improving)
  - CLOSE:   future_pnl_4h < -0.5% OR future drawdown from peak is severe
  - PARTIAL: future_pnl_1h < current_pnl * 0.5 (losing momentum)

Designed for PostgreSQL via psycopg2. Processes in batches.
"""

import psycopg2
import sys
import time

DB_DSN = "dbname=riskengine_db user=riskengine host=/var/run/postgresql"
BATCH_SIZE = 5000


def classify(current_pnl, max_pnl, future_1h, future_4h, zone):
    """Determine optimal action for a snapshot."""
    if future_1h is None or future_4h is None:
        return None  # skip — no label data

    cp = float(current_pnl or 0)
    mp = float(max_pnl or 0)
    f1 = float(future_1h)
    f4 = float(future_4h)
    z  = int(zone or 0)

    # --- CLOSE: price will dump significantly ---
    # If future 4h PnL goes deeply negative (< -0.5%)
    if f4 < -0.5:
        return "close"

    # If we're in profit (>1%) and future loses more than half of current
    if cp > 1.0 and f4 < cp * 0.3:
        return "close"

    # If at high zone (3+) and future 4h drops below breakeven
    if z >= 3 and f4 < 0:
        return "close"

    # --- PARTIAL: losing momentum ---
    # Future 1h drops significantly from current
    if cp > 0.5 and f1 < cp * 0.4:
        return "partial"

    # At zone 2+ and 1h future is negative
    if z >= 2 and f1 < 0 and cp > 0:
        return "partial"

    # --- HOLD: price keeps going up ---
    return "hold"


def run():
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    # Count total to process
    cur.execute("""
        SELECT COUNT(*) FROM position_snapshots
        WHERE future_pnl_1h IS NOT NULL AND future_pnl_4h IS NOT NULL
    """)
    total = cur.fetchone()[0]
    print(f"[labeler] Total snapshots with future PnL data: {total}")

    # Count already labeled
    cur.execute("SELECT COUNT(*) FROM position_snapshots WHERE optimal_action IS NOT NULL")
    already = cur.fetchone()[0]
    print(f"[labeler] Already labeled: {already}")

    if already == total:
        print("[labeler] All snapshots already labeled. Nothing to do.")
        # Still show stats
        show_stats(cur)
        cur.close()
        conn.close()
        return

    offset = 0
    labeled = 0
    skipped = 0
    t0 = time.time()
    stats = {"hold": 0, "partial": 0, "close": 0}

    while offset < total:
        cur.execute("""
            SELECT id, pnl_pct, max_pnl_pct, future_pnl_1h, future_pnl_4h, zone
            FROM position_snapshots
            WHERE future_pnl_1h IS NOT NULL
              AND future_pnl_4h IS NOT NULL
              AND optimal_action IS NULL
            ORDER BY id
            LIMIT %s OFFSET %s
        """, (BATCH_SIZE, 0))  # always offset 0 since we're updating

        rows = cur.fetchall()
        if not rows:
            break

        updates = []
        for row_id, cp, mp, f1, f4, z in rows:
            action = classify(cp, mp, f1, f4, z)
            if action:
                updates.append((action, row_id))
                stats[action] += 1
                labeled += 1
            else:
                skipped += 1

        if updates:
            cur.executemany(
                "UPDATE position_snapshots SET optimal_action = %s WHERE id = %s",
                updates
            )
            conn.commit()

        offset += BATCH_SIZE
        elapsed = time.time() - t0
        pct = min(100, (labeled + skipped) / max(total, 1) * 100)
        print(f"[labeler] Progress: {labeled + skipped}/{total} ({pct:.0f}%) — "
              f"labeled={labeled} skipped={skipped} — {elapsed:.1f}s")

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"[labeler] DONE in {elapsed:.1f}s")
    print(f"[labeler] Labeled: {labeled} | Skipped (no future data): {skipped}")
    print(f"[labeler] Distribution:")
    for action, count in sorted(stats.items(), key=lambda x: -x[1]):
        pct = count / max(labeled, 1) * 100
        bar = "█" * int(pct / 2)
        print(f"  {action:>8}: {count:>6} ({pct:>5.1f}%) {bar}")
    print(f"{'='*60}")

    show_stats(cur)
    cur.close()
    conn.close()


def show_stats(cur):
    """Show final label distribution from DB."""
    cur.execute("""
        SELECT optimal_action, COUNT(*)
        FROM position_snapshots
        WHERE optimal_action IS NOT NULL
        GROUP BY optimal_action
        ORDER BY COUNT(*) DESC
    """)
    rows = cur.fetchall()
    if rows:
        total = sum(r[1] for r in rows)
        print(f"\n[labeler] DB Distribution ({total} labeled):")
        for action, cnt in rows:
            print(f"  {action:>8}: {cnt:>6} ({cnt/total*100:.1f}%)")


if __name__ == "__main__":
    run()
