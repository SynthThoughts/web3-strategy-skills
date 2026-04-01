"""FI: Feature Interaction experiments.

Evaluate multiple interaction-feature combinations on top of the v10 base 5
features using 4-fold PurgedTimeSeriesSplit CV with CatBoost.

Experiments:
  FI-baseline : v10 5 features only
  FI-1        : v10 + taker multiply interactions (9 features)
  FI-2        : v10 + taker ratio interactions (7 features)
  FI-3        : v10 + ALL 13 interaction features (18 features)
  FI-4        : v10 + conditional interactions (8 features)
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
# v10 base features
# ---------------------------------------------------------------------------
V10_FEATURES = [
    "taker_vol_raw",
    "price_vs_rvwap_60",
    "cvd_slope_10",
    "hour4_sin",
    "vpt_sum_30",
]

# ---------------------------------------------------------------------------
# Interaction feature groups
# ---------------------------------------------------------------------------
IX_MULTIPLY_TAKER = [
    "ix_mul_taker_cvd",
    "ix_mul_taker_pvr",
    "ix_mul_taker_vpt",
    "ix_time_taker_h4sin",
]

IX_RATIO_TAKER = [
    "ix_ratio_taker_cvd",
    "ix_ratio_taker_vpt",
]

IX_CONDITIONAL = [
    "ix_cond_taker_cvdpos",
    "ix_cond_taker_pvrpos",
    "ix_cond_pvr_cvdpos",
]

IX_ALL = [
    # multiply
    "ix_mul_taker_cvd",
    "ix_mul_taker_pvr",
    "ix_mul_taker_vpt",
    "ix_mul_cvd_pvr",
    "ix_mul_pvr_vpt",
    # ratio
    "ix_ratio_taker_cvd",
    "ix_ratio_taker_vpt",
    "ix_ratio_cvd_pvr",
    # conditional
    "ix_cond_taker_cvdpos",
    "ix_cond_taker_pvrpos",
    "ix_cond_pvr_cvdpos",
    # time
    "ix_time_taker_h4sin",
    "ix_time_cvd_h4sin",
]

EXPERIMENTS = {
    "FI-baseline": V10_FEATURES,
    "FI-1": V10_FEATURES + IX_MULTIPLY_TAKER,
    "FI-2": V10_FEATURES + IX_RATIO_TAKER,
    "FI-3": V10_FEATURES + IX_ALL,
    "FI-4": V10_FEATURES + IX_CONDITIONAL,
}


# ---------------------------------------------------------------------------
# Data loading (reused from fs3)
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

    # Features (includes interaction features via build_features)
    print("Building features...")
    features = build_features(
        df_1m,
        btc_30m=df_30m,
        btc_4h=df_4h,
        coinbase_1m=df_coinbase,
        eth_1m=df_eth,
    )

    # Merge
    merged = labels.merge(features, on="window_start", how="inner")
    merged = merged[merged["zone"].isin(["up", "down"])].copy()
    merged["label"] = (merged["zone"] == "up").astype(int)
    merged = merged.sort_values("window_start").reset_index(drop=True)
    print(f"  {len(merged)} non-neutral samples")

    return merged


# ---------------------------------------------------------------------------
# CV evaluation
# ---------------------------------------------------------------------------
def cv_auc(
    merged: pd.DataFrame, feat_cols: list[str], n_splits: int = 4
) -> tuple[float, float]:
    """Return (mean_auc, std_auc) across folds."""
    splitter = PurgedTimeSeriesSplit(n_splits=n_splits, purge_gap=12)
    X = merged[feat_cols].values
    y = merged["label"].values

    aucs = []
    for train_idx, test_idx in splitter.split(X):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        model = CatBoostClassifier(
            depth=3,
            iterations=200,
            learning_rate=0.05,
            early_stopping_rounds=30,
            verbose=0,
            random_seed=42,
            nan_mode="Min",
        )
        model.fit(X_tr, y_tr, eval_set=(X_te, y_te))
        y_prob = model.predict_proba(X_te)[:, 1]
        aucs.append(roc_auc_score(y_te, y_prob))

    return float(np.mean(aucs)), float(np.std(aucs))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    sample_start = "2026-03-01"
    sample_end = "2026-03-31"

    print("=" * 70)
    print("FI: Feature Interaction Experiments")
    print("=" * 70)

    merged = load_and_prepare_data(sample_start, sample_end)

    # Verify all required features exist
    all_cols = set(merged.columns)
    for name, feats in EXPERIMENTS.items():
        missing = [f for f in feats if f not in all_cols]
        if missing:
            print(f"  WARNING: {name} missing features: {missing}")
            return

    # Run experiments
    results = []
    baseline_auc = None
    t_total = time.time()

    for name, feat_cols in EXPERIMENTS.items():
        print(f"\nEvaluating {name} ({len(feat_cols)} features)...")
        t0 = time.time()
        mean_auc, std_auc = cv_auc(merged, feat_cols)
        elapsed = time.time() - t0

        if name == "FI-baseline":
            baseline_auc = mean_auc

        delta = mean_auc - baseline_auc if baseline_auc is not None else 0.0

        record = {
            "experiment": name,
            "features": feat_cols,
            "n_features": len(feat_cols),
            "cv_auc_mean": round(mean_auc, 6),
            "cv_auc_std": round(std_auc, 6),
            "delta_vs_baseline": round(delta, 6),
            "elapsed_s": round(elapsed, 1),
        }
        results.append(record)
        print(f"  AUC = {mean_auc:.6f} +/- {std_auc:.6f}  (delta={delta:+.6f}, {elapsed:.1f}s)")

    total_elapsed = time.time() - t_total

    # --- Summary table ---
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    header = f"{'Experiment':<16} {'Features':<10} {'CV AUC (mean±std)':<24} {'Delta vs Baseline'}"
    print(header)
    print("-" * 80)
    for r in results:
        auc_str = f"{r['cv_auc_mean']:.6f}±{r['cv_auc_std']:.6f}"
        delta_str = f"{r['delta_vs_baseline']:+.6f}" if r["experiment"] != "FI-baseline" else "---"
        print(f"{r['experiment']:<16} {r['n_features']:<10} {auc_str:<24} {delta_str}")
    print(f"\nTotal elapsed: {total_elapsed:.1f}s")

    # --- Save JSON ---
    output = {
        "experiment_group": "FI Feature Interaction",
        "sample_start": sample_start,
        "sample_end": sample_end,
        "cv_params": {"n_splits": 4, "purge_gap": 12},
        "catboost_params": {
            "depth": 3,
            "iterations": 200,
            "learning_rate": 0.05,
            "early_stopping_rounds": 30,
            "nan_mode": "Min",
            "random_seed": 42,
        },
        "n_samples": len(merged),
        "results": results,
        "total_elapsed_s": round(total_elapsed, 1),
    }

    out_path = Path(__file__).resolve().parent / "fi_experiments.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
