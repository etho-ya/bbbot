#!/usr/bin/env python3
"""
XGBoost Exit Model Trainer — v0.18.6

Trains an XGBoost classifier to predict optimal exit actions for position management.

Classes: hold (0), partial_close (1), close (2)

Features (25 numeric):
  Core (10):        pnl_pct, max_pnl_pct, drawdown_pct, tp_progress_pct, hours_open,
                    zone, volatility, volume_ratio, mc_p_tp, mc_p_sl
  Engineered (4):   mc_edge, pnl_to_max_ratio, zone_x_tp_progress, funding_rate_change
  Conditional (4):  funding_rate, oi_change_pct, spread_pct, trend_sum
  Intelligence (4): btc_change_1h, rsi_14, orderbook_imbalance, long_short_ratio
  MC Reform (2):    full_e_pnl, pnl_skewness (v0.18.1, available since Apr 2)
  Regime (1):       market_regime → ordinal encoded

Usage:
  cd /opt/risk-engine/src && python3 scripts/export_ml_dataset.py
  python3 scripts/train_exit_model.py

Output:
  data/xgboost_exit_v3.json     — trained model (v3, 25 features)
  data/feature_importances.csv  — feature ranking
"""

import sys
import os
import csv
import time
import json
import numpy as np
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ml-train")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DATASET_FILE = os.path.join(DATA_DIR, "ml_dataset_v2.csv")
MODEL_FILE_V3 = os.path.join(DATA_DIR, "xgboost_exit_v3.json")
MODEL_FILE_V2 = os.path.join(DATA_DIR, "xgboost_exit_v2.json")
IMPORTANCE_FILE = os.path.join(DATA_DIR, "feature_importances.csv")

FEATURE_COLS = [
    # Core (10)
    "pnl_pct", "max_pnl_pct", "drawdown_pct", "tp_progress_pct", "hours_open",
    "zone", "volatility", "volume_ratio", "mc_p_tp", "mc_p_sl",
    # Engineered (4)
    "mc_edge", "pnl_to_max_ratio", "zone_x_tp_progress", "funding_rate_change",
    # Conditional (4)
    "funding_rate", "oi_change_pct", "spread_pct", "trend_sum",
    # Intelligence (4)
    "btc_change_1h", "rsi_14", "orderbook_imbalance", "long_short_ratio",
    # MC Reform (2) — v0.18.1
    "full_e_pnl", "pnl_skewness",
    # Regime (1) — ordinal encoded
    "market_regime_encoded",
]

REGIME_MAP = {
    "normal": 0, "trending": 1, "ranging": 2,
    "volatile": 3, "low_liquidity": 4, "": 0,
}

LABEL_MAP = {"hold": 0, "partial_close": 1, "close": 2}
LABEL_NAMES = ["hold", "partial_close", "close"]


def load_dataset():
    """Load CSV dataset, return features (X), labels (y), and position IDs."""
    logger.info(f"Loading dataset from {DATASET_FILE}")

    X_rows = []
    y_rows = []
    pos_ids = []

    with open(DATASET_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row.get("optimal_action", "")
            if label not in LABEL_MAP:
                continue

            features = []
            for col in FEATURE_COLS:
                if col == "market_regime_encoded":
                    regime = row.get("market_regime", "normal") or "normal"
                    features.append(float(REGIME_MAP.get(regime, 0)))
                else:
                    val = row.get(col, "0")
                    try:
                        features.append(float(val))
                    except (ValueError, TypeError):
                        features.append(0.0)

            X_rows.append(features)
            y_rows.append(LABEL_MAP[label])
            pos_ids.append(int(row.get("pos_id", 0)))

    X = np.array(X_rows, dtype=np.float32)
    y = np.array(y_rows, dtype=np.int32)
    pos_ids = np.array(pos_ids, dtype=np.int32)

    logger.info(f"Loaded {len(X):,} samples, {len(FEATURE_COLS)} features")

    # Report fill rates for intelligence features
    intel_cols = ["btc_change_1h", "rsi_14", "orderbook_imbalance", "long_short_ratio"]
    intel_indices = [FEATURE_COLS.index(c) for c in intel_cols]
    logger.info("Intelligence feature fill rates:")
    for col, idx in zip(intel_cols, intel_indices):
        non_zero = np.count_nonzero(X[:, idx])
        pct = non_zero / len(X) * 100
        logger.info(f"  {col:25s} → {non_zero:,}/{len(X):,} ({pct:.1f}%)")

    return X, y, pos_ids


def time_based_split(X, y, pos_ids, test_ratio=0.2):
    """Split by position ID (time-ordered) to prevent data leakage."""
    unique_pos = sorted(set(pos_ids))
    n_train_pos = int(len(unique_pos) * (1 - test_ratio))
    train_pos = set(unique_pos[:n_train_pos])

    train_mask = np.array([pid in train_pos for pid in pos_ids])
    test_mask = ~train_mask

    logger.info(
        f"Split: {sum(train_mask):,} train ({n_train_pos} pos) / "
        f"{sum(test_mask):,} test ({len(unique_pos) - n_train_pos} pos)"
    )
    return X[train_mask], X[test_mask], y[train_mask], y[test_mask]


def compute_class_weights(y):
    """Compute inverse frequency weights for imbalanced classes."""
    counts = np.bincount(y, minlength=3)
    total = len(y)
    weights = np.zeros(3)
    for i in range(3):
        if counts[i] > 0:
            weights[i] = total / (3.0 * counts[i])
    logger.info(f"Class weights: {dict(zip(LABEL_NAMES, weights.round(2)))}")
    return weights


def train_model(X_train, y_train, class_weights):
    """Train XGBoost classifier with class weights."""
    import xgboost as xgb

    sample_weights = np.array([class_weights[yi] for yi in y_train])

    dtrain = xgb.DMatrix(X_train, label=y_train, weight=sample_weights,
                         feature_names=FEATURE_COLS)

    params = {
        "objective": "multi:softprob",
        "num_class": 3,
        "max_depth": 7,             # v2: deeper trees (6→7) for richer features
        "learning_rate": 0.05,       # v2: lower LR (0.1→0.05) + more rounds
        "subsample": 0.8,
        "colsample_bytree": 0.7,     # v2: slightly lower (0.8→0.7) for 23 features
        "min_child_weight": 8,       # v2: slightly lower (10→8) for minority classes
        "gamma": 0.1,                # v2: regularization
        "reg_alpha": 0.1,            # v2: L1 regularization
        "reg_lambda": 1.0,           # v2: L2 regularization
        "eval_metric": "mlogloss",
        "tree_method": "hist",
        "verbosity": 0,
        "seed": 42,
    }

    logger.info(f"Training XGBoost v3 ({len(FEATURE_COLS)} features)...")
    model = xgb.train(
        params, dtrain,
        num_boost_round=500,
        verbose_eval=False,
    )

    return model


def evaluate_model(model, X_test, y_test, version="v2"):
    """Print classification metrics."""
    import xgboost as xgb
    from sklearn.metrics import classification_report, confusion_matrix

    dtest = xgb.DMatrix(X_test, feature_names=FEATURE_COLS)
    y_proba = model.predict(dtest)
    y_pred = np.argmax(y_proba, axis=1)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"CLASSIFICATION REPORT ({version})")
    logger.info("=" * 60)
    report = classification_report(y_test, y_pred, target_names=LABEL_NAMES, digits=3)
    logger.info("\n" + report)

    cm = confusion_matrix(y_test, y_pred)
    logger.info("Confusion Matrix (rows=actual, cols=predicted):")
    header = f"{'':15s} " + " ".join(f"{n:>13s}" for n in LABEL_NAMES)
    logger.info(header)
    for i, row in enumerate(cm):
        line = f"{LABEL_NAMES[i]:15s} " + " ".join(f"{v:13d}" for v in row)
        logger.info(line)

    logger.info("\nPer-class accuracy:")
    results = {}
    for i, name in enumerate(LABEL_NAMES):
        if cm[i].sum() > 0:
            acc = cm[i][i] / cm[i].sum() * 100
            logger.info(f"  {name:15s} → {acc:.1f}% ({cm[i][i]}/{cm[i].sum()})")
            results[name] = {"accuracy": acc, "correct": int(cm[i][i]), "total": int(cm[i].sum())}

    # Confidence stats
    max_proba = np.max(y_proba, axis=1)
    logger.info(f"\nPrediction confidence: mean={max_proba.mean():.3f}, "
                f"median={np.median(max_proba):.3f}, min={max_proba.min():.3f}")

    return y_pred, results


def save_feature_importances(model):
    """Save feature importances to CSV."""
    importance = model.get_score(importance_type='gain')

    sorted_imp = sorted(importance.items(), key=lambda x: -x[1])

    with open(IMPORTANCE_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["feature", "importance_gain", "rank"])
        for rank, (feat, score) in enumerate(sorted_imp, 1):
            writer.writerow([feat, round(score, 4), rank])

    logger.info("\n📊 Feature Importances (by gain):")
    for rank, (feat, score) in enumerate(sorted_imp, 1):
        bar = "█" * min(int(score / sorted_imp[0][1] * 30), 30)
        tag = " ★NEW" if feat in (
            "btc_change_1h", "rsi_14", "orderbook_imbalance",
            "long_short_ratio", "market_regime_encoded", "funding_rate_change",
            "full_e_pnl", "pnl_skewness"
        ) else ""
        logger.info(f"  {rank:2d}. {feat:25s} {score:10.2f}  {bar}{tag}")

    logger.info(f"\nSaved to {IMPORTANCE_FILE}")
    return sorted_imp


def main():
    t0 = time.time()

    # Load data
    X, y, pos_ids = load_dataset()

    # Class distribution
    logger.info("Class distribution:")
    for i, name in enumerate(LABEL_NAMES):
        count = np.sum(y == i)
        pct = count / len(y) * 100
        logger.info(f"  {name:15s} → {count:,} ({pct:.1f}%)")

    # Split
    X_train, X_test, y_train, y_test = time_based_split(X, y, pos_ids, test_ratio=0.2)

    # Class weights
    class_weights = compute_class_weights(y_train)

    # Train v2
    model = train_model(X_train, y_train, class_weights)

    # Evaluate v3
    y_pred, results_v3 = evaluate_model(model, X_test, y_test, version="v3")

    # Feature importances
    sorted_imp = save_feature_importances(model)

    # Save model
    model.save_model(MODEL_FILE_V3)
    logger.info(f"\n💾 Model saved to {MODEL_FILE_V3}")

    # ── v3 Summary ──────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("v3 SUMMARY")
    logger.info("=" * 60)
    for name in LABEL_NAMES:
        if name in results_v3:
            logger.info(f"v3 {name:15s}: accuracy={results_v3[name]['accuracy']:.1f}%")
    logger.info(f"v3 features: {len(FEATURE_COLS)}")
    logger.info(f"v3 top feature: {sorted_imp[0][0]} (gain={sorted_imp[0][1]:.1f})")

    new_feats = {"btc_change_1h", "rsi_14", "orderbook_imbalance",
                 "long_short_ratio", "market_regime_encoded", "funding_rate_change",
                 "full_e_pnl", "pnl_skewness"}
    new_total = sum(s for f, s in sorted_imp if f in new_feats)
    all_total = sum(s for _, s in sorted_imp)
    logger.info(f"v3 new feature contribution: {new_total:.1f}/{all_total:.1f} "
                f"({new_total/all_total*100:.1f}% of total gain)")

    logger.info(f"\n⏱️  Total time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
