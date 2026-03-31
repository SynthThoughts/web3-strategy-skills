"""Post-training overfitting analysis.

Computes gaps between Train/CV/Holdout AUC and fold variance,
returning structured results with WARNING flags.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OverfitResult:
    """Overfitting analysis result."""

    train_auc: float | None
    cv_auc: float | None
    ho_auc: float | None
    train_cv_gap: float | None
    cv_ho_gap: float | None
    cv_fold_std: float | None
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "train_auc": self.train_auc,
            "overfit_train_cv_gap": self.train_cv_gap,
            "overfit_cv_ho_gap": self.cv_ho_gap,
            "cv_fold_std": self.cv_fold_std,
        }


# Configurable thresholds
TRAIN_CV_GAP_THRESHOLD = 0.05
CV_HO_GAP_THRESHOLD = 0.04
FOLD_STD_THRESHOLD = 0.03


def analyze(
    train_auc: float | None = None,
    cv_auc: float | None = None,
    ho_auc: float | None = None,
    fold_aucs: list[float] | None = None,
) -> OverfitResult:
    """Analyze overfitting from training metrics.

    Args:
        train_auc: Training set AUC (if available).
        cv_auc: Cross-validation mean AUC.
        ho_auc: Holdout AUC.
        fold_aucs: Per-fold AUC values.

    Returns:
        OverfitResult with gaps, std, and warnings.
    """
    warnings: list[str] = []
    train_cv_gap = None
    cv_ho_gap = None
    cv_fold_std = None

    # Train-CV gap
    if train_auc is not None and cv_auc is not None:
        train_cv_gap = train_auc - cv_auc
        if train_cv_gap > TRAIN_CV_GAP_THRESHOLD:
            warnings.append(
                f"WARNING: Train-CV AUC gap = {train_cv_gap:.4f} "
                f"(threshold: {TRAIN_CV_GAP_THRESHOLD}). "
                f"Model may be overfitting to training data."
            )

    # CV-Holdout gap
    if cv_auc is not None and ho_auc is not None:
        cv_ho_gap = cv_auc - ho_auc
        if cv_ho_gap > CV_HO_GAP_THRESHOLD:
            warnings.append(
                f"WARNING: CV-Holdout AUC gap = {cv_ho_gap:.4f} "
                f"(threshold: {CV_HO_GAP_THRESHOLD}). "
                f"CV may overestimate out-of-sample performance."
            )

    # Fold variance
    if fold_aucs and len(fold_aucs) > 1:
        import numpy as np

        cv_fold_std = float(np.std(fold_aucs))
        if cv_fold_std > FOLD_STD_THRESHOLD:
            warnings.append(
                f"WARNING: CV fold AUC std = {cv_fold_std:.4f} "
                f"(threshold: {FOLD_STD_THRESHOLD}). "
                f"Model performance varies significantly across folds."
            )

    return OverfitResult(
        train_auc=train_auc,
        cv_auc=cv_auc,
        ho_auc=ho_auc,
        train_cv_gap=train_cv_gap,
        cv_ho_gap=cv_ho_gap,
        cv_fold_std=cv_fold_std,
        warnings=warnings,
    )


def format_report(result: OverfitResult) -> str:
    """Format overfitting analysis as readable text."""
    lines = ["=== Overfitting Analysis ===", ""]

    if result.train_auc is not None:
        lines.append(f"  Train AUC:      {result.train_auc:.4f}")
    if result.cv_auc is not None:
        lines.append(f"  CV AUC:         {result.cv_auc:.4f}")
    if result.ho_auc is not None:
        lines.append(f"  Holdout AUC:    {result.ho_auc:.4f}")

    lines.append("")

    if result.train_cv_gap is not None:
        lines.append(f"  Train-CV gap:   {result.train_cv_gap:.4f}")
    if result.cv_ho_gap is not None:
        lines.append(f"  CV-HO gap:      {result.cv_ho_gap:.4f}")
    if result.cv_fold_std is not None:
        lines.append(f"  Fold std:       {result.cv_fold_std:.4f}")

    if result.warnings:
        lines.append("")
        for w in result.warnings:
            lines.append(f"  {w}")
    else:
        lines.append("")
        lines.append("  No overfitting concerns detected.")

    return "\n".join(lines)
