"""Tests for CLI skeleton: parser, subcommands, dispatch."""

import pytest

from cli import main


def test_no_args_prints_help(capsys):
    """btc with no args prints help and returns 0."""
    ret = main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower() or "btc" in out.lower()


def test_unknown_command():
    """btc with unknown command exits with error (argparse SystemExit)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent"])
    assert exc_info.value.code == 2  # argparse error exit code


def test_data_no_action(capsys):
    """btc data with no action prints usage hint."""
    ret = main(["data"])
    assert ret == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_data_status(capsys):
    """btc data status returns 0."""
    from unittest.mock import patch
    with patch("db.get_data_coverage", return_value={}):
        ret = main(["data", "status"])
    assert ret == 0


def test_feature_no_action(capsys):
    """btc feature with no action prints usage hint."""
    ret = main(["feature"])
    assert ret == 1


def test_feature_validate(capsys):
    """btc feature validate dispatches correctly."""
    from unittest.mock import patch
    mock_stats = {
        "mean": 0.0, "std": 0.05, "missing_pct": 1.0, "outlier_pct": 1.0,
        "min": -0.1, "max": 0.1, "median": 0.0, "univariate_auc": 0.55,
    }
    with patch("cli.cmd_feature._compute_feature_stats", return_value=mock_stats):
        ret = main(["feature", "validate", "ret_3"])
    assert ret == 0


def test_train(capsys):
    """btc train dispatches to cmd_train (actual training tested separately)."""
    from unittest.mock import patch, MagicMock
    mock_result = {
        "run_id": "test_run_id",
        "version": "v999",
        "cv_auc": 0.65,
        "ho_auc": 0.63,
        "bt_sharpe": 1.2,
        "n_features": 10,
        "overfit_report": None,
        "model_path": "/tmp/test",
    }
    with patch("training.train_pipeline.main", return_value=mock_result) as m:
        ret = main(["train"])
    assert ret == 0
    m.assert_called_once()


def test_experiment_no_action(capsys):
    """btc experiment with no action prints usage hint."""
    ret = main(["experiment"])
    assert ret == 1


def test_experiment_list(capsys):
    """btc experiment list returns 0 (tested in detail in test_cmd_experiment.py)."""
    from unittest.mock import MagicMock, patch
    con = MagicMock()
    con.execute.return_value.fetchall.return_value = []
    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "list"])
    assert ret == 0


def test_experiment_compare(capsys):
    """btc experiment compare dispatches (tested in detail in test_cmd_experiment.py)."""
    from unittest.mock import MagicMock, patch
    con = MagicMock()
    con.execute.return_value.fetchone.return_value = None
    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "compare", "run_a", "run_b"])
    assert ret == 1  # not found


def test_deploy_no_action(capsys):
    """btc deploy with no action prints usage hint."""
    ret = main(["deploy"])
    assert ret == 1


def test_deploy_promote(capsys):
    """btc deploy promote returns 0 (stub)."""
    ret = main(["deploy", "promote", "run_123"])
    assert ret == 0


def test_monitor_no_action(capsys):
    """btc monitor with no action prints usage hint."""
    ret = main(["monitor"])
    assert ret == 1


def test_monitor_drift(capsys):
    """btc monitor drift dispatches (returns 1 when no model runs)."""
    from unittest.mock import MagicMock, patch
    con = MagicMock()
    con.execute.return_value.fetchone.return_value = None
    with patch("db.get_connection", return_value=con):
        ret = main(["monitor", "drift"])
    assert ret == 1  # no model runs found


def test_all_subcommands_dispatch():
    """Every top-level subcommand dispatches without import errors."""
    from unittest.mock import MagicMock, patch

    # Commands that don't hit DB
    simple_commands = [
        # feature validate needs mock — tested in test_cmd_feature.py
        ["deploy", "promote", "x"],
        # monitor drift now hits DB — tested below with mocks
    ]
    for argv in simple_commands:
        ret = main(argv)
        assert ret == 0, f"Command {argv} returned {ret}"

    # Commands that hit DB — mock at the right level
    con = MagicMock()
    con.execute.return_value.fetchall.return_value = []
    con.execute.return_value.fetchone.return_value = None
    mock_result = {
        "run_id": "test", "version": "v1", "cv_auc": 0.65,
        "ho_auc": 0.63, "bt_sharpe": 1.0, "n_features": 5,
        "overfit_report": None, "model_path": "/tmp/t",
    }
    mock_feat_stats = {
        "mean": 0.0, "std": 0.05, "missing_pct": 1.0, "outlier_pct": 1.0,
        "min": -0.1, "max": 0.1, "median": 0.0, "univariate_auc": 0.55,
    }
    with patch("db.get_data_coverage", return_value={}), \
         patch("db.get_connection", return_value=con), \
         patch("training.train_pipeline.main", return_value=mock_result), \
         patch("cli.cmd_feature._compute_feature_stats", return_value=mock_feat_stats):
        ret = main(["data", "status"])
        assert ret == 0
        ret = main(["feature", "validate", "ret_3"])
        assert ret == 0
        ret = main(["train"])
        assert ret == 0
        ret = main(["experiment", "list"])
        assert ret == 0
        # monitor drift/retrain-check return 1 (no model runs) with mock
        ret = main(["monitor", "drift"])
        assert ret == 1
        ret = main(["monitor", "retrain-check"])
        assert ret == 1
