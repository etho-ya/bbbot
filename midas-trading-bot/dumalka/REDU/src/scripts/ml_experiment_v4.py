#!/usr/bin/env python3
"""
ML Experiment v4 — Full Model Zoo (READ-ONLY)

Tests 9 models on the same position-level data (was_profitable):
  Tier 1: LightGBM, XGBoost, CatBoost, LogReg (L2)
  Tier 2: Random Forest, SVM (RBF), SVM (Linear), Ridge Classifier
  Tier 3: Stacking Ensemble (meta-model over Tier 1+2)

Plus: per-symbol breakdown, confidence calibration, and PnL simulation.

Zero writes to DB. Zero changes to production code. Safe to run anytime.

2026-04-08
"""

import sys, os, warnings, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import psycopg2
import psycopg2.extras
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.metrics import roc_auc_score, accuracy_score, brier_score_loss
from sklearn.linear_model import LogisticRegression, RidgeClassifier
from sklearn.ensemble import (
    RandomForestClassifier, StackingClassifier, VotingClassifier,
)
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.calibration import CalibratedClassifierCV
import lightgbm as lgb
import xgboost as xgb
from catboost import CatBoostClassifier

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
        SELECT a.*,
               p.symbol, p.side, p.realized_pnl_pct,
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
    X = np.zeros((n, len(ALL_FEATURES)), dtype=np.float64)
    y = np.zeros(n, dtype=np.int32)
    symbols = []
    pnls = []

    for i, row in enumerate(rows):
        for j, col in enumerate(NUMERIC_FEATURES):
            val = row[col]
            X[i, j] = float(val) if val is not None else np.nan
        base = len(NUMERIC_FEATURES)
        X[i, base] = 1.0 if row["side"] == "long" else 0.0
        X[i, base + 1] = 1.0 if row.get("re_recommendation") == "approve" else 0.0
        X[i, base + 2] = 1.0 if row.get("corrected_recommendation") == "approve" else 0.0
        y[i] = 1 if row["realized_pnl_pct"] > 0 else 0
        symbols.append(row["symbol"].replace("USDT", ""))
        pnls.append(float(row["realized_pnl_pct"]))

    return X, y, np.array(pnls), symbols


def loo_evaluate(model, X, y, needs_impute=False):
    """LOO CV returning probabilities. Some models need imputed X."""
    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    for train_idx, test_idx in loo.split(X):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr = y[train_idx]
        if needs_impute:
            imp = SimpleImputer(strategy="median")
            X_tr = imp.fit_transform(X_tr)
            X_te = imp.transform(X_te)
        model.fit(X_tr, y_tr)
        if hasattr(model, "predict_proba"):
            y_prob[test_idx] = model.predict_proba(X_te)[:, 1]
        elif hasattr(model, "decision_function"):
            d = model.decision_function(X_te)
            y_prob[test_idx] = 1 / (1 + np.exp(-d))
        else:
            y_prob[test_idx] = model.predict(X_te)
    return y_prob


def run():
    print("=" * 76)
    print("ML EXPERIMENT v4 — Full Model Zoo (READ-ONLY)")
    print("=" * 76)

    X, y, pnls, symbols = load_data()
    n_profit = int(y.sum())
    n_loss = len(y) - n_profit
    baseline = max(n_profit, n_loss) / len(y)
    avg_pnl = float(pnls.mean())

    print(f"\n  Positions: {len(y)} (profit: {n_profit}, loss: {n_loss})")
    print(f"  Baseline: {baseline:.3f} (always {'profit' if n_profit > n_loss else 'loss'})")
    print(f"  Avg PnL: {avg_pnl:+.3f}%, Total PnL: {pnls.sum():+.1f}%")
    print(f"  Features: {len(ALL_FEATURES)}")

    # ── Prepare imputed X for models that can't handle NaN ──
    imp = SimpleImputer(strategy="median")
    X_imp = imp.fit_transform(X)
    scl = StandardScaler()
    X_scaled = scl.fit_transform(X_imp)

    spw = n_loss / max(n_profit, 1)

    models = {
        # --- Tier 1: Gradient Boosting ---
        "LightGBM": (
            lgb.LGBMClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.05,
                num_leaves=8, min_child_samples=5,
                subsample=0.8, colsample_bytree=0.7,
                class_weight="balanced", random_state=42, verbose=-1,
            ), False,
        ),
        "XGBoost": (
            xgb.XGBClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.7,
                scale_pos_weight=spw,
                eval_metric="logloss", random_state=42, verbosity=0,
            ), False,
        ),
        "CatBoost": (
            CatBoostClassifier(
                iterations=200, depth=4, learning_rate=0.05,
                l2_leaf_reg=5, auto_class_weights="Balanced",
                random_seed=42, verbose=0,
            ), False,
        ),
        # --- Tier 1: Linear ---
        "LogReg L2": (
            Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(
                    C=0.1, class_weight="balanced", max_iter=1000, random_state=42,
                )),
            ]), False,
        ),
        # --- Tier 2: Tree-based ---
        "RandomForest": (
            RandomForestClassifier(
                n_estimators=200, max_depth=4, min_samples_leaf=5,
                class_weight="balanced", random_state=42, n_jobs=-1,
            ), True,
        ),
        # --- Tier 2: Kernel methods ---
        "SVM (RBF)": (
            Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", SVC(
                    kernel="rbf", C=1.0, gamma="scale",
                    class_weight="balanced", probability=True, random_state=42,
                )),
            ]), False,
        ),
        "SVM (Linear)": (
            Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", SVC(
                    kernel="linear", C=0.1,
                    class_weight="balanced", probability=True, random_state=42,
                )),
            ]), False,
        ),
        # --- Tier 2: Ridge ---
        "Ridge": (
            Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", CalibratedClassifierCV(
                    RidgeClassifier(alpha=1.0, class_weight="balanced"),
                    cv=5, method="sigmoid",
                )),
            ]), False,
        ),
    }

    # ── Run all models ──
    results = {}
    for name, (model, needs_impute) in models.items():
        t0 = time.time()
        print(f"\n  [{time.strftime('%H:%M:%S')}] Running {name}...", end=" ", flush=True)
        y_prob = loo_evaluate(model, X, y, needs_impute=needs_impute)
        elapsed = time.time() - t0
        y_pred = (y_prob >= 0.5).astype(int)

        acc = accuracy_score(y, y_pred)
        try:
            auc = roc_auc_score(y, y_prob)
        except ValueError:
            auc = 0.5
        brier = brier_score_loss(y, y_prob)

        results[name] = {
            "acc": acc, "auc": auc, "brier": brier,
            "elapsed": elapsed, "y_prob": y_prob, "y_pred": y_pred,
        }
        print(f"Acc={acc:.3f} AUC={auc:.3f} Brier={brier:.3f} ({elapsed:.1f}s)")

    # ── Stacking Ensemble ──
    print(f"\n  [{time.strftime('%H:%M:%S')}] Running Stacking Ensemble...", end=" ", flush=True)
    t0 = time.time()
    stack_estimators = [
        ("lgbm", lgb.LGBMClassifier(
            n_estimators=80, max_depth=3, learning_rate=0.05,
            num_leaves=8, min_child_samples=5, class_weight="balanced",
            random_state=42, verbose=-1)),
        ("xgb", xgb.XGBClassifier(
            n_estimators=80, max_depth=3, learning_rate=0.05,
            scale_pos_weight=spw, eval_metric="logloss",
            random_state=42, verbosity=0)),
        ("rf", RandomForestClassifier(
            n_estimators=100, max_depth=4, min_samples_leaf=5,
            class_weight="balanced", random_state=42, n_jobs=-1)),
        ("lr", Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("scl", StandardScaler()),
            ("clf", LogisticRegression(C=0.1, class_weight="balanced",
                                       max_iter=1000, random_state=42)),
        ])),
    ]
    stacking = StackingClassifier(
        estimators=stack_estimators,
        final_estimator=LogisticRegression(C=1.0, max_iter=500),
        cv=5, passthrough=False, n_jobs=-1,
    )
    y_prob_stack = loo_evaluate(stacking, X, y, needs_impute=True)
    elapsed_stack = time.time() - t0
    y_pred_stack = (y_prob_stack >= 0.5).astype(int)
    acc_stack = accuracy_score(y, y_pred_stack)
    auc_stack = roc_auc_score(y, y_prob_stack)
    brier_stack = brier_score_loss(y, y_prob_stack)
    results["Stacking"] = {
        "acc": acc_stack, "auc": auc_stack, "brier": brier_stack,
        "elapsed": elapsed_stack, "y_prob": y_prob_stack, "y_pred": y_pred_stack,
    }
    print(f"Acc={acc_stack:.3f} AUC={auc_stack:.3f} Brier={brier_stack:.3f} ({elapsed_stack:.1f}s)")

    # ── Soft Voting ──
    print(f"\n  [{time.strftime('%H:%M:%S')}] Computing Soft Voting...", end=" ", flush=True)
    all_probs = np.stack([results[n]["y_prob"] for n in results])
    y_prob_vote = all_probs.mean(axis=0)
    y_pred_vote = (y_prob_vote >= 0.5).astype(int)
    acc_vote = accuracy_score(y, y_pred_vote)
    auc_vote = roc_auc_score(y, y_prob_vote)
    brier_vote = brier_score_loss(y, y_prob_vote)
    results["SoftVoting(all)"] = {
        "acc": acc_vote, "auc": auc_vote, "brier": brier_vote,
        "elapsed": 0, "y_prob": y_prob_vote, "y_pred": y_pred_vote,
    }
    print(f"Acc={acc_vote:.3f} AUC={auc_vote:.3f} Brier={brier_vote:.3f}")

    # ══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 76)
    print("FULL COMPARISON TABLE — sorted by AUC")
    print("=" * 76)

    sorted_names = sorted(results, key=lambda n: results[n]["auc"], reverse=True)

    print(f"\n  {'#':<3} {'Model':<18} {'Acc':>6} {'AUC':>6} {'Brier':>6} "
          f"{'Lift':>7} {'Time':>6}")
    print(f"  {'-'*3} {'-'*18} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")
    print(f"  {'—':<3} {'Baseline':<18} {baseline:>6.3f} {'—':>6} {'—':>6} "
          f"{'—':>7} {'—':>6}")

    for rank, name in enumerate(sorted_names, 1):
        r = results[name]
        lift = r["acc"] - baseline
        print(f"  {rank:<3} {name:<18} {r['acc']:>6.3f} {r['auc']:>6.3f} "
              f"{r['brier']:>6.3f} {lift:>+7.3f} {r['elapsed']:>5.1f}s")

    # ── Confidence filtering ──
    print("\n" + "=" * 76)
    print("CONFIDENCE FILTERING — если бы модель отбирала вход")
    print("=" * 76)

    thresholds = [0.6, 0.65, 0.7, 0.75, 0.8]
    best_strategy = None
    best_avg = -999

    print(f"\n  {'Model':<18} {'Thr':>4} {'N':>4} {'Avg PnL':>8} {'Total':>8} "
          f"{'Win%':>6} {'Avoided':>8}")
    print(f"  {'-'*18} {'-'*4} {'-'*4} {'-'*8} {'-'*8} {'-'*6} {'-'*8}")

    for name in sorted_names[:6]:
        r = results[name]
        for thr in thresholds:
            mask = r["y_prob"] >= thr
            if mask.sum() < 5:
                continue
            sel_pnl = pnls[mask]
            sel_wr = np.mean(y[mask] == 1) * 100
            avoided = pnls[~mask]
            avoided_avg = avoided.mean() if len(avoided) > 0 else 0
            if sel_pnl.mean() > best_avg:
                best_avg = sel_pnl.mean()
                best_strategy = (name, thr, mask.sum())
            print(f"  {name:<18} {thr:>4.2f} {mask.sum():>4} "
                  f"{sel_pnl.mean():>+7.2f}% {sel_pnl.sum():>+7.1f}% "
                  f"{sel_wr:>5.1f}% {avoided_avg:>+7.2f}%")
        print()

    if best_strategy:
        print(f"  >>> BEST: {best_strategy[0]} @ {best_strategy[1]:.2f} "
              f"({best_strategy[2]} trades, avg PnL {best_avg:+.2f}%)")

    # ── Model agreement ──
    print("\n" + "=" * 76)
    print("CONSENSUS — когда модели согласны")
    print("=" * 76)

    core_models = ["LightGBM", "XGBoost", "CatBoost", "LogReg L2",
                   "RandomForest", "SVM (RBF)"]
    core_preds = np.stack([results[n]["y_pred"] for n in core_models if n in results])
    core_probs = np.stack([results[n]["y_prob"] for n in core_models if n in results])
    n_models = len(core_preds)

    for min_agree in [n_models, n_models - 1, n_models - 2]:
        agree_profit = np.sum(core_preds == 1, axis=0) >= min_agree
        agree_loss = np.sum(core_preds == 0, axis=0) >= min_agree

        if agree_profit.sum() > 0:
            ap = pnls[agree_profit]
            aw = np.mean(y[agree_profit] == 1) * 100
            print(f"\n  {min_agree}/{n_models} → profit: {agree_profit.sum()} trades, "
                  f"avg PnL={ap.mean():+.2f}%, total={ap.sum():+.1f}%, win={aw:.0f}%")
        if agree_loss.sum() > 0:
            al = pnls[agree_loss]
            alw = np.mean(y[agree_loss] == 0) * 100
            print(f"  {min_agree}/{n_models} → loss:   {agree_loss.sum()} trades, "
                  f"avg PnL={al.mean():+.2f}%, total={al.sum():+.1f}%, correct={alw:.0f}%")

    # ── High-confidence consensus ──
    avg_prob = core_probs.mean(axis=0)
    strong_profit = avg_prob >= 0.65
    strong_loss = avg_prob <= 0.35

    print(f"\n  Avg probability >= 0.65: {strong_profit.sum()} trades", end="")
    if strong_profit.sum() > 0:
        sp = pnls[strong_profit]
        print(f", avg PnL={sp.mean():+.2f}%, win={np.mean(y[strong_profit]==1)*100:.0f}%")
    else:
        print()

    print(f"  Avg probability <= 0.35: {strong_loss.sum()} trades", end="")
    if strong_loss.sum() > 0:
        sl = pnls[strong_loss]
        print(f", avg PnL={sl.mean():+.2f}%, correct={np.mean(y[strong_loss]==0)*100:.0f}%")
    else:
        print()

    # ── Per-symbol breakdown ──
    print("\n" + "=" * 76)
    print("PER-SYMBOL — средняя вероятность profit по моделям")
    print("=" * 76)

    sym_arr = np.array(symbols)
    unique_syms = sorted(set(symbols))

    print(f"\n  {'Symbol':<12} {'N':>3} {'Actual Win%':>10} {'Model Prob':>10} "
          f"{'Avg PnL':>8} {'Calibration':>12}")
    print(f"  {'-'*12} {'-'*3} {'-'*10} {'-'*10} {'-'*8} {'-'*12}")

    for sym in unique_syms:
        mask = sym_arr == sym
        if mask.sum() < 2:
            continue
        actual_wr = np.mean(y[mask] == 1)
        model_prob = avg_prob[mask].mean()
        sym_pnl = pnls[mask].mean()
        calib = "GOOD" if abs(actual_wr - model_prob) < 0.15 else (
            "OVER-CONF" if model_prob > actual_wr + 0.15 else "UNDER-CONF")
        print(f"  {sym:<12} {mask.sum():>3} {actual_wr*100:>9.0f}% "
              f"{model_prob*100:>9.0f}% {sym_pnl:>+7.2f}% {calib:>12}")

    # ── Feature importance across models ──
    print("\n" + "=" * 76)
    print("CROSS-MODEL FEATURE IMPORTANCE — среднее по 4 tree-based моделям")
    print("=" * 76)

    fi_models = {}
    for name, (model, needs_impute) in models.items():
        if name in ("LightGBM", "XGBoost", "CatBoost", "RandomForest"):
            m = model
            X_fit = X_imp if needs_impute else X
            m.fit(X_fit, y)
            fi = m.feature_importances_
            fi = fi / fi.sum()
            fi_models[name] = fi

    if fi_models:
        avg_fi = np.mean(list(fi_models.values()), axis=0)
        sorted_fi = np.argsort(avg_fi)[::-1]

        print(f"\n  {'Rank':<5} {'Feature':<25} {'Avg%':>6} "
              f"{'LGBM':>6} {'XGB':>6} {'CB':>6} {'RF':>6}")
        print(f"  {'-'*5} {'-'*25} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

        for rank, idx in enumerate(sorted_fi[:15]):
            vals = {n: fi_models[n][idx] * 100 for n in fi_models}
            print(f"  {rank+1:<5} {ALL_FEATURES[idx]:<25} {avg_fi[idx]*100:>5.1f}% "
                  f"{vals.get('LightGBM',0):>5.1f}% {vals.get('XGBoost',0):>5.1f}% "
                  f"{vals.get('CatBoost',0):>5.1f}% {vals.get('RandomForest',0):>5.1f}%")

    # ── LONG vs SHORT breakdown ──
    print("\n" + "=" * 76)
    print("LONG vs SHORT — подтверждение гипотезы side_is_long → LOSS")
    print("=" * 76)

    side_idx = ALL_FEATURES.index("side_is_long")
    is_long = X[:, side_idx] == 1.0
    is_short = ~is_long

    for label, mask in [("LONG", is_long), ("SHORT", is_short)]:
        n_m = mask.sum()
        if n_m == 0:
            continue
        wr = np.mean(y[mask] == 1) * 100
        ap = pnls[mask].mean()
        tp = pnls[mask].sum()
        mp = avg_prob[mask].mean()
        print(f"\n  {label}: {n_m} trades, win_rate={wr:.1f}%, "
              f"avg PnL={ap:+.2f}%, total={tp:+.1f}%, model_prob={mp:.2f}")

    print("\n" + "=" * 76)
    print("COMPLETE — zero DB writes, zero production impact")
    print("=" * 76)


if __name__ == "__main__":
    run()
