"""Tests for leakage_check module."""

import pytest

from training.leakage_check import (
    LeakageError,
    check_purge_gap,
    check_feature_data_coverage,
    check_label_timing,
    run_all_checks,
)


def test_purge_gap_ok():
    """purge_gap >= min does not raise."""
    check_purge_gap(12, min_purge_gap=12)
    check_purge_gap(24, min_purge_gap=12)


def test_purge_gap_too_small():
    """purge_gap < min raises LeakageError."""
    with pytest.raises(LeakageError, match="Purge gap"):
        check_purge_gap(6, min_purge_gap=12)


def test_feature_data_coverage_ok():
    """All features have sufficient data."""
    meta = {
        "rsi_14": {"min_days": 30},
        "macd_hist": {"min_days": 60},
    }
    warnings = check_feature_data_coverage(meta, ["rsi_14", "macd_hist"], actual_days=90)
    assert len(warnings) == 0


def test_feature_data_coverage_warning():
    """Feature with min_days > actual_days but > 50% gives warning."""
    meta = {"slow_feature": {"min_days": 150}}
    warnings = check_feature_data_coverage(meta, ["slow_feature"], actual_days=100)
    assert len(warnings) == 1
    assert "slow_feature" in warnings[0]


def test_feature_data_coverage_error():
    """Feature with min_days >> actual_days raises LeakageError."""
    meta = {"very_slow": {"min_days": 300}}
    with pytest.raises(LeakageError, match="Insufficient data"):
        check_feature_data_coverage(meta, ["very_slow"], actual_days=100)


def test_feature_without_min_days():
    """Features without min_days are silently OK."""
    meta = {"basic_feat": {"category": "ta"}}
    warnings = check_feature_data_coverage(meta, ["basic_feat"], actual_days=30)
    assert len(warnings) == 0


def test_label_timing_ok():
    """Default label timing passes."""
    check_label_timing()


def test_label_timing_bad():
    """Non-close-based label raises."""
    with pytest.raises(LeakageError, match="close price"):
        check_label_timing(label_uses_close=False)


def test_run_all_checks_ok():
    """All checks pass together."""
    warnings = run_all_checks(purge_gap=12, feature_cols=["a", "b"], actual_days=100)
    assert isinstance(warnings, list)


def test_run_all_checks_purge_fail():
    """run_all_checks raises on purge gap failure."""
    with pytest.raises(LeakageError):
        run_all_checks(purge_gap=5, feature_cols=[], actual_days=100)
