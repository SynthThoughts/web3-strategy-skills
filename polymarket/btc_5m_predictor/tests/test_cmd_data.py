"""Tests for data management commands."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from cli import main


def test_data_status_output(capsys):
    """btc data status outputs coverage table with all data sources."""
    mock_coverage = {
        "klines_1m": {
            "count": 100000,
            "min_time": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "max_time": datetime(2026, 3, 30, tzinfo=timezone.utc),
        },
        "futures_funding_rate": {
            "count": 5000,
            "min_time": datetime(2024, 6, 1, tzinfo=timezone.utc),
            "max_time": datetime(2026, 3, 30, tzinfo=timezone.utc),
        },
        "empty_table": {
            "count": 0,
            "min_time": None,
            "max_time": None,
        },
    }
    with patch("db.get_data_coverage", return_value=mock_coverage):
        ret = main(["data", "status"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Data Coverage" in out
    assert "klines_1m" in out
    assert "100,000" in out
    assert "empty_table" in out


def test_data_status_empty(capsys):
    """btc data status handles no tables gracefully."""
    with patch("db.get_data_coverage", return_value={}):
        ret = main(["data", "status"])
    assert ret == 0
    assert "No data tables" in capsys.readouterr().out


def test_data_fetch_valid_source(capsys):
    """btc data fetch --source futures --days 7 calls correct function."""
    mock_mod = MagicMock()
    mock_mod.fetch_all_incremental.return_value = 42

    with patch("importlib.import_module", return_value=mock_mod) as mock_import:
        ret = main(["data", "fetch", "--source", "futures", "--days", "7"])

    assert ret == 0
    mock_import.assert_called_with("data.fetch_futures_data")
    mock_mod.fetch_all_incremental.assert_called_once_with(days=7)
    assert "42 rows" in capsys.readouterr().out


def test_data_fetch_unknown_source(capsys):
    """btc data fetch with unknown source outputs error and available list."""
    ret = main(["data", "fetch", "--source", "unknown_source"])
    assert ret == 1
    out = capsys.readouterr().out
    assert "unknown source" in out.lower() or "Error" in out
    assert "klines_1m" in out  # suggests available sources


def test_data_fetch_all(capsys):
    """btc data fetch --source all calls all fetch functions."""
    mock_mod = MagicMock()
    mock_mod.fetch_incremental.return_value = 10
    mock_mod.fetch_incremental_30m.return_value = 5
    mock_mod.fetch_incremental_4h.return_value = 3
    mock_mod.fetch_all_incremental.return_value = 20

    with patch("importlib.import_module", return_value=mock_mod):
        ret = main(["data", "fetch", "--source", "all"])

    assert ret == 0
    out = capsys.readouterr().out
    assert "All" in out
    assert "sources fetched" in out


def test_data_fetch_error(capsys):
    """btc data fetch handles fetch function errors gracefully."""
    mock_mod = MagicMock()
    mock_mod.fetch_incremental.side_effect = ConnectionError("API rate limited")

    with patch("importlib.import_module", return_value=mock_mod):
        ret = main(["data", "fetch", "--source", "klines_1m"])

    assert ret == 1
    assert "Error" in capsys.readouterr().out


def test_data_sync_incremental(capsys):
    """btc data sync calls pull_all without full flag."""
    with patch("service.sync_data.pull_all", return_value={"klines_1m": 50, "futures_funding_rate": 10}) as mock_pull:
        ret = main(["data", "sync"])

    assert ret == 0
    mock_pull.assert_called_once_with(full=False)
    out = capsys.readouterr().out
    assert "60 total rows" in out


def test_data_sync_full(capsys):
    """btc data sync --full passes full=True."""
    with patch("service.sync_data.pull_all", return_value={"klines_1m": 100}) as mock_pull:
        ret = main(["data", "sync", "--full"])

    assert ret == 0
    mock_pull.assert_called_once_with(full=True)


def test_data_sync_error(capsys):
    """btc data sync handles connection errors gracefully."""
    with patch("service.sync_data.pull_all", side_effect=ConnectionError("SSH failed")):
        ret = main(["data", "sync"])

    assert ret == 1
    assert "Error" in capsys.readouterr().out


def test_data_health_vps_unreachable(capsys):
    """btc data health handles VPS unreachable without crashing."""
    with patch("subprocess.run", side_effect=FileNotFoundError("ssh not found")):
        ret = main(["data", "health"])

    assert ret == 1
    out = capsys.readouterr().out
    assert "Error" in out or "ssh" in out


def test_data_validate_passes(capsys):
    """btc data validate outputs PASS for clean data."""
    mock_con = MagicMock()

    # Gaps query returns empty dataframe (no gaps)
    empty_gaps = pd.DataFrame({"gap_min": []})
    mock_execute = MagicMock()

    call_count = [0]

    def side_effect(sql):
        call_count[0] += 1
        result = MagicMock()
        if "LEAD" in sql:
            result.fetchdf.return_value = empty_gaps
        elif "MIN(close)" in sql:
            result.fetchone.return_value = (10000.0, 100000.0, 0, 0)
        elif "DATE_TRUNC" in sql:
            result.fetchone.return_value = (1000, 1000, 990)
        return result

    mock_con.execute = side_effect

    with patch("db.get_connection", return_value=mock_con):
        ret = main(["data", "validate"])

    assert ret == 0
    out = capsys.readouterr().out
    assert "PASS" in out
