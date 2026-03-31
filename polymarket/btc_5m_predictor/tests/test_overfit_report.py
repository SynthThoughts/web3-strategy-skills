"""Tests for overfit_report module."""

from training.overfit_report import analyze, format_report


def test_no_overfitting():
    """Clean metrics produce no warnings."""
    result = analyze(train_auc=0.68, cv_auc=0.65, ho_auc=0.63, fold_aucs=[0.64, 0.65, 0.66, 0.65])
    assert len(result.warnings) == 0
    assert result.train_cv_gap is not None
    assert abs(result.train_cv_gap - 0.03) < 1e-6


def test_train_cv_gap_warning():
    """Large train-CV gap triggers warning."""
    result = analyze(train_auc=0.85, cv_auc=0.65)
    assert len(result.warnings) == 1
    assert "Train-CV" in result.warnings[0]
    assert abs(result.train_cv_gap - 0.20) < 1e-6


def test_cv_ho_gap_warning():
    """Large CV-holdout gap triggers warning."""
    result = analyze(cv_auc=0.70, ho_auc=0.60)
    assert any("CV-Holdout" in w for w in result.warnings)
    assert abs(result.cv_ho_gap - 0.10) < 1e-6


def test_fold_std_warning():
    """High fold variance triggers warning."""
    result = analyze(fold_aucs=[0.50, 0.70, 0.55, 0.75])
    assert any("fold AUC std" in w for w in result.warnings)
    assert result.cv_fold_std > 0.03


def test_multiple_warnings():
    """Multiple overfitting signals produce multiple warnings."""
    result = analyze(
        train_auc=0.90,
        cv_auc=0.65,
        ho_auc=0.55,
        fold_aucs=[0.50, 0.80, 0.55, 0.75],
    )
    assert len(result.warnings) == 3


def test_to_dict():
    """to_dict produces correct keys."""
    result = analyze(train_auc=0.70, cv_auc=0.65, ho_auc=0.63)
    d = result.to_dict()
    assert "train_auc" in d
    assert "overfit_train_cv_gap" in d
    assert "overfit_cv_ho_gap" in d
    assert "cv_fold_std" in d


def test_partial_data():
    """Missing metrics are handled gracefully."""
    result = analyze(cv_auc=0.65)
    assert result.train_cv_gap is None
    assert result.cv_ho_gap is None
    assert len(result.warnings) == 0


def test_format_report():
    """format_report produces readable text."""
    result = analyze(train_auc=0.80, cv_auc=0.65, ho_auc=0.62, fold_aucs=[0.64, 0.66])
    text = format_report(result)
    assert "Overfitting Analysis" in text
    assert "Train AUC" in text
    assert "WARNING" in text  # train-cv gap is 0.15
