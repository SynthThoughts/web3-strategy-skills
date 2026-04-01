"""MO-1: Depth exploration experiment.

For each tree depth in [2..8], train CatBoost with v10's 5 features
using PurgedTimeSeriesSplit CV, measuring AUC, overfitting gap, and
early-stop iterations. Also test grow_policy variants at depth=3.
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.metrics import roc_auc_score

# Add project root to path
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config import DATA_DIR, PARQUET_FILE
from data.features import build_features, get_feature_columns
from data.labels import generate_labels
from training.train_pipeline import PurgedTimeSeriesSplit
import db

V10_FEATURES = [
    "taker_vol_raw",
    "price_vs_rvwap_60",
    "cvd_slope_10",
    "hour4_sin",
    "vpt_sum_30",
]


def load_and_prepare():
    """Load data, build features, merge labels, return non-neutral dataset."""
    sample_start = "2026-03-01"
    sample_end = "2026-03-31"
    ts_start = pd.Timestamp(sample_start, tz="UTC")
    ts_end = pd.Timestamp(sample_end, tz="UTC")

    def _filter_df(df, time_col="open_time"):
        if df is None:
            return None
        out = df[(df[time_col] >= ts_start) & (df[time_col] <= ts_end)].copy()
        return out.reset_index(drop=True)

    print("=" * 70)
    print("MO-1: Depth Exploration Experiment")
    print("=" * 70)

    # --- Load data ---
    db.init_db()

    print("\nLoading 1m klines...")
    df_1m = None
    try:
        df_1m = db.read_klines()
        if df_1m is not None and len(df_1m) > 0:
            print(f"  {len(df_1m)} candles (from DuckDB)")
        else:
            df_1m = None
    except Exception:
        pass
    if df_1m is None:
        print("  Falling back to parquet...")
        df_1m = pd.read_parquet(PARQUET_FILE)
        print(f"  {len(df_1m)} candles (from parquet)")

    print("Loading 30m klines...")
    df_30m = None
    try:
        df_30m = db.read_klines_30m()
        if df_30m is not None and len(df_30m) > 0:
            print(f"  {len(df_30m)} 30m candles")
        else:
            df_30m = None
    except Exception:
        pass
    if df_30m is None:
        p30 = DATA_DIR / "raw" / "btcusdt_30m.parquet"
        if p30.exists():
            df_30m = pd.read_parquet(p30)
            print(f"  {len(df_30m)} 30m candles (from parquet)")

    print("Loading 4h klines...")
    df_4h = None
    try:
        df_4h = db.read_klines_4h()
        if df_4h is not None and len(df_4h) > 0:
            print(f"  {len(df_4h)} 4h candles")
        else:
            df_4h = None
    except Exception:
        pass
    if df_4h is None:
        p4h = DATA_DIR / "raw" / "btcusdt_4h.parquet"
        if p4h.exists():
            df_4h = pd.read_parquet(p4h)
            print(f"  {len(df_4h)} 4h candles (from parquet)")

    print("Loading Coinbase klines...")
    df_coinbase = None
    try:
        df_coinbase = db.read_coinbase_klines()
        if df_coinbase is not None and len(df_coinbase) > 0:
            print(f"  {len(df_coinbase)} Coinbase 1m candles")
        else:
            df_coinbase = None
    except Exception:
        pass

    print("Loading ETH klines...")
    df_eth = None
    try:
        df_eth = db.read_eth_klines()
        if df_eth is not None and len(df_eth) > 0:
            print(f"  {len(df_eth)} ETH 1m candles")
        else:
            df_eth = None
    except Exception:
        pass

    # Filter ALL dataframes by date range
    df_1m = _filter_df(df_1m)
    df_30m = _filter_df(df_30m)
    df_4h = _filter_df(df_4h)
    df_coinbase = _filter_df(df_coinbase)
    df_eth = _filter_df(df_eth)
    print(f"\nFiltered to {len(df_1m)} candles ({sample_start} -> {sample_end})")

    # Generate labels
    print("\nGenerating labels...")
    labels = generate_labels(df_1m)
    n_up = (labels["zone"] == "up").sum()
    n_down = (labels["zone"] == "down").sum()
    n_neutral = (labels["zone"] == "neutral").sum()
    print(f"  Up: {n_up}, Down: {n_down}, Neutral: {n_neutral}")

    # Build features
    print("\nBuilding features...")
    features = build_features(
        df_1m, btc_30m=df_30m, btc_4h=df_4h,
        coinbase_1m=df_coinbase, eth_1m=df_eth,
    )
    print(f"  {len(get_feature_columns(features))} total features, {len(features)} samples")

    # Merge and filter to non-neutral
    merged = labels.merge(features, on="window_start", how="inner")
    merged = merged[merged["zone"].isin(["up", "down"])].copy()
    merged["label"] = (merged["zone"] == "up").astype(int)
    merged = merged.sort_values("window_start").reset_index(drop=True)
    print(f"  {len(merged)} non-neutral samples after merge")

    # Verify v10 features exist
    missing = [f for f in V10_FEATURES if f not in merged.columns]
    if missing:
        raise ValueError(f"Missing v10 features: {missing}")
    print(f"  v10 features all present: {V10_FEATURES}")

    return merged


def run_depth_experiment(merged, depth, grow_policy=None):
    """Run CV for a given depth/policy, return metrics dict."""
    cv = PurgedTimeSeriesSplit(n_splits=4, purge_gap=12)
    X = merged[V10_FEATURES].values
    y = merged["label"].values

    cv_aucs = []
    train_aucs = []
    iterations_used = []

    for fold_idx, (train_idx, test_idx) in enumerate(cv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        params = dict(
            depth=depth,
            iterations=300,
            learning_rate=0.05,
            early_stopping_rounds=30,
            nan_mode="Min",
            verbose=0,
            random_seed=42,
        )
        if grow_policy is not None:
            params["grow_policy"] = grow_policy

        model = CatBoostClassifier(**params)
        model.fit(
            X_train, y_train,
            eval_set=(X_test, y_test),
            use_best_model=True,
        )

        # CV AUC
        y_prob_test = model.predict_proba(X_test)[:, 1]
        cv_auc = roc_auc_score(y_test, y_prob_test)
        cv_aucs.append(cv_auc)

        # Train AUC
        y_prob_train = model.predict_proba(X_train)[:, 1]
        train_auc = roc_auc_score(y_train, y_prob_train)
        train_aucs.append(train_auc)

        # Iterations used (best_iteration_ is 0-indexed)
        iters = model.get_best_iteration() if hasattr(model, 'get_best_iteration') else model.tree_count_
        iterations_used.append(iters)

    result = {
        "depth": depth,
        "grow_policy": grow_policy or "SymmetricTree",
        "cv_auc_mean": round(float(np.mean(cv_aucs)), 6),
        "cv_auc_std": round(float(np.std(cv_aucs)), 6),
        "train_auc_mean": round(float(np.mean(train_aucs)), 6),
        "gap": round(float(np.mean(train_aucs) - np.mean(cv_aucs)), 6),
        "avg_iterations": round(float(np.mean(iterations_used)), 1),
        "fold_cv_aucs": [round(a, 6) for a in cv_aucs],
        "fold_train_aucs": [round(a, 6) for a in train_aucs],
        "fold_iterations": iterations_used,
    }
    return result


def main():
    merged = load_and_prepare()

    results = []
    t0 = time.time()

    # --- Part 1: Depth sweep with default SymmetricTree ---
    depths = [2, 3, 4, 5, 6, 7, 8]
    print(f"\n{'=' * 70}")
    print("Part 1: Depth sweep (SymmetricTree)")
    print(f"{'=' * 70}")

    for depth in depths:
        print(f"  Training depth={depth} ...", end=" ", flush=True)
        r = run_depth_experiment(merged, depth)
        results.append(r)
        print(f"CV AUC={r['cv_auc_mean']:.4f}±{r['cv_auc_std']:.4f}  "
              f"Train={r['train_auc_mean']:.4f}  Gap={r['gap']:.4f}  "
              f"Iters={r['avg_iterations']:.0f}")

    # --- Part 2: Grow policy variations at depth=3 ---
    print(f"\n{'=' * 70}")
    print("Part 2: Grow policy variations (depth=3)")
    print(f"{'=' * 70}")

    policies = ["Depthwise", "Lossguide"]
    for policy in policies:
        print(f"  Training depth=3 policy={policy} ...", end=" ", flush=True)
        r = run_depth_experiment(merged, depth=3, grow_policy=policy)
        results.append(r)
        print(f"CV AUC={r['cv_auc_mean']:.4f}±{r['cv_auc_std']:.4f}  "
              f"Train={r['train_auc_mean']:.4f}  Gap={r['gap']:.4f}  "
              f"Iters={r['avg_iterations']:.0f}")

    elapsed = time.time() - t0

    # --- Summary table ---
    print(f"\n{'=' * 70}")
    print("SUMMARY TABLE")
    print(f"{'=' * 70}")
    header = f"{'Depth':<7} {'Policy':<15} {'CV AUC':<18} {'Train AUC':<12} {'Gap':<10} {'Avg Iters':<10}"
    print(header)
    print("-" * len(header))
    for r in results:
        cv_str = f"{r['cv_auc_mean']:.4f}±{r['cv_auc_std']:.4f}"
        print(f"{r['depth']:<7} {r['grow_policy']:<15} {cv_str:<18} "
              f"{r['train_auc_mean']:<12.4f} {r['gap']:<10.4f} {r['avg_iterations']:<10.0f}")

    print(f"\nTotal elapsed: {elapsed:.1f}s")

    # --- Save results ---
    output = {
        "experiment": "MO-1 Depth Exploration",
        "sample_range": "2026-03-01 to 2026-03-31",
        "features": V10_FEATURES,
        "cv_config": {"n_splits": 4, "purge_gap": 12},
        "fixed_params": {
            "iterations": 300,
            "learning_rate": 0.05,
            "early_stopping_rounds": 30,
            "nan_mode": "Min",
            "random_seed": 42,
        },
        "n_samples": len(merged),
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    }

    out_path = Path(__file__).resolve().parent / "mo1_depth_explore.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
