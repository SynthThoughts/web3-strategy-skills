"""Tests for ho_auc persistence in model_runs table."""

from __future__ import annotations

import duckdb
import pytest

from db import init_db, insert_model_run, get_connection


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Use a temporary DuckDB file for each test."""
    db_path = tmp_path / "test.duckdb"
    monkeypatch.setattr("db.DB_PATH", db_path)
    monkeypatch.setattr("db.PARQUET_FILE", tmp_path / "nonexistent.parquet")
    init_db()
    return db_path


def _query_ho_auc(db_path: str, run_id: str):
    """Helper to fetch ho_auc for a given run_id."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(
            "SELECT ho_auc FROM model_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        return row
    finally:
        con.close()


class TestHoAucColumn:
    """Tests for ho_auc column in model_runs."""

    def test_column_exists_after_init(self, fresh_db):
        """ho_auc column should exist in model_runs after init_db()."""
        con = duckdb.connect(str(fresh_db), read_only=True)
        try:
            cols = con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'model_runs' ORDER BY ordinal_position"
            ).fetchall()
            col_names = [r[0] for r in cols]
            assert "ho_auc" in col_names
        finally:
            con.close()

    def test_insert_with_ho_auc(self, fresh_db):
        """Insert a run with ho_auc set, verify it persists correctly."""
        run = {
            "n_samples": 1000,
            "cv_mean_auc": 0.68,
            "ho_auc": 0.6542,
            "status": "completed",
        }
        rid = insert_model_run(run, run_id="test_ho_auc")
        row = _query_ho_auc(fresh_db, "test_ho_auc")
        assert row is not None
        ho_auc = row[0]
        assert ho_auc is not None
        assert 0.5 <= ho_auc <= 1.0
        assert abs(ho_auc - 0.6542) < 1e-6

    def test_insert_without_ho_auc_is_null(self, fresh_db):
        """Insert a run WITHOUT ho_auc (old-style), verify it's NULL."""
        run = {
            "n_samples": 500,
            "cv_mean_auc": 0.60,
            "status": "completed",
        }
        insert_model_run(run, run_id="test_no_ho_auc")
        row = _query_ho_auc(fresh_db, "test_no_ho_auc")
        assert row is not None
        assert row[0] is None

    def test_alter_table_preserves_existing_rows(self, fresh_db, monkeypatch):
        """ALTER TABLE on existing table with data doesn't lose rows."""
        # Insert a row before re-running init_db
        run = {
            "n_samples": 200,
            "cv_mean_auc": 0.55,
            "ho_auc": 0.53,
        }
        insert_model_run(run, run_id="test_before_alter")

        # Re-run init_db (simulates upgrade path)
        init_db()

        # Verify old row still exists
        con = duckdb.connect(str(fresh_db), read_only=True)
        try:
            row = con.execute(
                "SELECT run_id, n_samples, ho_auc FROM model_runs "
                "WHERE run_id = 'test_before_alter'"
            ).fetchone()
            assert row is not None
            assert row[0] == "test_before_alter"
            assert row[1] == 200
            assert abs(row[2] - 0.53) < 1e-6
        finally:
            con.close()


class TestHoAucInMetricCols:
    """Tests for ho_auc in experiment compare metrics."""

    def test_metric_cols_contains_ho_auc(self):
        """_METRIC_COLS should include ho_auc."""
        from cli.cmd_experiment import _METRIC_COLS

        col_names = [c[0] for c in _METRIC_COLS]
        assert "ho_auc" in col_names

    def test_ho_auc_format_spec(self):
        """ho_auc should use .4f format."""
        from cli.cmd_experiment import _METRIC_COLS

        for col, label, fmt in _METRIC_COLS:
            if col == "ho_auc":
                assert label == "HO AUC"
                assert fmt == ".4f"
                break
        else:
            pytest.fail("ho_auc not found in _METRIC_COLS")

    def test_ho_auc_position_after_brier_before_sharpe(self):
        """ho_auc should be between cv_mean_brier and bt_sharpe."""
        from cli.cmd_experiment import _METRIC_COLS

        col_names = [c[0] for c in _METRIC_COLS]
        brier_idx = col_names.index("cv_mean_brier")
        ho_idx = col_names.index("ho_auc")
        sharpe_idx = col_names.index("bt_sharpe")
        assert brier_idx < ho_idx < sharpe_idx
