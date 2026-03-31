"""Tests for model_runs table migration and named-column INSERT."""

import json
from datetime import datetime, timezone

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


def _get_columns(db_path, table="model_runs"):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' ORDER BY ordinal_position"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def test_new_columns_exist(fresh_db):
    """After init_db(), model_runs should have the 9 new columns."""
    cols = _get_columns(fresh_db)
    new_cols = [
        "feature_set", "parent_run_id", "tags",
        "loss_function", "eval_metric", "train_auc",
        "overfit_train_cv_gap", "overfit_cv_ho_gap", "cv_fold_std",
    ]
    for col in new_cols:
        assert col in cols, f"Missing column: {col}"


def test_init_db_idempotent(fresh_db, monkeypatch):
    """Running init_db() twice does not raise."""
    init_db()  # second call
    cols = _get_columns(fresh_db)
    assert "feature_set" in cols


def test_insert_basic_run(fresh_db, monkeypatch):
    """Insert a minimal run with only original columns."""
    run = {
        "n_samples": 1000,
        "n_features": 50,
        "cv_mean_auc": 0.65,
        "status": "completed",
    }
    rid = insert_model_run(run, run_id="test_basic")
    assert rid == "test_basic"

    con = duckdb.connect(str(fresh_db), read_only=True)
    try:
        row = con.execute(
            "SELECT run_id, n_samples, cv_mean_auc, feature_set "
            "FROM model_runs WHERE run_id = 'test_basic'"
        ).fetchone()
        assert row is not None
        assert row[0] == "test_basic"
        assert row[1] == 1000
        assert abs(row[2] - 0.65) < 1e-6
        assert row[3] is None  # new column not provided
    finally:
        con.close()


def test_insert_with_new_columns(fresh_db, monkeypatch):
    """Insert a run using new iteration-platform columns."""
    run = {
        "n_samples": 500,
        "cv_mean_auc": 0.70,
        "feature_set": ["rsi_14", "macd_hist"],
        "parent_run_id": "run_parent",
        "tags": "experiment_v2",
        "loss_function": "Logloss",
        "eval_metric": "AUC",
        "train_auc": 0.85,
        "overfit_train_cv_gap": 0.15,
        "overfit_cv_ho_gap": 0.05,
        "cv_fold_std": 0.02,
    }
    rid = insert_model_run(run, run_id="test_new_cols")

    con = duckdb.connect(str(fresh_db), read_only=True)
    try:
        row = con.execute(
            "SELECT feature_set, parent_run_id, tags, loss_function, "
            "eval_metric, train_auc, overfit_train_cv_gap, "
            "overfit_cv_ho_gap, cv_fold_std "
            "FROM model_runs WHERE run_id = 'test_new_cols'"
        ).fetchone()
        assert row is not None
        fs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        assert fs == ["rsi_14", "macd_hist"]
        assert row[1] == "run_parent"
        assert row[2] == "experiment_v2"
        assert row[3] == "Logloss"
        assert row[4] == "AUC"
        assert abs(row[5] - 0.85) < 1e-6
        assert abs(row[6] - 0.15) < 1e-6
        assert abs(row[7] - 0.05) < 1e-6
        assert abs(row[8] - 0.02) < 1e-6
    finally:
        con.close()


def test_insert_with_best_params_dict(fresh_db, monkeypatch):
    """best_params as dict is serialized to JSON string."""
    run = {
        "best_params": {"depth": 6, "lr": 0.03},
        "cv_mean_auc": 0.60,
    }
    rid = insert_model_run(run, run_id="test_params")

    con = duckdb.connect(str(fresh_db), read_only=True)
    try:
        row = con.execute(
            "SELECT best_params FROM model_runs WHERE run_id = 'test_params'"
        ).fetchone()
        params = json.loads(row[0])
        assert params["depth"] == 6
        assert params["lr"] == 0.03
    finally:
        con.close()


def test_auto_generated_run_id(fresh_db, monkeypatch):
    """run_id is auto-generated when not provided."""
    run = {"n_samples": 100}
    rid = insert_model_run(run)
    assert rid.startswith("run_")
    assert len(rid) > 10
