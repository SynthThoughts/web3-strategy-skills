"""Drift monitoring: PSI computation, probability distribution checks, retrain recommendation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


# PSI thresholds
PSI_DRIFT = 0.2
PSI_SHIFT = 0.1

# AUC alert threshold (fraction of training CV AUC)
AUC_ALERT_RATIO = 0.95


@dataclass
class FeatureDriftResult:
    """Drift result for a single feature."""
    name: str
    psi: float | None
    status: str  # STABLE, SHIFT, DRIFT, MISSING

    @property
    def status_label(self) -> str:
        return self.status


@dataclass
class DriftReport:
    """Overall drift report."""
    feature_results: list[FeatureDriftResult] = field(default_factory=list)
    prob_ks_pvalue: float | None = None
    prob_mean_shift: float | None = None
    prob_var_ratio: float | None = None
    rolling_auc: float | None = None
    rolling_win_rate: float | None = None
    calibration_bias: float | None = None
    sample_count: int = 0
    insufficient_data: bool = False

    @property
    def drift_features(self) -> list[FeatureDriftResult]:
        return [f for f in self.feature_results if f.status == "DRIFT"]

    @property
    def shift_features(self) -> list[FeatureDriftResult]:
        return [f for f in self.feature_results if f.status == "SHIFT"]

    @property
    def overall_status(self) -> str:
        if len(self.drift_features) > 0:
            return "DRIFT"
        if len(self.shift_features) > 0:
            return "SHIFT"
        return "STABLE"


@dataclass
class RetrainRecommendation:
    """Retrain check result."""
    recommendation: str  # RETRAIN_RECOMMENDED, MONITORING, STABLE
    reasons: list[str] = field(default_factory=list)
    new_data_count: int = 0
    days_since_training: float | None = None
    drift_status: str = "UNKNOWN"
    rolling_auc: float | None = None
    cv_auc: float | None = None


def compute_psi(
    reference_bins: list[float],
    reference_counts: list[int],
    actual_values: list[float],
    epsilon: float = 1e-4,
) -> float:
    """Compute Population Stability Index.

    Args:
        reference_bins: Bin edges from training (N+1 edges for N bins).
        reference_counts: Count in each bin during training.
        actual_values: New data values to compare.
        epsilon: Small value to avoid log(0).

    Returns:
        PSI value.
    """
    import numpy as np

    if len(actual_values) == 0:
        return 0.0

    # Bin the actual values using reference edges
    actual_counts = np.histogram(actual_values, bins=reference_bins)[0]

    # Convert to proportions
    ref_total = sum(reference_counts)
    act_total = len(actual_values)

    if ref_total == 0 or act_total == 0:
        return 0.0

    psi = 0.0
    for ref_c, act_c in zip(reference_counts, actual_counts):
        ref_pct = max(ref_c / ref_total, epsilon)
        act_pct = max(act_c / act_total, epsilon)
        psi += (act_pct - ref_pct) * math.log(act_pct / ref_pct)

    return psi


def classify_psi(psi: float) -> str:
    """Classify PSI into STABLE/SHIFT/DRIFT."""
    if psi >= PSI_DRIFT:
        return "DRIFT"
    if psi >= PSI_SHIFT:
        return "SHIFT"
    return "STABLE"


def compute_feature_drift(
    feature_name: str,
    reference: dict,
    actual_values: list[float] | None,
) -> FeatureDriftResult:
    """Compute drift for a single feature.

    Args:
        feature_name: Feature name.
        reference: Dict with 'bins' and 'counts' from training.
        actual_values: Current values (None if entirely missing).
    """
    if actual_values is None or len(actual_values) == 0:
        return FeatureDriftResult(name=feature_name, psi=None, status="MISSING")

    bins = reference.get("bins", [])
    counts = reference.get("counts", [])

    if len(bins) < 2 or len(counts) == 0:
        return FeatureDriftResult(name=feature_name, psi=None, status="MISSING")

    psi = compute_psi(bins, counts, actual_values)
    status = classify_psi(psi)
    return FeatureDriftResult(name=feature_name, psi=psi, status=status)


def load_reference_distribution(model_dir: str | Path) -> dict | None:
    """Load reference_distribution.json from a model directory."""
    path = Path(model_dir) / "reference_distribution.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def compute_retrain_recommendation(
    drift_report: DriftReport,
    cv_auc: float | None = None,
    days_since_training: float | None = None,
    new_data_count: int = 0,
) -> RetrainRecommendation:
    """Decide whether retraining is recommended."""
    rec = RetrainRecommendation(
        recommendation="STABLE",
        drift_status=drift_report.overall_status,
        rolling_auc=drift_report.rolling_auc,
        cv_auc=cv_auc,
        new_data_count=new_data_count,
        days_since_training=days_since_training,
    )

    # Check drift
    if drift_report.overall_status == "DRIFT":
        rec.reasons.append(f"Feature drift detected ({len(drift_report.drift_features)} features)")
        rec.recommendation = "RETRAIN_RECOMMENDED"

    # Check AUC degradation
    if (
        cv_auc is not None
        and drift_report.rolling_auc is not None
        and drift_report.rolling_auc < cv_auc * AUC_ALERT_RATIO
    ):
        rec.reasons.append(
            f"Rolling AUC ({drift_report.rolling_auc:.4f}) below "
            f"{AUC_ALERT_RATIO:.0%} of CV AUC ({cv_auc:.4f})"
        )
        rec.recommendation = "RETRAIN_RECOMMENDED"

    # Check data volume
    if new_data_count > 50_000:
        rec.reasons.append(f"Significant new data available ({new_data_count:,} rows)")
        if rec.recommendation != "RETRAIN_RECOMMENDED":
            rec.recommendation = "MONITORING"

    # Check time since training
    if days_since_training is not None and days_since_training > 14:
        rec.reasons.append(f"Model age: {days_since_training:.0f} days")
        if rec.recommendation != "RETRAIN_RECOMMENDED":
            rec.recommendation = "MONITORING"

    if not rec.reasons:
        rec.reasons.append("All metrics within normal range")

    return rec


def format_drift_report(report: DriftReport) -> str:
    """Format drift report as readable text."""
    lines = ["\n=== Drift Monitoring Report ===\n"]

    if report.insufficient_data:
        lines.append("  ⚠ Sample size insufficient — conclusions should be interpreted cautiously\n")

    lines.append(f"  Overall Status: {report.overall_status}")
    lines.append(f"  Sample Count:   {report.sample_count}")

    # Feature PSI table
    lines.append("\n  --- Feature PSI ---\n")
    lines.append(f"  {'Feature':<30s}  {'PSI':>8s}  {'Status':<8s}")
    lines.append(f"  {'─' * 30}  {'─' * 8}  {'─' * 8}")

    for fr in sorted(report.feature_results, key=lambda x: -(x.psi or 0)):
        psi_str = f"{fr.psi:.4f}" if fr.psi is not None else "—"
        lines.append(f"  {fr.name:<30s}  {psi_str:>8s}  {fr.status:<8s}")

    # Probability distribution
    if report.prob_ks_pvalue is not None:
        lines.append("\n  --- Prediction Probability Distribution ---\n")
        lines.append(f"  KS p-value:      {report.prob_ks_pvalue:.4f}")
        if report.prob_mean_shift is not None:
            lines.append(f"  Mean shift:      {report.prob_mean_shift:.4f}")
        if report.prob_var_ratio is not None:
            lines.append(f"  Variance ratio:  {report.prob_var_ratio:.4f}")

    # Rolling accuracy
    if report.rolling_auc is not None:
        lines.append("\n  --- Rolling Accuracy ---\n")
        lines.append(f"  Rolling AUC:     {report.rolling_auc:.4f}")
    if report.rolling_win_rate is not None:
        lines.append(f"  Win Rate:        {report.rolling_win_rate:.2%}")
    if report.calibration_bias is not None:
        lines.append(f"  Calibration Bias:{report.calibration_bias:+.4f}")

    # Summary
    drift_count = len(report.drift_features)
    shift_count = len(report.shift_features)
    lines.append(f"\n  Summary: {drift_count} DRIFT, {shift_count} SHIFT, "
                 f"{len(report.feature_results) - drift_count - shift_count} STABLE")

    return "\n".join(lines)


def format_retrain_recommendation(rec: RetrainRecommendation) -> str:
    """Format retrain recommendation as readable text."""
    lines = ["\n=== Retrain Check ===\n"]
    lines.append(f"  Recommendation: {rec.recommendation}")

    if rec.days_since_training is not None:
        lines.append(f"  Days since training: {rec.days_since_training:.0f}")
    lines.append(f"  New data count: {rec.new_data_count:,}")
    lines.append(f"  Drift status: {rec.drift_status}")

    if rec.cv_auc is not None:
        lines.append(f"  CV AUC: {rec.cv_auc:.4f}")
    if rec.rolling_auc is not None:
        lines.append(f"  Rolling AUC: {rec.rolling_auc:.4f}")

    lines.append("\n  Reasons:")
    for r in rec.reasons:
        lines.append(f"    - {r}")

    return "\n".join(lines)
