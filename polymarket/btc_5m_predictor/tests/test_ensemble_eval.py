"""Tests for ensemble evaluation script."""

import numpy as np
import pandas as pd
import pytest

from training.ensemble_eval import ensemble_predictions, evaluate_ensemble


def _make_cv_preds(n: int = 100, seed: int = 42, n_folds: int = 4) -> pd.DataFrame:
    """Create synthetic CV predictions."""
    rng = np.random.RandomState(seed)
    labels = rng.randint(0, 2, n)
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


class TestEnsemblePredictions:
    def test_averages_probabilities(self):
        """Ensemble should average y_prob across runs."""
        preds1 = _make_cv_preds(100, seed=42)
        preds2 = _make_cv_preds(100, seed=42)  # same structure
        preds2["y_prob"] = preds2["y_prob"] + 0.02  # slightly shifted

        # Monkey-patch load to return our synthetic data
        import training.ensemble_eval as mod
        call_count = [0]
        preds_list = [preds1, preds2]

        def mock_load(run_id):
            result = preds_list[call_count[0]]
            call_count[0] += 1
            return result

        original = mod.load_cv_predictions
        mod.load_cv_predictions = mock_load
        try:
            result = ensemble_predictions(["run_a", "run_b"])
        finally:
            mod.load_cv_predictions = original

        assert len(result) == 100
        assert "y_prob" in result.columns
        # Averaged probs should be between the two
        expected_avg = (preds1["y_prob"] + preds2["y_prob"]) / 2
        np.testing.assert_allclose(result["y_prob"].values, expected_avg.values, atol=1e-10)

    def test_preserves_labels_and_folds(self):
        """Ensemble result should keep original labels and fold assignments."""
        preds1 = _make_cv_preds(80, seed=10)
        preds2 = _make_cv_preds(80, seed=10)
        preds2["y_prob"] = preds2["y_prob"] * 0.9

        import training.ensemble_eval as mod
        idx = [0]
        pl = [preds1, preds2]

        def mock_load(run_id):
            r = pl[idx[0]]
            idx[0] += 1
            return r

        original = mod.load_cv_predictions
        mod.load_cv_predictions = mock_load
        try:
            result = ensemble_predictions(["a", "b"])
        finally:
            mod.load_cv_predictions = original

        pd.testing.assert_series_equal(
            result["label"].reset_index(drop=True),
            preds1["label"].reset_index(drop=True),
        )
        pd.testing.assert_series_equal(
            result["fold"].reset_index(drop=True),
            preds1["fold"].reset_index(drop=True),
        )


class TestEvaluateEnsemble:
    def test_returns_expected_structure(self):
        """evaluate_ensemble should return dict with individual and ensemble keys."""
        preds_a = _make_cv_preds(200, seed=42)
        preds_b = _make_cv_preds(200, seed=123)

        import training.ensemble_eval as mod
        idx = [0]
        # evaluate_ensemble calls load 2x for individual + 2x for ensemble
        pl = [preds_a, preds_b, preds_a, preds_b]

        def mock_load(run_id):
            r = pl[idx[0] % len(pl)]
            idx[0] += 1
            return r

        original = mod.load_cv_predictions
        mod.load_cv_predictions = mock_load
        try:
            result = evaluate_ensemble(["run_a", "run_b"])
        finally:
            mod.load_cv_predictions = original

        assert "individual" in result
        assert "ensemble" in result
        assert len(result["individual"]) == 2
        assert result["ensemble"]["n_models"] == 2
        assert 0.5 <= result["ensemble"]["auc"] <= 1.0

    def test_error_on_missing_run(self):
        """Should raise ValueError for non-existent run."""
        import training.ensemble_eval as mod
        original = mod.load_cv_predictions

        def mock_load(run_id):
            raise ValueError(f"No CV predictions found for run_id '{run_id}'")

        mod.load_cv_predictions = mock_load
        try:
            with pytest.raises(ValueError, match="No CV predictions"):
                evaluate_ensemble(["nonexistent_1", "nonexistent_2"])
        finally:
            mod.load_cv_predictions = original

    def test_warns_on_low_auc_seed(self, capsys):
        """Should print warning when a seed has very low AUC."""
        preds_good = _make_cv_preds(200, seed=42)
        preds_bad = _make_cv_preds(200, seed=42)
        preds_bad["y_prob"] = 0.5  # random predictions, AUC ≈ 0.5

        import training.ensemble_eval as mod
        idx = [0]
        pl = [preds_good, preds_bad, preds_good, preds_bad]

        def mock_load(run_id):
            r = pl[idx[0] % len(pl)]
            idx[0] += 1
            return r

        original = mod.load_cv_predictions
        mod.load_cv_predictions = mock_load
        try:
            ensemble_predictions(["good", "bad"])
        finally:
            mod.load_cv_predictions = original

        captured = capsys.readouterr()
        assert "WARNING" in captured.out or "low AUC" in captured.out
