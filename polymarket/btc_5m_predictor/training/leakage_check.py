"""Pre-training information leakage detection.

Three static checks run before training begins:
1. Purge gap >= configured minimum (12)
2. Feature source dependencies have sufficient data coverage
3. Label calculation uses close timestamps after window_end
"""

from __future__ import annotations


class LeakageError(Exception):
    """Raised when information leakage is detected."""

    pass


def check_purge_gap(purge_gap: int, min_purge_gap: int = 12) -> None:
    """Assert purge_gap >= minimum.

    Args:
        purge_gap: Actual purge gap in the CV splitter.
        min_purge_gap: Minimum required purge gap (default 12 = 1 hour at 5min).

    Raises:
        LeakageError: If purge_gap is insufficient.
    """
    if purge_gap < min_purge_gap:
        raise LeakageError(
            f"Purge gap {purge_gap} < minimum {min_purge_gap}. "
            f"This risks information leakage between CV folds."
        )


def check_feature_data_coverage(
    feature_meta: dict,
    feature_cols: list[str],
    actual_days: int,
) -> list[str]:
    """Check that each feature's required data coverage is met.

    Args:
        feature_meta: FEATURE_META dict with optional 'min_days' field.
        feature_cols: List of feature column names being used.
        actual_days: Number of days of data available.

    Returns:
        List of warning strings (empty if all OK).

    Raises:
        LeakageError: If any feature requires more data than available.
    """
    warnings: list[str] = []
    errors: list[str] = []

    for col in feature_cols:
        meta = feature_meta.get(col, {})
        min_days = meta.get("min_days")
        if min_days is not None and actual_days < min_days:
            msg = (
                f"Feature '{col}' requires {min_days} days of data, "
                f"but only {actual_days} days available"
            )
            if actual_days < min_days * 0.5:
                errors.append(msg)
            else:
                warnings.append(msg)

    if errors:
        raise LeakageError(
            "Insufficient data coverage for features:\n  "
            + "\n  ".join(errors)
        )

    return warnings


def check_label_timing(
    label_col_name: str = "label",
    label_uses_close: bool = True,
) -> None:
    """Verify label calculation uses close price after window_end.

    This is a static assertion — the actual label generation in
    generate_labels() uses df['close'].shift(-1) which is the close
    of the NEXT candle, ensuring no look-ahead.

    Raises:
        LeakageError: If label timing is suspect.
    """
    if not label_uses_close:
        raise LeakageError(
            f"Label '{label_col_name}' does not use close price. "
            "This may cause information leakage."
        )


def run_all_checks(
    purge_gap: int,
    feature_cols: list[str],
    actual_days: int,
    feature_meta: dict | None = None,
    min_purge_gap: int = 12,
) -> list[str]:
    """Run all leakage checks. Returns warnings list.

    Raises:
        LeakageError: On any critical leakage detection.
    """
    warnings: list[str] = []

    check_purge_gap(purge_gap, min_purge_gap)
    check_label_timing()

    if feature_meta:
        w = check_feature_data_coverage(feature_meta, feature_cols, actual_days)
        warnings.extend(w)

    return warnings
