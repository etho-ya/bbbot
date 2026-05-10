#!/usr/bin/env python3
"""
Train and persist ExtraTrees+Optuna shadow model for production prediction logging.

Trains on ALL closed positions with clean MC data (Apr 2+), saves:
  - Fitted ExtraTreesClassifier (Optuna-tuned)
  - Fitted SimpleImputer (median strategy)
  - Feature list, training metadata, LOO AUC

Output: src/models/et_shadow_v1.pkl

Safe to re-run anytime — overwrites the output file.

2026-04-08 (v0.19.6)
"""

import sys, os, time, warnings
import numpy as np
import joblib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import psycopg2
import psycopg2.extras
from sklearn.model_selection import LeaveOneOut, StratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

DB = os.getenv("PG_DB", "riskengine_db")
USER = os.getenv("PG_USER", "riskengine")
HOST = os.getenv("PG_HOST", "/var/run/postgresql")

NUMERIC_FEATURES = [
    "entry_vol", "entry_p_tp", "entry_p_sl", "entry_e_pnl", "entry_skewness",
    "entry_signal_score", "entry_mc_var", "entry_volume_ratio",
    "entry_funding", "entry_oi_change", "entry_btc_1h", "entry_rsi",
    "entry_spread", "entry_trend", "entry_lsr", "entry_ob_imbal",
    "early_avg_p_tp", "early_avg_p_sl", "early_avg_pnl",
    "early_max_pnl", "early_min_pnl", "early_pnl_std",
]
ALL_FEATURES = NUMERIC_FEATURES + ["side_is_long", "re_approve", "corrected_approve"]

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "models", "et_shadow_v1.pkl")


def load_data():
    conn = psycopg2.connect(dbname=DB, user=USER, host=HOST)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        WITH entry_window AS (
            SELECT ps.*,
                   ROW_NUMBER() OVER (PARTITION BY ps.pos_id ORDER BY ps.id ASC) AS rn
            FROM position_snapshots ps
            JOIN open_positions p ON p.id = ps.pos_id
            WHERE p.status = 'closed'
              AND p.opened_at >= '2026-04-02'
              AND p.realized_pnl_pct IS NOT NULL
              AND ps.snapshot_at >= p.opened_at
              AND (ps.mc_p_tp + ps.mc_p_sl) <= 1.01
              AND ps.mc_p_sl > 0
        ),
        agg AS (
            SELECT
                pos_id,
                MAX(CASE WHEN rn = 1 THEN volatility END) AS entry_vol,
                MAX(CASE WHEN rn = 1 THEN mc_p_tp END) AS entry_p_tp,
                MAX(CASE WHEN rn = 1 THEN mc_p_sl END) AS entry_p_sl,
                MAX(CASE WHEN rn = 1 THEN full_e_pnl END) AS entry_e_pnl,
                MAX(CASE WHEN rn = 1 THEN pnl_skewness END) AS entry_skewness,
                MAX(CASE WHEN rn = 1 THEN signal_score END) AS entry_signal_score,
                MAX(CASE WHEN rn = 1 THEN mc_var END) AS entry_mc_var,
                MAX(CASE WHEN rn = 1 THEN volume_ratio END) AS entry_volume_ratio,
                MAX(CASE WHEN rn = 1 THEN funding_rate END) AS entry_funding,
                MAX(CASE WHEN rn = 1 THEN oi_change_pct END) AS entry_oi_change,
                MAX(CASE WHEN rn = 1 THEN btc_change_1h END) AS entry_btc_1h,
                MAX(CASE WHEN rn = 1 THEN rsi_14 END) AS entry_rsi,
                MAX(CASE WHEN rn = 1 THEN spread_pct END) AS entry_spread,
                MAX(CASE WHEN rn = 1 THEN trend_sum END) AS entry_trend,
                MAX(CASE WHEN rn = 1 THEN long_short_ratio END) AS entry_lsr,
                MAX(CASE WHEN rn = 1 THEN orderbook_imbalance END) AS entry_ob_imbal,
                AVG(CASE WHEN rn <= 5 THEN mc_p_tp END) AS early_avg_p_tp,
                AVG(CASE WHEN rn <= 5 THEN mc_p_sl END) AS early_avg_p_sl,
                AVG(CASE WHEN rn <= 5 THEN pnl_pct END) AS early_avg_pnl,
                MAX(CASE WHEN rn <= 5 THEN max_pnl_pct END) AS early_max_pnl,
                MIN(CASE WHEN rn <= 5 THEN pnl_pct END) AS early_min_pnl,
                STDDEV(CASE WHEN rn <= 5 THEN pnl_pct END) AS early_pnl_std,
                COUNT(*) AS total_snaps
            FROM entry_window
            WHERE rn <= 10
            GROUP BY pos_id
            HAVING COUNT(*) >= 3
        )
        SELECT a.*, p.symbol, p.side, p.realized_pnl_pct,
               s.re_recommendation, s.corrected_recommendation
        FROM agg a
        JOIN open_positions p ON p.id = a.pos_id
        LEFT JOIN signals s ON s.signal_hash = p.signal_hash
        ORDER BY a.pos_id
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    n = len(rows)
    X = np.zeros((n, len(ALL_FEATURES)), dtype=np.float32)
    y = np.zeros(n, dtype=np.int32)

    for i, row in enumerate(rows):
        for j, col in enumerate(NUMERIC_FEATURES):
            val = row[col]
            X[i, j] = float(val) if val is not None else np.nan
        base = len(NUMERIC_FEATURES)
        X[i, base] = 1.0 if row["side"] == "long" else 0.0
        X[i, base + 1] = 1.0 if row.get("re_recommendation") == "approve" else 0.0
        X[i, base + 2] = 1.0 if row.get("corrected_recommendation") == "approve" else 0.0
        y[i] = 1 if row["realized_pnl_pct"] > 0 else 0

    return X, y


def run():
    print("=" * 60)
    print("Training shadow model (ExtraTrees + Optuna)")
    print("=" * 60)

    X, y = load_data()
    n_profit = int(y.sum())
    print(f"\n  Positions: {len(y)} (profit: {n_profit}, loss: {len(y) - n_profit})")

    imp = SimpleImputer(strategy="median")
    X_imp = imp.fit_transform(X)

    # Optuna tuning (200 trials)
    print("\n  Running Optuna (200 trials, 5-fold CV)...", flush=True)
    t0 = time.time()

    def objective(trial):
        params = dict(
            n_estimators=trial.suggest_int("n_estimators", 100, 800),
            max_depth=trial.suggest_int("max_depth", 2, 10),
            min_samples_leaf=trial.suggest_int("min_samples_leaf", 1, 20),
            max_features=trial.suggest_float("max_features", 0.2, 1.0),
            min_samples_split=trial.suggest_int("min_samples_split", 2, 10),
            max_leaf_nodes=trial.suggest_int("max_leaf_nodes", 20, 200),
            class_weight="balanced", random_state=42, n_jobs=-1,
        )
        m = ExtraTreesClassifier(**params)
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        return cross_val_score(m, X_imp, y, cv=cv, scoring="roc_auc").mean()

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=200, show_progress_bar=False, n_jobs=4)
    print(f"  Inner CV AUC: {study.best_value:.4f} ({time.time() - t0:.0f}s)")
    print(f"  Best params: {study.best_params}")

    # LOO AUC for reference
    best_p = study.best_params.copy()
    best_p.update({"class_weight": "balanced", "random_state": 42, "n_jobs": -1})
    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    for train_idx, test_idx in loo.split(X_imp):
        m = ExtraTreesClassifier(**best_p)
        m.fit(X_imp[train_idx], y[train_idx])
        y_prob[test_idx] = m.predict_proba(X_imp[test_idx])[:, 1]
    loo_auc = roc_auc_score(y, y_prob)
    print(f"  LOO AUC: {loo_auc:.4f}")

    # Train final model on ALL data
    model = ExtraTreesClassifier(**best_p)
    model.fit(X_imp, y)

    bundle = {
        "model": model,
        "imputer": imp,
        "features": ALL_FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "version": "et_shadow_v1",
        "n_positions": len(y),
        "loo_auc": round(loo_auc, 4),
        "inner_cv_auc": round(study.best_value, 4),
        "optuna_params": best_p,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    joblib.dump(bundle, OUTPUT_PATH, compress=3)
    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"\n  Saved: {OUTPUT_PATH} ({size_kb:.0f} KB)")
    print(f"  Version: {bundle['version']}")
    print(f"  LOO AUC: {loo_auc:.4f}, Inner CV AUC: {study.best_value:.4f}")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    run()
