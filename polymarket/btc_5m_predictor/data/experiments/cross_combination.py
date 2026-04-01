"""Unit 6: Cross-combination experiments.

Evaluate all combinations of feature sets x depths using 4-fold
PurgedTimeSeriesSplit CV + holdout (80/20 time split).

Feature sets:
  v10  (5f):  taker_vol_raw, price_vs_rvwap_60, cvd_slope_10, hour4_sin, vpt_sum_30
  FS-3 (8f):  v10 + skew_30, mfi_14, macd_signal
  FS-4 (10f): FS-3 + trix_30, rally_30
  FS-5 (15f): FS-4 + obv_slope_20, buy_ratio_20, MIN_60, rally_60, toxicity_20

Depths: [3, 4, 5]
Grow policy: SymmetricTree only
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
from data.features import build_features
from data.labels import generate_labels
from training.train_pipeline import PurgedTimeSeriesSplit
import db

# ---------------------------------------------------------------------------
# Feature set definitions
# ---------------------------------------------------------------------------
V10_FEATURES = [
    "taker_vol_raw",
    "price_vs_rvwap_60",
    "cvd_slope_10",
    "hour4_sin",
    "vpt_sum_30",
]

FS3_FEATURES = V10_FEATURES + [
    "skew_30",
    "mfi_14",
    "macd_signal",
]

FS4_FEATURES = FS3_FEATURES + [
    "trix_30",
    "rally_30",
]

FS5_FEATURES = FS4_FEATURES + [
    "obv_slope_20",
    "buy_ratio_20",
    "MIN_60",
    "rally_60",
    "toxicity_20",
]

FEATURE_SETS = {
    "v10_5f": V10_FEATURES,
    "FS3_8f": FS3_FEATURES,
    "FS4_10f": FS4_FEATURES,
    "FS5_15f": FS5_FEATURES,
}

DEPTHS = [3, 4, 5]

CATBOOST_BASE_PARAMS = dict(
    iterations=300,
    learning_rate=0.05,
    early_stopping_rounds=30,
    nan_mode="Min",
    random_seed=42,
    verbose=0,
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_and_prepare_data(sample_start: str, sample_end: str):
    """Load data from DuckDB, filter by date range, build features + labels."""
    ts_start = pd.Timestamp(sample_start, tz="UTC")
    ts_end = pd.Timestamp(sample_end, tz="UTC")

    def _filter_df(df, time_col="open_time"):
        if df is None:
            return None
        out = df[(df[time_col] >= ts_start) & (df[time_col] <= ts_end)]
        return out.reset_index(drop=True)

    print("Loading data from DuckDB...")
    db.init_db()

    # 1m klines
    df_1m = None
    try:
        df_1m = db.read_klines()
        if df_1m is not None and len(df_1m) > 0:
            print(f"  1m: {len(df_1m)} candles")
        else:
            df_1m = None
    except Exception:
        pass
    if df_1m is None:
        df_1m = pd.read_parquet(PARQUET_FILE)
        print(f"  1m: {len(df_1m)} candles (parquet fallback)")

    # 30m klines
    df_30m = None
    try:
        df_30m = db.read_klines_30m()
        if df_30m is not None and len(df_30m) > 0:
            print(f"  30m: {len(df_30m)} candles")
        else:
            df_30m = None
    except Exception:
        pass
    if df_30m is None:
        p30 = DATA_DIR / "raw" / "btcusdt_30m.parquet"
        if p30.exists():
            df_30m = pd.read_parquet(p30)
            print(f"  30m: {len(df_30m)} candles (parquet)")

    # 4h klines
    df_4h = None
    try:
        df_4h = db.read_klines_4h()
        if df_4h is not None and len(df_4h) > 0:
            print(f"  4h: {len(df_4h)} candles")
        else:
            df_4h = None
    except Exception:
        pass
    if df_4h is None:
        p4h = DATA_DIR / "raw" / "btcusdt_4h.parquet"
        if p4h.exists():
            df_4h = pd.read_parquet(p4h)
            print(f"  4h: {len(df_4h)} candles (parquet)")

    # Coinbase
    df_coinbase = None
    try:
        df_coinbase = db.read_coinbase_klines()
        if df_coinbase is not None and len(df_coinbase) > 0:
            print(f"  Coinbase: {len(df_coinbase)} candles")
        else:
            df_coinbase = None
    except Exception:
        pass

    # ETH
    df_eth = None
    try:
        df_eth = db.read_eth_klines()
        if df_eth is not None and len(df_eth) > 0:
            print(f"  ETH: {len(df_eth)} candles")
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
    print(f"  Filtered to {len(df_1m)} 1m candles ({sample_start} -> {sample_end})")

    # Labels
    print("Generating labels...")
    labels = generate_labels(df_1m)

    # Features
    print("Building features...")
    features = build_features(
        df_1m, btc_30m=df_30m, btc_4h=df_4h,
        coinbase_1m=df_coinbase, eth_1m=df_eth,
    )

    # Merge
    merged = labels.merge(features, on="window_start", how="inner")
    merged = merged[merged["zone"].isin(["up", "down"])].copy()
    merged["label"] = (merged["zone"] == "up").astype(int)
    merged = merged.sort_values("window_start").reset_index(drop=True)
    print(f"  {len(merged)} non-neutral samples")

    return merged


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------
def run_cv(merged: pd.DataFrame, feat_cols: list[str], depth: int):
    """Run 4-fold PurgedTimeSeriesSplit CV.

    Returns dict with cv_auc_mean, cv_auc_std, train_auc_mean, fold details.
    """
    splitter = PurgedTimeSeriesSplit(n_splits=4, purge_gap=12)
    X = merged[feat_cols].values
    y = merged["label"].values

    cv_aucs = []
    train_aucs = []

    for train_idx, test_idx in splitter.split(X):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        model = CatBoostClassifier(depth=depth, **CATBOOST_BASE_PARAMS)
        model.fit(X_tr, y_tr, eval_set=(X_te, y_te), use_best_model=True)

        y_prob_te = model.predict_proba(X_te)[:, 1]
        cv_aucs.append(roc_auc_score(y_te, y_prob_te))

        y_prob_tr = model.predict_proba(X_tr)[:, 1]
        train_aucs.append(roc_auc_score(y_tr, y_prob_tr))

    return {
        "cv_auc_mean": float(np.mean(cv_aucs)),
        "cv_auc_std": float(np.std(cv_aucs)),
        "train_auc_mean": float(np.mean(train_aucs)),
        "fold_cv_aucs": [round(a, 6) for a in cv_aucs],
        "fold_train_aucs": [round(a, 6) for a in train_aucs],
    }


def run_holdout(merged: pd.DataFrame, feat_cols: list[str], depth: int):
    """Train on first 80% of time, test on last 20%. Return holdout AUC."""
    n = len(merged)
    split_idx = int(n * 0.8)

    X = merged[feat_cols].values
    y = merged["label"].values

    X_tr, X_te = X[:split_idx], X[split_idx:]
    y_tr, y_te = y[:split_idx], y[split_idx:]

    model = CatBoostClassifier(depth=depth, **CATBOOST_BASE_PARAMS)
    model.fit(X_tr, y_tr, eval_set=(X_te, y_te), use_best_model=True)

    y_prob = model.predict_proba(X_te)[:, 1]
    ho_auc = roc_auc_score(y_te, y_prob)

    # Also get train AUC for this split
    y_prob_tr = model.predict_proba(X_tr)[:, 1]
    train_auc = roc_auc_score(y_tr, y_prob_tr)

    return {
        "holdout_auc": float(ho_auc),
        "holdout_train_auc": float(train_auc),
        "train_size": split_idx,
        "test_size": n - split_idx,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    sample_start = "2026-03-01"
    sample_end = "2026-03-31"

    print("=" * 70)
    print("Unit 6: Cross-Combination Experiments")
    print("  Feature sets x Depths → best v11 candidate")
    print("=" * 70)

    merged = load_and_prepare_data(sample_start, sample_end)

    # Verify all features exist
    all_cols = set(merged.columns)
    for fs_name, feat_cols in FEATURE_SETS.items():
        missing = [f for f in feat_cols if f not in all_cols]
        if missing:
            print(f"  ERROR: {fs_name} missing features: {missing}")
            return

    # Run all combinations
    results = []
    t_total = time.time()

    total_combos = len(FEATURE_SETS) * len(DEPTHS)
    combo_idx = 0

    for fs_name, feat_cols in FEATURE_SETS.items():
        for depth in DEPTHS:
            combo_idx += 1
            print(f"\n[{combo_idx}/{total_combos}] {fs_name} x depth={depth} "
                  f"({len(feat_cols)} features)...")
            t0 = time.time()

            # CV evaluation
            cv_result = run_cv(merged, feat_cols, depth)

            # Holdout evaluation
            ho_result = run_holdout(merged, feat_cols, depth)

            elapsed = time.time() - t0

            # Compute gaps
            train_cv_gap = cv_result["train_auc_mean"] - cv_result["cv_auc_mean"]
            cv_ho_gap = cv_result["cv_auc_mean"] - ho_result["holdout_auc"]

            record = {
                "feature_set": fs_name,
                "features": feat_cols,
                "n_features": len(feat_cols),
                "depth": depth,
                "cv_auc_mean": round(cv_result["cv_auc_mean"], 6),
                "cv_auc_std": round(cv_result["cv_auc_std"], 6),
                "train_auc_mean": round(cv_result["train_auc_mean"], 6),
                "holdout_auc": round(ho_result["holdout_auc"], 6),
                "train_cv_gap": round(train_cv_gap, 6),
                "cv_ho_gap": round(cv_ho_gap, 6),
                "fold_cv_aucs": cv_result["fold_cv_aucs"],
                "fold_train_aucs": cv_result["fold_train_aucs"],
                "holdout_train_auc": round(ho_result["holdout_train_auc"], 6),
                "holdout_train_size": ho_result["train_size"],
                "holdout_test_size": ho_result["test_size"],
                "elapsed_s": round(elapsed, 1),
            }
            results.append(record)

            print(f"  CV AUC  = {record['cv_auc_mean']:.4f} ± {record['cv_auc_std']:.4f}")
            print(f"  HO AUC  = {record['holdout_auc']:.4f}")
            print(f"  Train   = {record['train_auc_mean']:.4f}")
            print(f"  Gaps    = Train-CV {record['train_cv_gap']:+.4f}  |  CV-HO {record['cv_ho_gap']:+.4f}")
            print(f"  ({elapsed:.1f}s)")

    total_elapsed = time.time() - t_total

    # --- Rank by CV AUC ---
    ranked = sorted(results, key=lambda r: r["cv_auc_mean"], reverse=True)

    # --- Formatted summary table ---
    print("\n" + "=" * 100)
    print("CROSS-COMBINATION RESULTS (sorted by CV AUC)")
    print("=" * 100)
    header = (
        f"{'Features':<12} {'Depth':<7} {'CV AUC':<18} "
        f"{'HO AUC':<10} {'Train AUC':<11} "
        f"{'Train-CV':<10} {'CV-HO':<10} {'Best?'}"
    )
    print(header)
    print("-" * 100)

    for i, r in enumerate(ranked):
        cv_str = f"{r['cv_auc_mean']:.4f}±{r['cv_auc_std']:.4f}"
        best_marker = "  <<<" if i < 3 else ""
        print(
            f"{r['feature_set']:<12} {r['depth']:<7} {cv_str:<18} "
            f"{r['holdout_auc']:<10.4f} {r['train_auc_mean']:<11.4f} "
            f"{r['train_cv_gap']:+<10.4f} {r['cv_ho_gap']:+<10.4f} {best_marker}"
        )

    # --- Top 3 candidates ---
    print("\n" + "=" * 70)
    print("TOP 3 v11 CANDIDATES")
    print("=" * 70)
    for i, r in enumerate(ranked[:3]):
        print(f"\n  #{i+1}: {r['feature_set']} x depth={r['depth']}")
        print(f"       CV AUC: {r['cv_auc_mean']:.6f} ± {r['cv_auc_std']:.6f}")
        print(f"       HO AUC: {r['holdout_auc']:.6f}")
        print(f"       Train-CV gap: {r['train_cv_gap']:+.6f}")
        print(f"       CV-HO gap:    {r['cv_ho_gap']:+.6f}")
        print(f"       Features ({r['n_features']}): {r['features']}")

    print(f"\nTotal elapsed: {total_elapsed:.1f}s")

    # --- Save JSON ---
    output = {
        "experiment": "Unit 6: Cross-Combination",
        "sample_start": sample_start,
        "sample_end": sample_end,
        "feature_sets_config": {k: v for k, v in FEATURE_SETS.items()},
        "depths": DEPTHS,
        "catboost_params": {
            k: v for k, v in CATBOOST_BASE_PARAMS.items() if k != "verbose"
        },
        "cv_params": {"n_splits": 4, "purge_gap": 12},
        "holdout_split": "80/20 time-based",
        "n_samples": len(merged),
        "results": results,
        "ranked_by_cv_auc": [
            {
                "rank": i + 1,
                "feature_set": r["feature_set"],
                "depth": r["depth"],
                "n_features": r["n_features"],
                "cv_auc_mean": r["cv_auc_mean"],
                "cv_auc_std": r["cv_auc_std"],
                "holdout_auc": r["holdout_auc"],
                "train_cv_gap": r["train_cv_gap"],
                "cv_ho_gap": r["cv_ho_gap"],
            }
            for i, r in enumerate(ranked)
        ],
        "top3_candidates": [
            {
                "rank": i + 1,
                "feature_set": r["feature_set"],
                "depth": r["depth"],
                "features": r["features"],
                "cv_auc_mean": r["cv_auc_mean"],
                "holdout_auc": r["holdout_auc"],
                "train_cv_gap": r["train_cv_gap"],
                "cv_ho_gap": r["cv_ho_gap"],
            }
            for i, r in enumerate(ranked[:3])
        ],
        "total_elapsed_s": round(total_elapsed, 1),
    }

    out_path = Path(__file__).resolve().parent / "cross_combination.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
