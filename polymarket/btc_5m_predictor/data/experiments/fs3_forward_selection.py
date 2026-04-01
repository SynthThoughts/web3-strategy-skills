"""FS-3/4/5: Greedy forward feature selection.

Starting from v10's 5 base features, greedily add 10 more features (to 15 total)
using 4-fold PurgedTimeSeriesSplit CV with CatBoost.

Outputs:
  - FS-3: best 8-feature set (base 5 + 3 added)
  - FS-4: best 10-feature set (base 5 + 5 added)
  - FS-5: best 15-feature set (base 5 + 10 added)
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


# v10 baseline features
V10_FEATURES = [
    "taker_vol_raw",
    "price_vs_rvwap_60",
    "cvd_slope_10",
    "hour4_sin",
    "vpt_sum_30",
]

TOP_K_CANDIDATES = 50
MAX_ADD = 10  # add up to 10 features (5 base + 10 = 15 total)


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

    return merged, features


def cv_auc(merged: pd.DataFrame, feat_cols: list[str], n_splits: int = 4) -> float:
    """Evaluate feature set using PurgedTimeSeriesSplit CV, return mean AUC."""
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

    return float(np.mean(aucs))


def main():
    sample_start = "2026-03-01"
    sample_end = "2026-03-31"

    print("=" * 70)
    print("FS-3/4/5: Greedy Forward Feature Selection")
    print("=" * 70)

    # --- Load FS-2 results ---
    fs2_path = Path(__file__).resolve().parent / "fs2_feature_screening.json"
    with open(fs2_path) as f:
        fs2 = json.load(f)

    # Build candidate list: top 50 from FS-2, excluding v10 features
    v10_set = set(V10_FEATURES)
    fs2_ranked = fs2["ranked_features"]
    candidates = []
    fs2_auc_map = {}
    for r in fs2_ranked:
        feat = r["feature"]
        auc = r["auc"]
        if auc is None:
            continue
        fs2_auc_map[feat] = auc
        if feat not in v10_set and len(candidates) < TOP_K_CANDIDATES:
            candidates.append(feat)

    print(f"\nFS-2 top {TOP_K_CANDIDATES} candidates (excluding v10):")
    print(f"  AUC range: {fs2_auc_map.get(candidates[-1], 0):.6f} - {fs2_auc_map.get(candidates[0], 0):.6f}")

    # --- Load data ---
    merged, features = load_and_prepare_data(sample_start, sample_end)

    # Verify all v10 features exist
    all_cols = set(merged.columns)
    for f in V10_FEATURES:
        if f not in all_cols:
            print(f"  WARNING: v10 feature '{f}' not found in data!")
            return

    # Filter candidates to those actually present in data
    candidates = [c for c in candidates if c in all_cols]
    print(f"  {len(candidates)} candidates available in data")

    # --- Evaluate baseline (v10 5 features) ---
    print("\nEvaluating v10 baseline (5 features)...")
    base_auc = cv_auc(merged, V10_FEATURES)
    print(f"  Baseline CV AUC: {base_auc:.6f}")

    # --- Greedy forward selection ---
    print(f"\nStarting forward selection (adding up to {MAX_ADD} features)...")
    print("-" * 70)
    header = f"{'Step':<6} {'Added Feature':<35} {'Indiv AUC':<12} {'Combined AUC':<14} {'Delta AUC':<12} {'Total':<6}"
    print(header)
    print("-" * 70)

    current_features = list(V10_FEATURES)
    remaining_candidates = list(candidates)
    prev_auc = base_auc
    selection_path = []
    t_start = time.time()

    for step in range(1, MAX_ADD + 1):
        step_start = time.time()
        best_feat = None
        best_auc = -1.0

        for cand in remaining_candidates:
            trial_feats = current_features + [cand]
            auc = cv_auc(merged, trial_feats)
            if auc > best_auc:
                best_auc = auc
                best_feat = cand

        if best_feat is None:
            print("  No more candidates available.")
            break

        # Add best feature
        current_features.append(best_feat)
        remaining_candidates.remove(best_feat)
        delta = best_auc - prev_auc
        indiv_auc = fs2_auc_map.get(best_feat, float("nan"))

        record = {
            "step": step,
            "feature_added": best_feat,
            "individual_auc_fs2": round(indiv_auc, 6),
            "combined_cv_auc": round(best_auc, 6),
            "delta_auc": round(delta, 6),
            "total_features": len(current_features),
            "step_time_s": round(time.time() - step_start, 1),
        }
        selection_path.append(record)

        print(
            f"{step:<6} {best_feat:<35} {indiv_auc:<12.6f} {best_auc:<14.6f} "
            f"{delta:+<12.6f} {len(current_features):<6}"
        )

        prev_auc = best_auc

    elapsed = time.time() - t_start

    # --- Build feature sets ---
    fs3_features = list(V10_FEATURES) + [r["feature_added"] for r in selection_path[:3]]
    fs4_features = list(V10_FEATURES) + [r["feature_added"] for r in selection_path[:5]]
    fs5_features = list(V10_FEATURES) + [r["feature_added"] for r in selection_path[:10]]

    # Get the CV AUC at each milestone
    fs3_auc = selection_path[2]["combined_cv_auc"] if len(selection_path) >= 3 else None
    fs4_auc = selection_path[4]["combined_cv_auc"] if len(selection_path) >= 5 else None
    fs5_auc = selection_path[9]["combined_cv_auc"] if len(selection_path) >= 10 else None

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Baseline (v10, 5 features) CV AUC: {base_auc:.6f}")
    if fs3_auc:
        print(f"  FS-3 (8 features) CV AUC:          {fs3_auc:.6f}  (+{fs3_auc - base_auc:.6f})")
    if fs4_auc:
        print(f"  FS-4 (10 features) CV AUC:         {fs4_auc:.6f}  (+{fs4_auc - base_auc:.6f})")
    if fs5_auc:
        print(f"  FS-5 (15 features) CV AUC:         {fs5_auc:.6f}  (+{fs5_auc - base_auc:.6f})")
    print(f"  Total elapsed: {elapsed:.1f}s")

    print(f"\n  FS-3 features: {fs3_features}")
    print(f"  FS-4 features: {fs4_features}")
    print(f"  FS-5 features: {fs5_features}")

    # --- Save results ---
    output = {
        "experiment": "FS-3/4/5 Forward Feature Selection",
        "sample_start": sample_start,
        "sample_end": sample_end,
        "baseline_features": V10_FEATURES,
        "baseline_cv_auc": round(base_auc, 6),
        "cv_params": {
            "n_splits": 4,
            "purge_gap": 12,
        },
        "catboost_params": {
            "depth": 3,
            "iterations": 200,
            "learning_rate": 0.05,
            "early_stopping_rounds": 30,
            "nan_mode": "Min",
        },
        "top_k_candidates": TOP_K_CANDIDATES,
        "n_samples": len(merged),
        "selection_path": selection_path,
        "feature_sets": {
            "fs3_8feat": {
                "features": fs3_features,
                "cv_auc": fs3_auc,
                "delta_from_baseline": round(fs3_auc - base_auc, 6) if fs3_auc else None,
            },
            "fs4_10feat": {
                "features": fs4_features,
                "cv_auc": fs4_auc,
                "delta_from_baseline": round(fs4_auc - base_auc, 6) if fs4_auc else None,
            },
            "fs5_15feat": {
                "features": fs5_features,
                "cv_auc": fs5_auc,
                "delta_from_baseline": round(fs5_auc - base_auc, 6) if fs5_auc else None,
            },
        },
        "elapsed_seconds": round(elapsed, 1),
    }

    out_path = Path(__file__).resolve().parent / "fs3_forward_selection.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
