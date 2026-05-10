#!/usr/bin/env python3
"""
ML Dataset Export — v0.18.6

Exports labeled position snapshots from PostgreSQL to CSV for XGBoost training.

Features (10 core, 100% fill rate):
  pnl_pct, max_pnl_pct, drawdown_pct, tp_progress_pct, hours_open,
  zone, volatility, volume_ratio, mc_p_tp, mc_p_sl

Engineered (4):
  mc_edge, pnl_to_max_ratio, zone_x_tp_progress, funding_rate_change

Conditional (4):
  funding_rate, oi_change_pct, spread_pct, trend_sum

Intelligence (4):
  btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio

MC Reform (2, v0.18.1):
  full_e_pnl, pnl_skewness

Regime (1): market_regime

Target: optimal_action (hold / partial_close / close)

Output: data/ml_dataset_v2.csv

Usage:
  cd /opt/risk-engine/src && python3 scripts/export_ml_dataset.py
"""

import sys
import os
import csv
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_adapter import _sync_fetch_all, _sync_fetch_val, PG_DB, PG_USER, PG_HOST
import db_adapter
import psycopg2.pool
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ml-export")

# Feature columns to export
CORE_FEATURES = [
    "pnl_pct", "max_pnl_pct", "drawdown_pct", "tp_progress_pct", "hours_open",
    "zone", "volatility", "volume_ratio", "mc_p_tp", "mc_p_sl",
]

CONDITIONAL_FEATURES = [
    "funding_rate", "oi_change_pct", "spread_pct", "trend_sum",
]

INTEL_FEATURES = [
    "btc_change_1h", "rsi_14", "orderbook_imbalance", "long_short_ratio",
    "market_regime",
]

MC_REFORM_FEATURES = [
    "full_e_pnl", "pnl_skewness",
]

ENGINEERED_FEATURES = [
    "mc_edge", "pnl_to_max_ratio", "zone_x_tp_progress", "funding_rate_change",
]

META_COLUMNS = ["pos_id", "snapshot_id", "symbol", "side", "signal_score"]
TARGET = "optimal_action"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ml_dataset_v2.csv")


def _ensure_pool():
    if db_adapter._pool is None:
        pg_options = "-c jit=off -c statement_timeout=60000 -c work_mem=32MB"
        db_adapter._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
            dbname=PG_DB, user=PG_USER, host=PG_HOST,
            options=pg_options,
        )
        logger.info(f"PostgreSQL pool initialized (DB={PG_DB})")


def export_dataset():
    _ensure_pool()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Count labeled snapshots
    total = _sync_fetch_val(
        "SELECT COUNT(*) FROM position_snapshots WHERE optimal_action IS NOT NULL"
    )
    logger.info(f"Total labeled snapshots: {total}")

    # Query all labeled snapshots with position context
    logger.info("Fetching labeled snapshots from PostgreSQL...")
    rows = _sync_fetch_all("""
        SELECT
            ps.id as snapshot_id,
            ps.pos_id,
            ps.symbol,
            ps.side,
            ps.pnl_pct,
            ps.max_pnl_pct,
            ps.drawdown_pct,
            ps.tp_progress_pct,
            ps.hours_open,
            ps.zone,
            ps.volatility,
            ps.volume_ratio,
            ps.mc_p_tp,
            ps.mc_p_sl,
            ps.mc_var,
            ps.signal_score,
            ps.funding_rate,
            ps.oi_change_pct,
            ps.spread_pct,
            ps.trend_sum,
            ps.btc_change_1h,
            ps.rsi_14,
            ps.orderbook_imbalance,
            ps.long_short_ratio,
            ps.regime as market_regime,
            ps.full_e_pnl,
            ps.pnl_skewness,
            ps.optimal_action
        FROM position_snapshots ps
        WHERE ps.optimal_action IS NOT NULL
          AND ps.optimal_action IN ('hold', 'close', 'partial_close')
        ORDER BY ps.pos_id, ps.id
    """)
    logger.info(f"Fetched {len(rows)} rows")

    # Write CSV with engineered features
    all_columns = META_COLUMNS + CORE_FEATURES + ENGINEERED_FEATURES + CONDITIONAL_FEATURES + INTEL_FEATURES + MC_REFORM_FEATURES + [TARGET]

    written = 0
    prev_funding_by_pos = {}
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns)
        writer.writeheader()

        for row in rows:
            # Core features (default 0 for nulls)
            out = {
                "pos_id": row["pos_id"],
                "snapshot_id": row["snapshot_id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "signal_score": row["signal_score"] or 0,
            }

            for feat in CORE_FEATURES:
                out[feat] = row.get(feat) or 0

            # Engineered features
            mc_p_tp = row["mc_p_tp"] or 0
            mc_p_sl = row["mc_p_sl"] or 0
            max_pnl = row["max_pnl_pct"] or 0
            pnl = row["pnl_pct"] or 0
            zone = row["zone"] or 0
            tp_prog = row["tp_progress_pct"] or 0

            out["mc_edge"] = round(mc_p_tp - mc_p_sl, 4)
            out["pnl_to_max_ratio"] = round(pnl / max_pnl, 4) if max_pnl > 0 else 0
            out["zone_x_tp_progress"] = round(zone * tp_prog / 100.0, 4)

            # Conditional features
            for feat in CONDITIONAL_FEATURES:
                out[feat] = row.get(feat) or 0

            # ML Intelligence features (v0.13.2)
            for feat in INTEL_FEATURES:
                val = row.get(feat)
                if feat == "market_regime":
                    out[feat] = val or "normal"
                else:
                    out[feat] = val or 0

            # MC Reform features (v0.18.1)
            for feat in MC_REFORM_FEATURES:
                out[feat] = row.get(feat) or 0

            # Engineered: funding_rate_change (delta from prev snapshot by pos_id)
            pid = row["pos_id"]
            cur_funding = row.get("funding_rate") or 0
            prev_funding = prev_funding_by_pos.get(pid, cur_funding)
            out["funding_rate_change"] = round(cur_funding - prev_funding, 8)
            prev_funding_by_pos[pid] = cur_funding

            # Target
            out[TARGET] = row["optimal_action"]

            writer.writerow(out)
            written += 1

    logger.info(f"✅ Written {written} rows to {OUTPUT_FILE}")

    # Print class distribution
    class_dist = {}
    for row in rows:
        label = row["optimal_action"]
        class_dist[label] = class_dist.get(label, 0) + 1

    logger.info("📊 Class distribution:")
    for label, count in sorted(class_dist.items(), key=lambda x: -x[1]):
        pct = count / written * 100
        logger.info(f"  {label:15s} → {count:,} ({pct:.1f}%)")

    # Print feature fill rates
    logger.info("📊 Conditional feature fill rates:")
    for feat in CONDITIONAL_FEATURES + INTEL_FEATURES + MC_REFORM_FEATURES:
        if feat == "market_regime":
            filled = sum(1 for r in rows if r.get(feat) not in (None, "", "normal"))
        else:
            filled = sum(1 for r in rows if (r.get(feat) or 0) != 0)
        pct = filled / written * 100
        logger.info(f"  {feat:25s} → {filled:,}/{written:,} ({pct:.1f}%)")

    return OUTPUT_FILE


if __name__ == "__main__":
    t0 = time.time()
    path = export_dataset()
    logger.info(f"⏱️  Total time: {time.time() - t0:.1f}s")
    logger.info(f"📁 Dataset: {path}")
