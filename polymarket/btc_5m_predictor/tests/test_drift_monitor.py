"""Tests for drift_monitor module."""

import math

from training.drift_monitor import (
    DriftReport,
    FeatureDriftResult,
    RetrainRecommendation,
    classify_psi,
    compute_feature_drift,
    compute_psi,
    compute_retrain_recommendation,
    format_drift_report,
    format_retrain_recommendation,
)


def test_compute_psi_stable():
    """Identical distributions produce PSI ≈ 0."""
    bins = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [25, 25, 25, 25]
    # Feed values that fall equally across bins
    import random
    random.seed(42)
    values = [random.uniform(0, 1) for _ in range(1000)]
    psi = compute_psi(bins, counts, values)
    assert psi < 0.1, f"Expected PSI < 0.1 for similar distribution, got {psi}"


def test_compute_psi_drift():
    """Shifted distribution produces high PSI."""
    bins = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [25, 25, 25, 25]  # uniform reference
    # Actual: all values in last bin (extreme shift)
    values = [0.9] * 100
    psi = compute_psi(bins, counts, values)
    assert psi > 0.2, f"Expected PSI > 0.2 for shifted distribution, got {psi}"


def test_compute_psi_empty():
    """Empty actual values produce PSI = 0."""
    bins = [0.0, 0.5, 1.0]
    counts = [50, 50]
    psi = compute_psi(bins, counts, [])
    assert psi == 0.0


def test_classify_psi_stable():
    assert classify_psi(0.05) == "STABLE"


def test_classify_psi_shift():
    assert classify_psi(0.15) == "SHIFT"


def test_classify_psi_drift():
    assert classify_psi(0.25) == "DRIFT"


def test_feature_drift_missing():
    """Missing values produce MISSING status."""
    ref = {"bins": [0, 0.5, 1.0], "counts": [50, 50]}
    result = compute_feature_drift("test_feat", ref, None)
    assert result.status == "MISSING"
    assert result.psi is None


def test_feature_drift_empty_values():
    """Empty values list produces MISSING status."""
    ref = {"bins": [0, 0.5, 1.0], "counts": [50, 50]}
    result = compute_feature_drift("test_feat", ref, [])
    assert result.status == "MISSING"


def test_feature_drift_stable():
    """Stable feature returns STABLE."""
    import random
    random.seed(42)
    ref = {"bins": [0.0, 0.25, 0.5, 0.75, 1.0], "counts": [250, 250, 250, 250]}
    values = [random.uniform(0, 1) for _ in range(1000)]
    result = compute_feature_drift("test_feat", ref, values)
    assert result.status == "STABLE"
    assert result.psi is not None
    assert result.psi < 0.1


def test_feature_drift_drifted():
    """Shifted feature returns DRIFT."""
    ref = {"bins": [0.0, 0.25, 0.5, 0.75, 1.0], "counts": [250, 250, 250, 250]}
    values = [0.9] * 200  # all in last bin
    result = compute_feature_drift("test_feat", ref, values)
    assert result.status == "DRIFT"
    assert result.psi > 0.2


def test_drift_report_overall_status():
    """DriftReport.overall_status reflects worst case."""
    report = DriftReport(feature_results=[
        FeatureDriftResult("a", 0.05, "STABLE"),
        FeatureDriftResult("b", 0.25, "DRIFT"),
        FeatureDriftResult("c", 0.15, "SHIFT"),
    ])
    assert report.overall_status == "DRIFT"


def test_drift_report_all_stable():
    """All stable features produce STABLE overall."""
    report = DriftReport(feature_results=[
        FeatureDriftResult("a", 0.03, "STABLE"),
        FeatureDriftResult("b", 0.05, "STABLE"),
    ])
    assert report.overall_status == "STABLE"


def test_retrain_recommendation_stable():
    """Normal metrics produce STABLE recommendation."""
    report = DriftReport(
        feature_results=[FeatureDriftResult("a", 0.03, "STABLE")],
        rolling_auc=0.65,
    )
    rec = compute_retrain_recommendation(report, cv_auc=0.66, days_since_training=3, new_data_count=1000)
    assert rec.recommendation == "STABLE"


def test_retrain_recommendation_drift():
    """Feature drift triggers RETRAIN_RECOMMENDED."""
    report = DriftReport(
        feature_results=[FeatureDriftResult("a", 0.30, "DRIFT")],
    )
    rec = compute_retrain_recommendation(report, cv_auc=0.66, days_since_training=3, new_data_count=1000)
    assert rec.recommendation == "RETRAIN_RECOMMENDED"
    assert any("drift" in r.lower() for r in rec.reasons)


def test_retrain_recommendation_auc_degradation():
    """AUC below threshold triggers RETRAIN_RECOMMENDED."""
    report = DriftReport(
        feature_results=[FeatureDriftResult("a", 0.03, "STABLE")],
        rolling_auc=0.60,
    )
    rec = compute_retrain_recommendation(report, cv_auc=0.66, days_since_training=3, new_data_count=1000)
    # 0.60 < 0.66 * 0.95 = 0.627
    assert rec.recommendation == "RETRAIN_RECOMMENDED"
    assert any("AUC" in r for r in rec.reasons)


def test_retrain_recommendation_monitoring():
    """Old model with normal metrics triggers MONITORING."""
    report = DriftReport(
        feature_results=[FeatureDriftResult("a", 0.03, "STABLE")],
        rolling_auc=0.65,
    )
    rec = compute_retrain_recommendation(report, cv_auc=0.66, days_since_training=20, new_data_count=5000)
    assert rec.recommendation == "MONITORING"


def test_format_drift_report():
    """format_drift_report produces readable text."""
    report = DriftReport(
        feature_results=[
            FeatureDriftResult("feat_a", 0.03, "STABLE"),
            FeatureDriftResult("feat_b", 0.25, "DRIFT"),
        ],
        sample_count=500,
        rolling_auc=0.63,
    )
    text = format_drift_report(report)
    assert "Drift Monitoring" in text
    assert "feat_a" in text
    assert "feat_b" in text
    assert "DRIFT" in text


def test_format_retrain_recommendation():
    """format_retrain_recommendation produces readable text."""
    rec = RetrainRecommendation(
        recommendation="RETRAIN_RECOMMENDED",
        reasons=["Feature drift detected"],
        new_data_count=10000,
        days_since_training=5.0,
        drift_status="DRIFT",
    )
    text = format_retrain_recommendation(rec)
    assert "Retrain Check" in text
    assert "RETRAIN_RECOMMENDED" in text
    assert "drift" in text.lower()


def test_format_drift_report_insufficient_data():
    """Insufficient data flag shows warning."""
    report = DriftReport(
        feature_results=[FeatureDriftResult("a", 0.05, "STABLE")],
        sample_count=50,
        insufficient_data=True,
    )
    text = format_drift_report(report)
    assert "insufficient" in text.lower() or "cautiously" in text.lower()
