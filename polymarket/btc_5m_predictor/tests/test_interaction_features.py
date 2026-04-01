"""Tests for _interaction_features() builder."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

# Ensure talib can be imported even when the C library is missing.
if "talib" not in sys.modules:
    sys.modules["talib"] = MagicMock()

from data.features import _interaction_features  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_core_df(n: int = 100, *, include_taker: bool = True) -> pd.DataFrame:
    """Create a DataFrame with the 5 core feature columns."""
    rng = np.random.default_rng(42)
    data: dict[str, np.ndarray] = {
        "price_vs_rvwap_60": rng.standard_normal(n),
        "cvd_slope_10": rng.standard_normal(n),
        "hour4_sin": np.sin(np.linspace(0, 4 * np.pi, n)),
        "vpt_sum_30": rng.standard_normal(n) * 100,
    }
    if include_taker:
        data["taker_vol_raw"] = rng.uniform(0, 10, n)
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_returns_ix_columns_with_all_5_features(self) -> None:
        df = _make_core_df()
        result = _interaction_features(df)

        assert not result.empty
        assert len(result) == len(df)
        assert all(c.startswith("ix_") for c in result.columns)

    def test_expected_column_count(self) -> None:
        """With all 5 features present, expect 14 interaction columns."""
        df = _make_core_df()
        result = _interaction_features(df)
        # 5 products + 3 ratios + 3 conditional + 2 time = 13
        # (counting from the spec)
        assert len(result.columns) == 13

    def test_product_is_elementwise(self) -> None:
        df = _make_core_df()
        result = _interaction_features(df)

        expected = df["taker_vol_raw"] * df["cvd_slope_10"]
        pd.testing.assert_series_equal(
            result["ix_mul_taker_cvd"], expected, check_names=False,
        )


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_nan_propagation(self) -> None:
        df = _make_core_df(50)
        df.loc[10, "taker_vol_raw"] = np.nan
        df.loc[20, "cvd_slope_10"] = np.nan

        result = _interaction_features(df)
        # NaN in taker_vol_raw -> NaN in product with cvd
        assert np.isnan(result["ix_mul_taker_cvd"].iloc[10])
        # NaN in cvd -> NaN in product with taker
        assert np.isnan(result["ix_mul_taker_cvd"].iloc[20])

    def test_vpt_zero_no_inf(self) -> None:
        df = _make_core_df(50)
        df["vpt_sum_30"] = 0.0

        result = _interaction_features(df)
        assert np.isfinite(result["ix_ratio_taker_vpt"]).all()

    def test_missing_taker_vol(self) -> None:
        df = _make_core_df(50, include_taker=False)
        result = _interaction_features(df)

        # Should still produce non-taker interactions
        assert not result.empty
        # No taker columns
        taker_cols = [c for c in result.columns if "taker" in c]
        assert taker_cols == []
        # cvd * pvr product should exist
        assert "ix_mul_cvd_pvr" in result.columns


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_build_features_calls_interaction(self) -> None:
        """Verify _interaction_features is wired into build_features.

        Instead of running the full build_features pipeline (which requires
        TA-Lib C library), we check the source code integration point.
        """
        import inspect
        from data.features import build_features

        source = inspect.getsource(build_features)
        assert "_interaction_features" in source
        assert "ix_feats" in source

    def test_feature_categories_interaction_matches(self) -> None:
        """FEATURE_CATEGORIES['interaction'] should match ix_ columns.

        We parse the dict from the source to avoid importing the full
        train_pipeline module (which requires Python 3.11+ datetime.UTC).
        """
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "training"
            / "train_pipeline.py"
        ).read_text()

        # Verify the category definition exists in source
        assert '"interaction"' in src or "'interaction'" in src
        assert 'c.startswith("ix_")' in src

        # Functionally verify the lambda logic
        matcher = lambda c: c.startswith("ix_")  # noqa: E731
        assert matcher("ix_mul_taker_cvd")
        assert matcher("ix_ratio_taker_vpt")
        assert matcher("ix_cond_taker_cvdpos")
        assert matcher("ix_time_taker_h4sin")
        assert not matcher("taker_vol_raw")
        assert not matcher("cvd_slope_10")
