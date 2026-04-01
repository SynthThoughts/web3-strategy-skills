"""Tests for calibration evaluation script."""

import numpy as np
import pandas as pd
import pytest

from training.calibration_eval import (
    calibrate_cv_predictions,
    evaluate_calibration,
)


def _make_cv_preds(n: int = 200, seed: int = 42, n_folds: int = 4) -> pd.DataFrame:
    """Create synthetic CV predictions with controllable signal."""
    rng = np.random.RandomState(seed)
    labels = rng.randint(0, 2, n)
    # Predictions with some signal + noise
    probs = np.clip(labels * 0.3 + rng.normal(0.5, 0.15, n), 0.01, 0.99)
    folds = np.tile(np.arange(1, n_folds + 1), n // n_folds + 1)[:n]
    return pd.DataFrame({
        "window_start": pd.date_range("2026-03-01", periods=n, freq="5min"),
        "fold": folds,
        "label": labels,
        "y_prob": probs,
        "open_price": 85000.0 + rng.normal(0, 100, n),
        "close_price": 85000.0 + rng.normal(0, 100, n),
    })


class TestCalibrateCvPredictions:
    def test_platt_returns_calibrated_probs(self):
        """Platt scaling should return calibrated probabilities in [0, 1]."""
        preds = _make_cv_preds(400, seed=42)
        result = calibrate_cv_predictions(preds, method="sigmoid")

        assert result is not None
        assert len(result) == len(preds)
        assert result["y_prob"].min() >= 0
        assert result["y_prob"].max() <= 1

    def test_isotonic_returns_calibrated_probs(self):
        """Isotonic regression should return calibrated probabilities."""
        preds = _make_cv_preds(800, seed=42)  # needs more samples
        result = calibrate_cv_predictions(preds, method="isotonic", min_samples=150)

        assert result is not None
        assert len(result) == len(preds)
        assert result["y_prob"].min() >= 0
        assert result["y_prob"].max() <= 1

    def test_isotonic_skips_on_small_folds(self):
        """Isotonic should return None when calibration training set is too small."""
        preds = _make_cv_preds(100, seed=42)  # 100 / 4 folds = 25 per fold
        # With 4 folds, train set = 75 samples, below default 500
        result = calibrate_cv_predictions(preds, method="isotonic", min_samples=500)
        assert result is None

    def test_preserves_labels(self):
        """Calibrated predictions should keep original labels."""
        preds = _make_cv_preds(400, seed=42)
        result = calibrate_cv_predictions(preds, method="sigmoid")

        assert result is not None
        # Labels should be identical (may be reordered)
        result_sorted = result.sort_values("window_start").reset_index(drop=True)
        preds_sorted = preds.sort_values("window_start").reset_index(drop=True)
        pd.testing.assert_series_equal(
            result_sorted["label"], preds_sorted["label"]
        )

    def test_single_fold_returns_none(self):
        """Should return None with only 1 fold."""
        preds = _make_cv_preds(100, seed=42)
        preds["fold"] = 1
        result = calibrate_cv_predictions(preds, method="sigmoid")
        assert result is None

    def test_calibrated_auc_close_to_original(self):
        """Calibration should not dramatically change AUC."""
        from sklearn.metrics import roc_auc_score

        preds = _make_cv_preds(400, seed=42)
        original_auc = roc_auc_score(preds["label"], preds["y_prob"])

        result = calibrate_cv_predictions(preds, method="sigmoid")
        assert result is not None

        result_sorted = result.sort_values("window_start").reset_index(drop=True)
        cal_auc = roc_auc_score(result_sorted["label"], result_sorted["y_prob"])

        # AUC should be close (Platt is monotonic transform, preserves AUC mostly)
        assert abs(cal_auc - original_auc) < 0.05


class TestEvaluateCalibration:
    def test_returns_expected_structure(self):
        """evaluate_calibration should return dict with baseline and calibrations."""
        preds = _make_cv_preds(400, seed=42)

        import training.calibration_eval as mod
        original = mod.load_cv_predictions
        mod.load_cv_predictions = lambda run_id: preds
        try:
            result = evaluate_calibration("test_run", methods=["sigmoid"])
        finally:
            mod.load_cv_predictions = original

        assert "run_id" in result
        assert "baseline" in result
        assert "calibrations" in result
        assert "platt" in result["calibrations"]
        assert result["baseline"]["auc"] > 0.5

    def test_error_on_missing_run(self):
        """Should raise ValueError for non-existent run."""
        import training.calibration_eval as mod
        original = mod.load_cv_predictions

        def mock_load(run_id):
            raise ValueError(f"No CV predictions found for run_id '{run_id}'")

        mod.load_cv_predictions = mock_load
        try:
            with pytest.raises(ValueError, match="No CV predictions"):
                evaluate_calibration("nonexistent")
        finally:
            mod.load_cv_predictions = original

    def test_isotonic_skipped_status(self):
        """Small sample isotonic should have 'skipped' status."""
        preds = _make_cv_preds(100, seed=42)

        import training.calibration_eval as mod
        original = mod.load_cv_predictions
        mod.load_cv_predictions = lambda run_id: preds
        try:
            result = evaluate_calibration("test_run", methods=["isotonic"])
        finally:
            mod.load_cv_predictions = original

        assert result["calibrations"]["isotonic"]["status"] == "skipped"
