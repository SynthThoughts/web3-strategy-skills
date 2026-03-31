"""Tests for feature CLI commands."""

from unittest.mock import patch, MagicMock
from cli import main


def test_feature_validate_unknown(capsys):
    """Validating an unknown feature returns error."""
    ret = main(["feature", "validate", "nonexistent_xyz"])
    assert ret == 1
    out = capsys.readouterr().out
    assert "not registered" in out


def test_feature_validate_known(capsys):
    """Validating a known feature with mocked stats returns 0."""
    mock_stats = {
        "mean": 0.001,
        "std": 0.05,
        "missing_pct": 2.0,
        "outlier_pct": 1.5,
        "min": -0.1,
        "max": 0.15,
        "median": 0.0005,
        "univariate_auc": 0.53,
    }
    with patch("cli.cmd_feature._compute_feature_stats", return_value=mock_stats):
        ret = main(["feature", "validate", "ret_3"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "ret_3" in out
    assert "GOOD" in out
    assert "0.5300" in out  # AUC display


def test_feature_validate_alert(capsys):
    """Feature with high missing and zero variance triggers ALERT."""
    mock_stats = {
        "mean": 0.0,
        "std": 0.0,
        "missing_pct": 15.0,
        "outlier_pct": 0.0,
        "min": 0.0,
        "max": 0.0,
        "median": 0.0,
        "univariate_auc": None,
    }
    with patch("cli.cmd_feature._compute_feature_stats", return_value=mock_stats):
        ret = main(["feature", "validate", "ret_3"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "ALERT" in out


def test_feature_validate_warning(capsys):
    """Feature with one issue triggers WARNING."""
    mock_stats = {
        "mean": 0.001,
        "std": 0.05,
        "missing_pct": 15.0,  # high missing
        "outlier_pct": 1.0,
        "min": -0.1,
        "max": 0.1,
        "median": 0.0,
        "univariate_auc": 0.55,
    }
    with patch("cli.cmd_feature._compute_feature_stats", return_value=mock_stats):
        ret = main(["feature", "validate", "ret_3"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "WARNING" in out


def test_feature_validate_compute_error(capsys):
    """When stats computation fails, returns 1."""
    with patch("cli.cmd_feature._compute_feature_stats", side_effect=ValueError("No data")):
        ret = main(["feature", "validate", "ret_3"])
    assert ret == 1
    out = capsys.readouterr().out
    assert "Could not compute" in out


def test_feature_explore_all(capsys):
    """Explore all features shows summary."""
    ret = main(["feature", "explore"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Feature Explorer" in out
    assert "Summary" in out
    assert "128 features" in out


def test_feature_explore_category(capsys):
    """Explore by category filters correctly."""
    ret = main(["feature", "explore", "--category", "期货数据"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "10 features" in out
    assert "taker_vol_raw" in out


def test_feature_explore_unknown_category(capsys):
    """Explore with unknown category returns error."""
    ret = main(["feature", "explore", "--category", "nonexistent"])
    assert ret == 1
    out = capsys.readouterr().out
    assert "No features found" in out
    assert "Available categories" in out


def test_feature_metadata_has_source_dep():
    """All FEATURE_META entries have source_dep field."""
    from data.feature_metadata import FEATURE_META
    missing = [k for k, v in FEATURE_META.items() if "source_dep" not in v]
    assert missing == [], f"Missing source_dep: {missing}"


def test_feature_metadata_has_min_days():
    """All FEATURE_META entries have min_days field."""
    from data.feature_metadata import FEATURE_META
    missing = [k for k, v in FEATURE_META.items() if "min_days" not in v]
    assert missing == [], f"Missing min_days: {missing}"


def test_feature_metadata_source_dep_valid():
    """All source_dep values are valid data source names."""
    from data.feature_metadata import FEATURE_META
    valid_sources = {"klines_1m", "klines_30m", "klines_4h", "futures"}
    for name, meta in FEATURE_META.items():
        assert meta["source_dep"] in valid_sources, f"{name}: invalid source_dep '{meta['source_dep']}'"
