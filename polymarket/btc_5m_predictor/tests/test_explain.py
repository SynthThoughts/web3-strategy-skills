"""Tests for explain module."""

import pandas as pd
import numpy as np

from training.explain import (
    DiagnoseResult,
    ShapResult,
    SliceResult,
    compute_diagnose,
    compute_market_slices,
    format_diagnose_report,
    format_shap_report,
    format_slice_report,
)


def test_format_shap_report():
    """format_shap_report produces readable text."""
    result = ShapResult(
        feature_importances=[("feat_a", 0.15), ("feat_b", 0.10), ("feat_c", 0.05)],
        top_dependencies=[{
            "feature": "feat_a",
            "importance": 0.15,
            "low_range_shap": -0.05,
            "mid_range_shap": 0.01,
            "high_range_shap": 0.08,
        }],
    )
    text = format_shap_report(result)
    assert "SHAP" in text
    assert "feat_a" in text
    assert "feat_b" in text


def test_format_slice_report_empty():
    """Empty slice report shows message."""
    result = SliceResult(slices=[])
    text = format_slice_report(result)
    assert "No slice data" in text


def test_format_slice_report_with_data():
    """Slice report with data shows table."""
    result = SliceResult(slices=[
        {"name": "High Vol + Trend", "sample_count": 100, "win_rate": 0.55, "auc": 0.62},
        {"name": "Low Vol + Range", "sample_count": 150, "win_rate": 0.48, "auc": None},
    ])
    text = format_slice_report(result)
    assert "High Vol + Trend" in text
    assert "Low Vol + Range" in text


def test_compute_market_slices():
    """Market slices with proper data returns 4 slices."""
    np.random.seed(42)
    n = 200
    df = pd.DataFrame({
        "atr_ratio_5_20": np.random.uniform(0.5, 1.5, n),
        "adx_14": np.random.uniform(10, 40, n),
        "label": np.random.choice([0, 1], n),
    })
    result = compute_market_slices(df)
    assert len(result.slices) == 4


def test_compute_market_slices_missing_cols():
    """Missing required columns returns empty slices."""
    df = pd.DataFrame({"other": [1, 2, 3]})
    result = compute_market_slices(df)
    assert len(result.slices) == 0


def test_compute_diagnose_insufficient_data():
    """Less than 50 rows returns insufficient data."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=30, freq="5min"),
        "correct": [True] * 30,
    })
    result = compute_diagnose(df)
    assert result.insufficient_data is True


def test_compute_diagnose_none_data():
    """None data returns insufficient data."""
    result = compute_diagnose(None)
    assert result.insufficient_data is True


def test_compute_diagnose_normal():
    """Normal data with time attribution."""
    np.random.seed(42)
    n = 200
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "correct": np.random.choice([True, False], n, p=[0.6, 0.4]),
        "pred_prob": np.random.uniform(0.3, 0.7, n),
    })
    result = compute_diagnose(df)
    assert result.insufficient_data is False
    assert len(result.time_attribution) > 0
    # Time attribution should be sorted by accuracy ascending
    accs = [ta["accuracy"] for ta in result.time_attribution]
    assert accs == sorted(accs)


def test_compute_diagnose_all_correct():
    """All correct predictions — no anomalies or minimal errors."""
    n = 100
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "correct": [True] * n,
        "pred_prob": [0.55] * n,
    })
    result = compute_diagnose(df)
    assert result.insufficient_data is False
    # All correct → error_analysis should be empty
    assert len(result.error_analysis) == 0


def test_format_diagnose_report_insufficient():
    """Insufficient data shows appropriate message."""
    result = DiagnoseResult(insufficient_data=True)
    text = format_diagnose_report(result)
    assert "Insufficient" in text


def test_format_diagnose_report_no_anomalies():
    """No anomalies shows appropriate message."""
    result = DiagnoseResult(no_anomalies=True)
    text = format_diagnose_report(result)
    assert "No significant" in text


def test_format_diagnose_report_with_data():
    """Diagnose report with data shows three layers."""
    result = DiagnoseResult(
        time_attribution=[
            {"period": "00:00-04:00", "sample_count": 50, "accuracy": 0.45},
            {"period": "04:00-08:00", "sample_count": 60, "accuracy": 0.55},
        ],
        feature_attribution=[
            {"feature": "feat_x", "psi": 0.35, "status": "DRIFT"},
        ],
        error_analysis=[
            {"timestamp": "2026-01-01 02:15", "top_deviations": [("feat_x", 3.2)]},
        ],
    )
    text = format_diagnose_report(result)
    assert "Layer 1" in text
    assert "Layer 2" in text
    assert "Layer 3" in text
    assert "feat_x" in text
