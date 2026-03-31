"""SHAP explanation, market state slicing, and online attribution analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ShapResult:
    """SHAP feature importance result."""
    feature_importances: list[tuple[str, float]]  # (feature_name, mean_abs_shap)
    top_dependencies: list[dict] | None = None  # per-feature value-shap relationship


@dataclass
class SliceResult:
    """Market state slice analysis."""
    slices: list[dict] = field(default_factory=list)
    # Each dict: {name, sample_count, auc, win_rate, mean_pnl}


@dataclass
class DiagnoseResult:
    """Three-layer online attribution."""
    time_attribution: list[dict] = field(default_factory=list)  # worst time periods
    feature_attribution: list[dict] = field(default_factory=list)  # drifted features in worst periods
    error_analysis: list[dict] = field(default_factory=list)  # high-confidence errors
    insufficient_data: bool = False
    no_anomalies: bool = False


def compute_shap(model_path: str | Path, feature_cols: list[str], data) -> ShapResult:
    """Compute SHAP values using CatBoost's built-in method.

    Args:
        model_path: Path to saved CatBoost model.
        feature_cols: Feature column names.
        data: DataFrame with feature columns.

    Returns:
        ShapResult with sorted feature importances.
    """
    from catboost import CatBoostClassifier, Pool

    model = CatBoostClassifier()
    model.load_model(str(model_path))

    pool = Pool(data[feature_cols])
    shap_values = model.get_feature_importance(type="ShapValues", data=pool)

    # ShapValues returns (n_samples, n_features + 1), last column is bias
    import numpy as np
    mean_abs_shap = np.abs(shap_values[:, :-1]).mean(axis=0)

    importances = sorted(
        zip(feature_cols, mean_abs_shap),
        key=lambda x: -x[1],
    )

    # Top-5 dependency descriptions
    top_deps = []
    for feat_name, importance in importances[:5]:
        feat_vals = data[feat_name].values
        feat_shap = shap_values[:, feature_cols.index(feat_name)]

        # Simple binning: low/mid/high thirds
        q33 = np.percentile(feat_vals[~np.isnan(feat_vals)], 33)
        q66 = np.percentile(feat_vals[~np.isnan(feat_vals)], 66)

        low_mask = feat_vals < q33
        mid_mask = (feat_vals >= q33) & (feat_vals < q66)
        high_mask = feat_vals >= q66

        top_deps.append({
            "feature": feat_name,
            "importance": float(importance),
            "low_range_shap": float(feat_shap[low_mask].mean()) if low_mask.any() else 0,
            "mid_range_shap": float(feat_shap[mid_mask].mean()) if mid_mask.any() else 0,
            "high_range_shap": float(feat_shap[high_mask].mean()) if high_mask.any() else 0,
        })

    return ShapResult(
        feature_importances=[(name, float(val)) for name, val in importances],
        top_dependencies=top_deps,
    )


def compute_market_slices(data, label_col: str = "label") -> SliceResult:
    """Slice analysis by market state (volatility x trend).

    Requires 'atr_ratio_5_20' and 'adx_14' columns in data.
    """
    import numpy as np

    result = SliceResult()

    if "atr_ratio_5_20" not in data.columns or "adx_14" not in data.columns:
        return result
    if label_col not in data.columns:
        return result

    atr_med = data["atr_ratio_5_20"].median()
    adx_med = data["adx_14"].median()

    slices_def = [
        ("High Vol + Trend", (data["atr_ratio_5_20"] >= atr_med) & (data["adx_14"] >= adx_med)),
        ("High Vol + Range", (data["atr_ratio_5_20"] >= atr_med) & (data["adx_14"] < adx_med)),
        ("Low Vol + Trend", (data["atr_ratio_5_20"] < atr_med) & (data["adx_14"] >= adx_med)),
        ("Low Vol + Range", (data["atr_ratio_5_20"] < atr_med) & (data["adx_14"] < adx_med)),
    ]

    for name, mask in slices_def:
        subset = data[mask]
        if len(subset) < 10:
            continue

        labels = subset[label_col].values
        # Simple win rate (label == 1)
        win_rate = float(np.mean(labels == 1))

        slice_info = {
            "name": name,
            "sample_count": len(subset),
            "win_rate": win_rate,
        }

        # AUC if prediction probability available
        if "pred_prob" in subset.columns:
            from sklearn.metrics import roc_auc_score
            try:
                slice_info["auc"] = float(roc_auc_score(labels, subset["pred_prob"].values))
            except Exception:
                slice_info["auc"] = None

        result.slices.append(slice_info)

    return result


def compute_diagnose(live_data, reference_dist: dict | None = None) -> DiagnoseResult:
    """Three-layer online attribution analysis.

    Layer 1: Time attribution — worst performing time periods
    Layer 2: Feature drift in worst periods
    Layer 3: High-confidence error analysis
    """
    import numpy as np

    result = DiagnoseResult()

    if live_data is None or len(live_data) < 50:
        result.insufficient_data = True
        return result

    # Check required columns
    has_correct = "correct" in live_data.columns
    has_pred = "pred_prob" in live_data.columns
    has_timestamp = "timestamp" in live_data.columns

    if not has_correct or not has_timestamp:
        result.insufficient_data = True
        return result

    # Layer 1: Time attribution
    live_data = live_data.copy()
    live_data["hour_group"] = (live_data["timestamp"].dt.hour // 4) * 4

    for h in sorted(live_data["hour_group"].unique()):
        subset = live_data[live_data["hour_group"] == h]
        if len(subset) < 10:
            continue
        accuracy = float(subset["correct"].mean())
        result.time_attribution.append({
            "period": f"{int(h):02d}:00-{int(h)+4:02d}:00",
            "sample_count": len(subset),
            "accuracy": accuracy,
        })

    # Sort by accuracy ascending (worst first)
    result.time_attribution.sort(key=lambda x: x["accuracy"])

    # Layer 2: Feature drift in worst period
    if result.time_attribution and reference_dist:
        from training.drift_monitor import compute_feature_drift

        worst = result.time_attribution[0]
        worst_hour = int(worst["period"][:2])
        worst_subset = live_data[live_data["hour_group"] == worst_hour]

        drift_results = []
        for feat_name, feat_ref in reference_dist.items():
            if feat_name in worst_subset.columns:
                vals = worst_subset[feat_name].dropna().tolist()
                if vals:
                    dr = compute_feature_drift(feat_name, feat_ref, vals)
                    if dr.psi is not None:
                        drift_results.append({"feature": feat_name, "psi": dr.psi, "status": dr.status})

        drift_results.sort(key=lambda x: -x["psi"])
        result.feature_attribution = drift_results[:5]

    # Layer 3: High-confidence error analysis
    if has_pred:
        errors = live_data[(live_data["pred_prob"] > 0.6) & (live_data["correct"] == False)]
        if len(errors) > 0:
            # Find features with largest z-score deviations
            numeric_cols = [c for c in errors.columns if errors[c].dtype in ("float64", "float32", "int64")]
            for _, row in errors.head(5).iterrows():
                error_info = {"timestamp": str(row.get("timestamp", "—"))}
                deviations = []
                for col in numeric_cols[:20]:  # limit to first 20 features
                    col_mean = live_data[col].mean()
                    col_std = live_data[col].std()
                    if col_std > 0:
                        z = abs((row[col] - col_mean) / col_std)
                        deviations.append((col, float(z)))
                deviations.sort(key=lambda x: -x[1])
                error_info["top_deviations"] = deviations[:5]
                result.error_analysis.append(error_info)

    if not result.time_attribution and not result.feature_attribution and not result.error_analysis:
        result.no_anomalies = True

    return result


def format_shap_report(result: ShapResult) -> str:
    """Format SHAP result as readable text."""
    lines = ["\n=== SHAP Feature Importance ===\n"]
    lines.append(f"  {'Rank':<6s}{'Feature':<30s}{'Mean |SHAP|':>12s}")
    lines.append(f"  {'─' * 6}{'─' * 30}{'─' * 12}")

    for i, (name, val) in enumerate(result.feature_importances[:20], 1):
        lines.append(f"  {i:<6d}{name:<30s}{val:>12.6f}")

    if result.top_dependencies:
        lines.append("\n  --- Top-5 Feature Dependencies ---\n")
        for dep in result.top_dependencies:
            lines.append(f"  {dep['feature']} (importance: {dep['importance']:.6f})")
            lines.append(f"    Low values  → SHAP: {dep['low_range_shap']:+.4f}")
            lines.append(f"    Mid values  → SHAP: {dep['mid_range_shap']:+.4f}")
            lines.append(f"    High values → SHAP: {dep['high_range_shap']:+.4f}")

    return "\n".join(lines)


def format_slice_report(result: SliceResult) -> str:
    """Format slice analysis as readable text."""
    lines = ["\n=== Market State Slice Analysis ===\n"]

    if not result.slices:
        lines.append("  No slice data available (requires atr_ratio_5_20 and adx_14 columns)")
        return "\n".join(lines)

    lines.append(f"  {'State':<25s}{'N':>8s}{'Win Rate':>10s}{'AUC':>8s}")
    lines.append(f"  {'─' * 25}{'─' * 8}{'─' * 10}{'─' * 8}")

    for s in result.slices:
        auc_str = f"{s['auc']:.4f}" if s.get("auc") is not None else "—"
        lines.append(f"  {s['name']:<25s}{s['sample_count']:>8d}{s['win_rate']:>10.2%}{auc_str:>8s}")

    return "\n".join(lines)


def format_diagnose_report(result: DiagnoseResult) -> str:
    """Format three-layer attribution as readable text."""
    lines = ["\n=== Online Attribution Analysis ===\n"]

    if result.insufficient_data:
        lines.append("  Insufficient data (< 50 samples). Run `btc data sync` to get more data.")
        return "\n".join(lines)

    if result.no_anomalies:
        lines.append("  No significant anomaly patterns detected.")
        return "\n".join(lines)

    # Layer 1
    lines.append("  --- Layer 1: Time Period Attribution ---\n")
    lines.append(f"  {'Period':<18s}{'N':>8s}{'Accuracy':>10s}")
    lines.append(f"  {'─' * 18}{'─' * 8}{'─' * 10}")
    for ta in result.time_attribution:
        lines.append(f"  {ta['period']:<18s}{ta['sample_count']:>8d}{ta['accuracy']:>10.2%}")

    # Layer 2
    if result.feature_attribution:
        lines.append("\n  --- Layer 2: Feature Drift in Worst Period ---\n")
        lines.append(f"  {'Feature':<30s}{'PSI':>8s}{'Status':<8s}")
        lines.append(f"  {'─' * 30}{'─' * 8}{'─' * 8}")
        for fa in result.feature_attribution:
            lines.append(f"  {fa['feature']:<30s}{fa['psi']:>8.4f}{fa['status']:<8s}")

    # Layer 3
    if result.error_analysis:
        lines.append("\n  --- Layer 3: High-Confidence Error Analysis ---\n")
        for err in result.error_analysis:
            lines.append(f"  Timestamp: {err['timestamp']}")
            if err.get("top_deviations"):
                for feat, z in err["top_deviations"]:
                    lines.append(f"    {feat}: z-score = {z:.2f}")
            lines.append("")

    return "\n".join(lines)
