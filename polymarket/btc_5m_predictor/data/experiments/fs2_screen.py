"""FS-2: Single-feature AUC screening experiment.

For each feature individually, train a simple CatBoost classifier (depth=3, iter=100)
and measure AUC on a time-split holdout (last 20%).
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
from training.train_pipeline import split_feature_cols
import db


def main():
    sample_start = "2026-03-01"
    sample_end = "2026-03-31"
    ts_start = pd.Timestamp(sample_start, tz="UTC")
    ts_end = pd.Timestamp(sample_end, tz="UTC")

    def _filter_df(df, time_col="open_time"):
        if df is None:
            return None
        out = df.copy()
        out = out[out[time_col] >= ts_start]
        out = out[out[time_col] <= ts_end]
        return out.reset_index(drop=True)

    # --- 1. Load data ---
    print("=" * 60)
    print("FS-2: Single-Feature AUC Screening")
    print("=" * 60)

    print("\nLoading 1m klines...")
    db.init_db()
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

    # --- Filter ALL dataframes by date range ---
    df_1m = _filter_df(df_1m)
    df_30m = _filter_df(df_30m)
    df_4h = _filter_df(df_4h)
    df_coinbase = _filter_df(df_coinbase)
    df_eth = _filter_df(df_eth)
    print(f"\nFiltered to {len(df_1m)} candles ({sample_start} -> {sample_end})")

    # --- 2. Generate labels ---
    print("\nGenerating labels...")
    labels = generate_labels(df_1m)
    n_up = (labels["zone"] == "up").sum()
    n_down = (labels["zone"] == "down").sum()
    n_neutral = (labels["zone"] == "neutral").sum()
    print(f"  Up: {n_up}, Down: {n_down}, Neutral: {n_neutral}")

    # --- 3. Build features ---
    print("\nBuilding features...")
    features = build_features(df_1m, btc_30m=df_30m, btc_4h=df_4h,
                              coinbase_1m=df_coinbase, eth_1m=df_eth)
    all_feat_cols = get_feature_columns(features)
    print(f"  {len(all_feat_cols)} features, {len(features)} samples")

    # --- 4. Merge labels + features ---
    merged = labels.merge(features, on="window_start", how="inner")
    # Filter to non-neutral
    merged = merged[merged["zone"].isin(["up", "down"])].copy()
    merged["label"] = (merged["zone"] == "up").astype(int)
    merged = merged.sort_values("window_start").reset_index(drop=True)
    print(f"  {len(merged)} non-neutral samples after merge")

    # --- 5. Split features ---
    base_cols, futures_cols = split_feature_cols(all_feat_cols, features)
    print(f"  Base features: {len(base_cols)}, Futures features: {len(futures_cols)}")

    # --- 6. Time-split holdout (last 20%) ---
    split_idx = int(len(merged) * 0.8)
    train_df = merged.iloc[:split_idx].reset_index(drop=True)
    test_df = merged.iloc[split_idx:].reset_index(drop=True)
    y_train = train_df["label"].values
    y_test = test_df["label"].values
    print(f"\n  Train: {len(train_df)} samples, Test: {len(test_df)} samples")
    print(f"  Train label dist: {y_train.mean():.3f}, Test label dist: {y_test.mean():.3f}")

    # --- 7. Screen each feature ---
    print(f"\nScreening {len(all_feat_cols)} features individually...")
    print("-" * 60)

    results = []
    start_time = time.time()

    for i, feat in enumerate(all_feat_cols):
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start_time
            print(f"  [{i+1}/{len(all_feat_cols)}] elapsed={elapsed:.0f}s ...")

        X_tr = train_df[[feat]].values
        X_te = test_df[[feat]].values

        # Check if feature has enough non-NaN values
        nan_train = np.isnan(X_tr).sum()
        nan_test = np.isnan(X_te).sum()
        if nan_train > len(X_tr) * 0.9 or nan_test > len(X_te) * 0.9:
            results.append({"feature": feat, "auc": None, "reason": "too_many_nans"})
            continue

        try:
            model = CatBoostClassifier(
                depth=3,
                iterations=100,
                learning_rate=0.05,
                verbose=0,
                random_seed=42,
                nan_mode="Min",
            )
            model.fit(X_tr, y_train)
            y_prob = model.predict_proba(X_te)[:, 1]
            auc = roc_auc_score(y_test, y_prob)
            results.append({"feature": feat, "auc": round(auc, 6)})
        except Exception as e:
            results.append({"feature": feat, "auc": None, "reason": str(e)[:100]})

    elapsed_total = time.time() - start_time
    print(f"\nDone! Total time: {elapsed_total:.1f}s")

    # --- 8. Rank results ---
    valid = [r for r in results if r["auc"] is not None]
    valid.sort(key=lambda x: x["auc"], reverse=True)
    failed = [r for r in results if r["auc"] is None]

    above_052 = [r for r in valid if r["auc"] > 0.52]

    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"  Total features screened: {len(all_feat_cols)}")
    print(f"  Valid AUC computed: {len(valid)}")
    print(f"  Failed/skipped: {len(failed)}")
    print(f"  Features with AUC > 0.52: {len(above_052)}")

    print(f"\nTop 30 features by AUC:")
    print(f"{'Rank':<6} {'Feature':<45} {'AUC':<10}")
    print("-" * 61)
    for rank, r in enumerate(valid[:30], 1):
        print(f"{rank:<6} {r['feature']:<45} {r['auc']:.6f}")

    # --- v10 features ranking ---
    v10_features = ["taker_vol_raw", "price_vs_rvwap_60", "cvd_slope_10",
                    "hour4_sin", "vpt_sum_30"]
    print(f"\nv10 5-feature rankings:")
    print(f"{'Feature':<45} {'AUC':<10} {'Rank':<6}")
    print("-" * 61)
    for vf in v10_features:
        found = False
        for rank, r in enumerate(valid, 1):
            if r["feature"] == vf:
                print(f"{vf:<45} {r['auc']:.6f} {rank}")
                found = True
                break
        if not found:
            print(f"{vf:<45} {'N/A':<10} {'N/A'}")

    # --- 9. Save results ---
    output = {
        "experiment": "FS-2 Single-Feature AUC Screening",
        "sample_start": sample_start,
        "sample_end": sample_end,
        "total_features": len(all_feat_cols),
        "valid_features": len(valid),
        "failed_features": len(failed),
        "above_052_count": len(above_052),
        "params": {"depth": 3, "iterations": 100, "learning_rate": 0.05},
        "train_size": len(train_df),
        "test_size": len(test_df),
        "elapsed_seconds": round(elapsed_total, 1),
        "ranked_features": valid,
        "failed_features_detail": failed,
    }

    out_path = Path(__file__).resolve().parent / "fs2_feature_screening.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
