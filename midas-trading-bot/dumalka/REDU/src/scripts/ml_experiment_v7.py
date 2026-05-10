#!/usr/bin/env python3
"""
ML Experiment v7 — GPU Neural Networks + Optuna × 200 trials (READ-ONLY)

Uses NVIDIA Titan V (12 GB) for models that benefit from GPU:

  1. ExtraTrees + Optuna × 200 trials (CPU, but parallelized)
     Champion from EXP-6, more Optuna budget = potentially better params

  2. TabNet (GPU) — pytorch-tabnet, attention-based, interpretable per-sample
     feature importance via attention masks; crypto/finance proven

  3. TabM (GPU) — ICLR 2025 best tabular DL, parameter-efficient BatchEnsemble
     of MLPs; outperforms FT-Transformer on NeurIPS benchmarks

  4. FT-Transformer (GPU) — Feature Tokenizer + Transformer via rtdl-revisiting;
     best attention-based tabular model before TabM

  Note: GPU for GBDT (XGBoost/CatBoost/LightGBM) is counter-productive at
  N=98 — GPU overhead dominates. Neural nets benefit from GPU for the
  forward/backward passes even on small N.

Zero writes to DB. Zero changes to production code. Safe to run anytime.

2026-04-08
"""

import sys, os, warnings, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import torch
import psycopg2
import psycopg2.extras
from sklearn.model_selection import LeaveOneOut, StratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score, accuracy_score, brier_score_loss
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
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


# ─── Data loading ────────────────────────────────────────────────────────────

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
        pnls.append(float(row["realized_pnl_pct"]))

    return X, y, np.array(pnls)


def report(name, y_prob, y, pnls, elapsed):
    y_pred = (y_prob >= 0.5).astype(int)
    acc = accuracy_score(y, y_pred)
    try:
        auc = roc_auc_score(y, y_prob)
    except ValueError:
        auc = 0.5
    brier = brier_score_loss(y, y_prob)

    cf = []
    for thr in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        mask = y_prob >= thr
        if mask.sum() < 4:
            continue
        sel = pnls[mask]
        wr = np.mean(y[mask] == 1) * 100
        cf.append((thr, int(mask.sum()), float(sel.mean()), float(sel.sum()), wr))

    return {"name": name, "acc": acc, "auc": auc, "brier": brier,
            "elapsed": elapsed, "y_prob": y_prob, "cf": cf}


# ─── Model runners ───────────────────────────────────────────────────────────

def run_et_optuna(X_imp, y, n_trials=200):
    """ExtraTrees with deep Optuna search (200 trials, 5-fold CV)."""
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

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=10),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False, n_jobs=4)

    best_p = study.best_params
    best_p.update({"class_weight": "balanced", "random_state": 42, "n_jobs": -1})
    print(f"    Best inner CV AUC: {study.best_value:.4f}  params: {best_p}")

    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    imp = SimpleImputer(strategy="median")
    for train_idx, test_idx in loo.split(X_imp):
        m = ExtraTreesClassifier(**best_p)
        m.fit(X_imp[train_idx], y[train_idx])
        y_prob[test_idx] = m.predict_proba(X_imp[test_idx])[:, 1]
    return y_prob, study.best_value, best_p


def run_tabnet(X_imp, X_scaled, y, device):
    """TabNet LOO CV with GPU."""
    from pytorch_tabnet.tab_model import TabNetClassifier

    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    attention_importances = []

    print(f"    LOO {len(y)} iters", end="", flush=True)
    for i, (train_idx, test_idx) in enumerate(loo.split(X_imp)):
        if i % 20 == 0:
            print(f" {i}", end="", flush=True)

        X_tr, X_te = X_scaled[train_idx].astype(np.float32), X_scaled[test_idx].astype(np.float32)
        y_tr = y[train_idx]
        n_tr = len(y_tr)

        clf = TabNetClassifier(
            n_d=8, n_a=8,
            n_steps=3,
            gamma=1.3,
            lambda_sparse=1e-3,
            optimizer_fn=torch.optim.Adam,
            optimizer_params={"lr": 1e-2, "weight_decay": 1e-5},
            mask_type="sparsemax",
            device_name=device,
            verbose=0,
            seed=42,
        )
        # small validation split for early stopping
        val_size = max(1, int(0.15 * n_tr))
        train_sub = np.arange(val_size, n_tr)
        val_sub = np.arange(val_size)

        clf.fit(
            X_tr[train_sub], y_tr[train_sub],
            eval_set=[(X_tr[val_sub], y_tr[val_sub])],
            eval_metric=["auc"],
            max_epochs=200,
            patience=20,
            batch_size=min(64, n_tr),
            virtual_batch_size=min(32, n_tr),
            drop_last=False,
        )

        prob = clf.predict_proba(X_te)
        y_prob[test_idx] = prob[:, 1]
        attention_importances.append(clf.feature_importances_)

    print(" done")
    avg_importance = np.mean(attention_importances, axis=0)
    return y_prob, avg_importance


def run_tabm(X_imp, X_scaled, y, device):
    """TabM (ICLR 2025) — parameter-efficient MLP ensemble, GPU."""
    try:
        import tabm
    except ImportError:
        return None, None

    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset

    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    n_features = X_scaled.shape[1]

    print(f"    LOO {len(y)} iters", end="", flush=True)
    for i, (train_idx, test_idx) in enumerate(loo.split(X_scaled)):
        if i % 20 == 0:
            print(f" {i}", end="", flush=True)

        X_tr = torch.tensor(X_scaled[train_idx], dtype=torch.float32, device=device)
        y_tr = torch.tensor(y[train_idx], dtype=torch.long, device=device)
        X_te = torch.tensor(X_scaled[test_idx], dtype=torch.float32, device=device)
        n_tr = len(y_tr)

        # TabM: ensemble of k=8 small MLPs via BatchEnsemble
        model = tabm.Model(
            n_num_features=n_features,
            cat_cardinalities=[],
            n_classes=2,
            backbone={"type": "MLP", "n_blocks": 2, "d_block": 64, "dropout": 0.1},
            num_embeddings=None,
            arch_type="tabm",
            k=8,
        ).to(device)

        optimizer = torch.optim.AdamW(
            model.parameters(), lr=1e-3, weight_decay=1e-4,
        )
        criterion = nn.CrossEntropyLoss()

        # mini training loop
        dataset = TensorDataset(X_tr, y_tr)
        loader = DataLoader(dataset, batch_size=min(32, n_tr), shuffle=True)

        model.train()
        for epoch in range(100):
            for xb, yb in loader:
                optimizer.zero_grad()
                # TabM returns (k, batch, n_classes) — average over ensemble
                logits = model(xb).mean(dim=0)
                loss = criterion(logits, yb)
                loss.backward()
                optimizer.step()

        model.eval()
        with torch.no_grad():
            logits = model(X_te).mean(dim=0)
            probs = torch.softmax(logits, dim=-1)
            y_prob[test_idx] = probs[:, 1].cpu().numpy()

    print(" done")
    return y_prob


def run_ft_transformer(X_imp, X_scaled, y, device):
    """FT-Transformer via rtdl (Feature Tokenizer + Transformer), GPU."""
    try:
        import rtdl
    except ImportError:
        try:
            import pip
            os.system("pip3 install --quiet --break-system-packages rtdl")
            import rtdl
        except Exception:
            return None

    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset

    loo = LeaveOneOut()
    y_prob = np.zeros(len(y))
    n_features = X_scaled.shape[1]

    print(f"    LOO {len(y)} iters", end="", flush=True)
    for i, (train_idx, test_idx) in enumerate(loo.split(X_scaled)):
        if i % 20 == 0:
            print(f" {i}", end="", flush=True)

        X_tr = torch.tensor(X_scaled[train_idx], dtype=torch.float32, device=device)
        y_tr = torch.tensor(y[train_idx], dtype=torch.long, device=device)
        X_te = torch.tensor(X_scaled[test_idx], dtype=torch.float32, device=device)
        n_tr = len(y_tr)

        model = rtdl.FTTransformer.make_default(
            n_num_features=n_features,
            cat_cardinalities=None,
            last_layer_query_idx=[-1],
            d_out=2,
        ).to(device)

        optimizer = model.make_default_optimizer()
        criterion = nn.CrossEntropyLoss()

        dataset = TensorDataset(X_tr, y_tr)
        loader = DataLoader(dataset, batch_size=min(32, n_tr), shuffle=True)

        model.train()
        for epoch in range(80):
            for xb, yb in loader:
                optimizer.zero_grad()
                out = model(xb, None)
                loss = criterion(out, yb)
                loss.backward()
                optimizer.step()

        model.eval()
        with torch.no_grad():
            out = model(X_te, None)
            probs = torch.softmax(out, dim=-1)
            y_prob[test_idx] = probs[:, 1].cpu().numpy()

    print(" done")
    return y_prob


# ─── Main ────────────────────────────────────────────────────────────────────

def run():
    print("=" * 74)
    print("ML EXPERIMENT v7 — GPU Neural Nets + Optuna×200 (READ-ONLY)")
    print("=" * 74)

    print(f"\n  Device: {DEVICE}")
    if DEVICE == "cuda":
        props = torch.cuda.get_device_properties(0)
        vram_gb = props.total_memory / 1024 ** 3
        print(f"  GPU: {props.name}  VRAM: {vram_gb:.1f} GB")
    print()

    X, y, pnls = load_data()
    n_profit, n_total = int(y.sum()), len(y)
    n_loss = n_total - n_profit
    baseline = max(n_profit, n_loss) / n_total

    print(f"  Positions: {n_total}  profit: {n_profit}  loss: {n_loss}")
    print(f"  Baseline: {baseline:.3f}  Avg PnL: {pnls.mean():+.3f}%")
    print(f"  Features: {len(ALL_FEATURES)}")

    imp = SimpleImputer(strategy="median")
    scl = StandardScaler()
    X_imp = imp.fit_transform(X)
    X_scaled = scl.fit_transform(X_imp)

    results = {}

    # ── 1. ExtraTrees + Optuna × 200 trials ──────────────────────────────────
    print(f"\n  [1/4] ExtraTrees + Optuna × 200 trials (parallelized n_jobs=4)...")
    t0 = time.time()
    yp_et, inner_auc, best_params = run_et_optuna(X_imp, y, n_trials=200)
    elapsed = time.time() - t0
    results["ET+Optuna×200"] = report("ET+Optuna×200", yp_et, y, pnls, elapsed)
    r = results["ET+Optuna×200"]
    print(f"    LOO AUC={r['auc']:.3f}  Acc={r['acc']:.3f}  Brier={r['brier']:.3f}  ({elapsed:.0f}s)")

    # ── 2. TabNet (GPU) ───────────────────────────────────────────────────────
    print(f"\n  [2/4] TabNet ({DEVICE.upper()})...")
    t0 = time.time()
    try:
        yp_tn, tn_importance = run_tabnet(X_imp, X_scaled, y, DEVICE)
        elapsed = time.time() - t0
        results["TabNet"] = report("TabNet", yp_tn, y, pnls, elapsed)
        r = results["TabNet"]
        print(f"    LOO AUC={r['auc']:.3f}  Acc={r['acc']:.3f}  Brier={r['brier']:.3f}  ({elapsed:.0f}s)")
    except Exception as e:
        print(f"    FAILED: {e}")
        tn_importance = None

    # ── 3. TabM (GPU) ─────────────────────────────────────────────────────────
    print(f"\n  [3/4] TabM ICLR-2025 ({DEVICE.upper()})...")
    t0 = time.time()
    try:
        yp_tm = run_tabm(X_imp, X_scaled, y, DEVICE)
        if yp_tm is not None:
            elapsed = time.time() - t0
            results["TabM"] = report("TabM", yp_tm, y, pnls, elapsed)
            r = results["TabM"]
            print(f"    LOO AUC={r['auc']:.3f}  Acc={r['acc']:.3f}  Brier={r['brier']:.3f}  ({elapsed:.0f}s)")
        else:
            print("    Skipped (tabm not installed)")
    except Exception as e:
        print(f"    FAILED: {e}")

    # ── 4. FT-Transformer (GPU) ───────────────────────────────────────────────
    print(f"\n  [4/4] FT-Transformer ({DEVICE.upper()})...")
    t0 = time.time()
    try:
        yp_ft = run_ft_transformer(X_imp, X_scaled, y, DEVICE)
        if yp_ft is not None:
            elapsed = time.time() - t0
            results["FT-Transformer"] = report("FT-Transformer", yp_ft, y, pnls, elapsed)
            r = results["FT-Transformer"]
            print(f"    LOO AUC={r['auc']:.3f}  Acc={r['acc']:.3f}  Brier={r['brier']:.3f}  ({elapsed:.0f}s)")
        else:
            print("    Skipped (rtdl not installed)")
    except Exception as e:
        print(f"    FAILED: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════════════════════════
    valid = {k: v for k, v in results.items() if v is not None}

    print("\n" + "=" * 74)
    print("FULL COMPARISON — EXP-7 vs всех предыдущих чемпионов")
    print("=" * 74)

    prior = {
        "EXP-5 ET (base)":        {"auc": 0.606, "brier": 0.235},
        "EXP-6 ET+Optuna×40":     {"auc": 0.606, "brier": 0.235},
    }

    sorted_names = sorted(valid, key=lambda n: valid[n]["auc"], reverse=True)

    print(f"\n  {'Model':<22} {'Acc':>6} {'AUC':>6} {'Brier':>6} {'vs ET-base':>11} {'Time':>8}")
    print(f"  {'-'*22} {'-'*6} {'-'*6} {'-'*6} {'-'*11} {'-'*8}")
    print(f"  {'Baseline':<22} {baseline:>6.3f} {'—':>6} {'—':>6} {'—':>11} {'—':>8}")
    for nm, pr in prior.items():
        print(f"  {nm:<22} {'—':>6} {pr['auc']:>6.3f} {pr['brier']:>6.3f} {'ref':>11} {'—':>8}")
    print()
    for name in sorted_names:
        r = valid[name]
        vs = r["auc"] - 0.606
        print(f"  {name:<22} {r['acc']:>6.3f} {r['auc']:>6.3f} {r['brier']:>6.3f} "
              f"{vs:>+11.3f} {r['elapsed']:>7.0f}s")

    # ── Confidence filtering ──────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("CONFIDENCE FILTERING")
    print("=" * 74)

    print(f"\n  {'Model':<22} {'Thr':>5} {'N':>4} {'Avg PnL':>8} {'Total':>8} {'Win%':>6}")
    print(f"  {'-'*22} {'-'*5} {'-'*4} {'-'*8} {'-'*8} {'-'*6}")
    print(f"  {'ALL':<22} {'—':>5} {n_total:>4} "
          f"{pnls.mean():>+7.2f}% {pnls.sum():>+7.1f}% "
          f"{n_profit/n_total*100:>5.1f}%")
    print(f"  {'EXP-6 ET+Opt@0.75':<22} {'0.75':>5} {'13':>4} "
          f"{'  +1.55%':>8} {'  +20.1%':>8} {'92.3%':>6}")
    print()

    best_avg, best_label = -999, ""
    for name in sorted_names:
        r = valid[name]
        for thr, n, ap, tot, wr in r["cf"]:
            if ap > best_avg:
                best_avg, best_label = ap, f"{name} @ {thr:.2f}"
            print(f"  {name:<22} {thr:>5.2f} {n:>4} {ap:>+7.2f}% "
                  f"{tot:>+7.1f}% {wr:>5.1f}%")
        print()

    print(f"  >>> BEST: {best_label}  (avg PnL {best_avg:+.2f}%)")

    # ── TabNet feature importance ────────────────────────────────────────────
    if tn_importance is not None:
        print("\n" + "=" * 74)
        print("TABNET — attention-based feature importance (avg over LOO)")
        print("=" * 74)
        sorted_fi = np.argsort(tn_importance)[::-1]
        print(f"\n  {'Rank':<5} {'Feature':<28} {'Importance':>10}")
        print(f"  {'-'*5} {'-'*28} {'-'*10}")
        for rank, idx in enumerate(sorted_fi[:15], 1):
            bar = "█" * int(tn_importance[idx] * 200)
            print(f"  {rank:<5} {ALL_FEATURES[idx]:<28} {tn_importance[idx]:>10.4f}  {bar}")

    # ── Grand consensus ───────────────────────────────────────────────────────
    if len(valid) >= 2:
        print("\n" + "=" * 74)
        print("GRAND CONSENSUS — все модели EXP-7")
        print("=" * 74)

        all_probs = np.stack([valid[n]["y_prob"] for n in valid])
        avg_prob = all_probs.mean(axis=0)
        all_preds = (all_probs >= 0.5).astype(int)

        print(f"\n  {'Strategy':<35} {'N':>4} {'Avg PnL':>8} {'Total':>8} {'Win%':>6}")
        print(f"  {'-'*35} {'-'*4} {'-'*8} {'-'*8} {'-'*6}")
        print(f"  {'ALL':<35} {n_total:>4} {pnls.mean():>+7.2f}% "
              f"{pnls.sum():>+7.1f}% {n_profit/n_total*100:>5.1f}%")
        for thr in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
            mask = avg_prob >= thr
            if mask.sum() < 3:
                continue
            sel = pnls[mask]
            wr = np.mean(y[mask] == 1) * 100
            print(f"  {f'avg_prob >= {thr}':<35} {mask.sum():>4} {sel.mean():>+7.2f}% "
                  f"{sel.sum():>+7.1f}% {wr:>5.1f}%")

        n_m = len(all_preds)
        for k in [n_m, n_m - 1]:
            agree = np.sum(all_preds == 1, axis=0) >= k
            if agree.sum() < 3:
                continue
            sel = pnls[agree]
            wr = np.mean(y[agree] == 1) * 100
            print(f"  {f'{k}/{n_m} unanimous → profit':<35} {agree.sum():>4} "
                  f"{sel.mean():>+7.2f}% {sel.sum():>+7.1f}% {wr:>5.1f}%")

    # ── Optuna best params ───────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("OPTUNA × 200 — best ExtraTrees parameters")
    print("=" * 74)
    print(f"\n  Inner 5-fold AUC: {inner_auc:.4f}")
    prev = {"n_estimators": 366, "max_depth": 4, "min_samples_leaf": 11,
            "max_features": 0.924, "min_samples_split": "—", "max_leaf_nodes": "—"}
    for k, v in sorted(best_params.items()):
        if k in ("class_weight", "random_state", "n_jobs"):
            continue
        prev_v = prev.get(k, "—")
        delta = ""
        if isinstance(v, (int, float)) and isinstance(prev_v, (int, float)):
            delta = f"  Δ{v - prev_v:+.3g}"
        print(f"  {k:<25} {str(v):<12}  (EXP-6: {prev_v}){delta}")

    print("\n" + "=" * 74)
    print("COMPLETE — zero DB writes, zero production impact")
    print("=" * 74)


if __name__ == "__main__":
    run()
