"""Tests for experiment tracking commands."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli import main


def _mock_connection(rows, col_names=None):
    """Create a mock DB connection returning given rows."""
    con = MagicMock()
    execute_result = MagicMock()
    execute_result.fetchall.return_value = rows
    execute_result.fetchone = MagicMock(side_effect=rows if rows else [None])
    con.execute.return_value = execute_result
    return con


def test_experiment_list_with_data(capsys):
    """btc experiment list outputs Markdown table with experiments."""
    rows = [
        (
            "run_20260330_120000", datetime(2026, 3, 30, 12, tzinfo=timezone.utc),
            80, 0.6543, 1.234, 0.55, 42.50, "completed", "v1",
        ),
        (
            "run_20260329_100000", datetime(2026, 3, 29, 10, tzinfo=timezone.utc),
            60, 0.6321, 0.987, 0.52, 21.00, "completed", None,
        ),
    ]
    con = _mock_connection(rows)

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "list"])

    assert ret == 0
    out = capsys.readouterr().out
    assert "Experiments" in out
    assert "run_20260330" in out
    assert "0.6543" in out


def test_experiment_list_empty(capsys):
    """btc experiment list with no data outputs message."""
    con = _mock_connection([])

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "list"])

    assert ret == 0
    assert "No experiments" in capsys.readouterr().out


def test_experiment_list_json(capsys):
    """btc experiment list --json outputs valid JSON."""
    rows = [
        (
            "run_test", datetime(2026, 3, 30, tzinfo=timezone.utc),
            50, 0.65, 1.0, 0.5, 10.0, "completed", "tag1",
        ),
    ]
    con = _mock_connection(rows)

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "list", "--json"])

    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["run_id"] == "run_test"


def test_experiment_list_sort_by(capsys):
    """btc experiment list --sort-by bt_sharpe uses correct ORDER BY."""
    con = _mock_connection([])

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "list", "--sort-by", "bt_sharpe"])

    assert ret == 0
    # Verify the query used bt_sharpe
    call_args = con.execute.call_args
    assert "bt_sharpe" in call_args[0][0]


def test_experiment_compare_both_exist(capsys):
    """btc experiment compare outputs metrics diff table."""
    # _METRIC_COLS has 14 entries, plus 7 extra cols = 21 total
    row_data = [
        0.65, 0.02, 0.60, 0.25, 1.2, 0.55, 40.0, -10.0, 1.5, 100,
        0.70, 0.05, 0.03, 0.02,  # metric cols (14)
        80, 1000, "Logloss", "AUC", None, None,  # extra (n_features, n_samples, loss, eval, feature_set, best_params)
        datetime(2026, 3, 30, tzinfo=timezone.utc),  # created_at
    ]
    row_data2 = [
        0.68, 0.01, 0.63, 0.22, 1.5, 0.58, 55.0, -8.0, 1.8, 120,
        0.72, 0.04, 0.02, 0.01,
        85, 1200, "Logloss", "AUC", None, None,
        datetime(2026, 3, 31, tzinfo=timezone.utc),
    ]

    con = MagicMock()
    call_count = [0]

    def mock_execute(query, params=None):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:
            result.fetchone.return_value = tuple(row_data)
        else:
            result.fetchone.return_value = tuple(row_data2)
        return result

    con.execute = mock_execute

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "compare", "run_a", "run_b"])

    assert ret == 0
    out = capsys.readouterr().out
    assert "Comparison" in out
    assert "run_a" in out
    assert "run_b" in out
    assert "CV AUC" in out
    assert "Delta" in out


def test_experiment_compare_missing_run(capsys):
    """btc experiment compare with invalid run_id outputs error."""
    con = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = None
    con.execute.return_value = result

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "compare", "nonexistent", "also_missing"])

    assert ret == 1
    assert "not found" in capsys.readouterr().out


def test_experiment_compare_json(capsys):
    """btc experiment compare --json outputs valid JSON."""
    row_data = [
        0.65, 0.02, 0.60, 0.25, 1.2, 0.55, 40.0, -10.0, 1.5, 100,
        0.70, 0.05, 0.03, 0.02,
        80, 1000, "Logloss", "AUC", None, None,
        datetime(2026, 3, 30, tzinfo=timezone.utc),
    ]

    con = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = tuple(row_data)
    con.execute.return_value = result

    with patch("db.get_connection", return_value=con):
        ret = main(["experiment", "compare", "run_x", "run_y", "--json"])

    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert "run_1" in data
    assert "diff" in data
