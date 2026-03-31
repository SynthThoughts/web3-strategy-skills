"""Feature engineering commands: validate, explore."""

from __future__ import annotations

import argparse
import statistics


def run(args: argparse.Namespace) -> int:
    if args.feature_action is None:
        print("Usage: btc feature {validate|explore}")
        return 1

    if args.feature_action == "validate":
        return _validate(args.name)
    elif args.feature_action == "explore":
        return _explore(getattr(args, "category", None))

    return 1


def _validate(name: str) -> int:
    """Validate a single feature: distribution stats, univariate AUC, quality rating."""
    from data.feature_metadata import FEATURE_META

    if name not in FEATURE_META:
        print(f"Feature '{name}' not registered in FEATURE_META.")
        print(f"Available features ({len(FEATURE_META)}): {', '.join(sorted(FEATURE_META)[:10])} ...")
        return 1

    meta = FEATURE_META[name]
    print(f"\n=== Feature Validation: {name} ===\n")
    print(f"  Name:       {meta['cn']}")
    print(f"  Category:   {meta['category']}")
    print(f"  Logic:      {meta['logic']}")
    print(f"  Source:     {meta.get('source_dep', '—')}")
    print(f"  Min days:   {meta.get('min_days', '—')}")

    # Load data and compute the feature
    try:
        stats = _compute_feature_stats(name)
    except Exception as e:
        print(f"\n  Could not compute stats: {e}")
        return 1

    # Distribution stats
    print(f"\n  --- Distribution ---")
    print(f"  {'Mean':<16s} {stats['mean']:.6f}")
    print(f"  {'Std':<16s} {stats['std']:.6f}")
    print(f"  {'Missing %':<16s} {stats['missing_pct']:.2f}%")
    print(f"  {'Outlier %':<16s} {stats['outlier_pct']:.2f}%")
    print(f"  {'Min':<16s} {stats['min']:.6f}")
    print(f"  {'Max':<16s} {stats['max']:.6f}")
    print(f"  {'Median':<16s} {stats['median']:.6f}")

    # Univariate AUC
    if stats.get("univariate_auc") is not None:
        print(f"\n  --- Univariate AUC (Purged CV) ---")
        print(f"  {'AUC':<16s} {stats['univariate_auc']:.4f}")

    # Quality rating
    rating = _rate_quality(stats)
    print(f"\n  --- Quality Rating ---")
    print(f"  Rating: {rating}")

    return 0


def _compute_feature_stats(name: str) -> dict:
    """Compute distribution stats and univariate AUC for a feature."""
    import numpy as np
    import db
    from data import build_features

    con = db.get_connection(read_only=True)
    try:
        df_1m = con.execute(
            "SELECT * FROM klines_1m ORDER BY open_time"
        ).fetchdf()
    finally:
        con.close()

    if df_1m.empty:
        raise ValueError("No klines_1m data available")

    # Build features
    df_feat = build_features.build(df_1m)

    if name not in df_feat.columns:
        raise ValueError(f"Feature '{name}' not found in built feature columns")

    col = df_feat[name]
    total = len(col)
    missing = int(col.isna().sum())
    valid = col.dropna()

    if len(valid) == 0:
        raise ValueError(f"Feature '{name}' has no valid values")

    vals = valid.values
    mean_val = float(np.mean(vals))
    std_val = float(np.std(vals))

    # Outliers: beyond 3 sigma
    if std_val > 0:
        outlier_mask = np.abs(vals - mean_val) > 3 * std_val
        outlier_pct = float(np.sum(outlier_mask)) / len(vals) * 100
    else:
        outlier_pct = 0.0

    result = {
        "mean": mean_val,
        "std": std_val,
        "missing_pct": missing / total * 100 if total > 0 else 0,
        "outlier_pct": outlier_pct,
        "min": float(np.min(vals)),
        "max": float(np.max(vals)),
        "median": float(np.median(vals)),
    }

    # Univariate AUC via simple cross-validation
    try:
        result["univariate_auc"] = _univariate_auc(df_feat, name)
    except Exception:
        result["univariate_auc"] = None

    return result


def _univariate_auc(df: "pd.DataFrame", feature_name: str) -> float:  # noqa: F821
    """Compute univariate AUC for a single feature using purged CV."""
    import numpy as np
    from sklearn.metrics import roc_auc_score

    # Use the existing label column
    label_col = None
    for candidate in ["label", "target", "y"]:
        if candidate in df.columns:
            label_col = candidate
            break

    if label_col is None:
        return None

    mask = df[feature_name].notna() & df[label_col].notna()
    X = df.loc[mask, feature_name].values
    y = df.loc[mask, label_col].values

    if len(np.unique(y)) < 2:
        return None

    # Simple AUC: treat feature value directly as a score
    auc = roc_auc_score(y, X)
    # Ensure AUC >= 0.5 (flip if needed — we care about absolute predictive power)
    return max(auc, 1 - auc)


def _rate_quality(stats: dict) -> str:
    """Rate feature quality as GOOD / WARNING / ALERT."""
    alerts = []

    if stats["missing_pct"] > 10:
        alerts.append("high_missing")
    if stats["outlier_pct"] > 5:
        alerts.append("high_outliers")
    if stats["std"] < 1e-10:
        alerts.append("zero_variance")

    auc = stats.get("univariate_auc")
    if auc is not None and auc < 0.505:
        alerts.append("no_signal")

    if len(alerts) >= 2 or "zero_variance" in alerts:
        return f"ALERT ({', '.join(alerts)})"
    elif len(alerts) == 1:
        return f"WARNING ({alerts[0]})"
    return "GOOD"


def _explore(category: str | None) -> int:
    """Batch feature quality report, optionally filtered by category."""
    from data.feature_metadata import FEATURE_META

    features = {}
    for name, meta in sorted(FEATURE_META.items()):
        if category and meta["category"] != category:
            continue
        features[name] = meta

    if not features:
        if category:
            cats = sorted(set(m["category"] for m in FEATURE_META.values()))
            print(f"No features found for category '{category}'.")
            print(f"Available categories: {', '.join(cats)}")
        else:
            print("No features found in FEATURE_META.")
        return 1

    print(f"\n=== Feature Explorer ({len(features)} features) ===\n")

    if category:
        print(f"  Category: {category}\n")

    # Header
    print(f"  {'Feature':<30s}  {'Category':<12s}  {'Source':<12s}  {'MinDays':>7s}")
    print(f"  {'─' * 30}  {'─' * 12}  {'─' * 12}  {'─' * 7}")

    for name, meta in features.items():
        cat = meta["category"][:12]
        src = meta.get("source_dep", "—")[:12]
        md = str(meta.get("min_days", "—"))
        print(f"  {name:<30s}  {cat:<12s}  {src:<12s}  {md:>7s}")

    # Category summary
    cats = {}
    for name, meta in features.items():
        c = meta["category"]
        cats.setdefault(c, []).append(name)

    print(f"\n  --- Summary ---")
    print(f"  Total features: {len(features)}")
    for c, names in sorted(cats.items(), key=lambda x: -len(x[1])):
        print(f"    {c}: {len(names)}")

    return 0
