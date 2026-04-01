"""Evaluate probability calibration impact on Sharpe.

Uses CV-fold-internal calibration to avoid in-sample bias:
- For each CV fold, fit calibrator on train split, apply to test split
- Produces out-of-fold calibrated probabilities
- Compares Sharpe before/after calibration via backtest
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from training.backtest import run_backtest


def load_cv_predictions(run_id: str) -> pd.DataFrame:
    """Load CV predictions for a given run_id from DuckDB."""
    import db
    preds = db.get_cv_predictions_for_run(run_id)
    if preds.empty:
        raise ValueError(f"No CV predictions found for run_id '{run_id}'")
    return preds


def calibrate_cv_predictions(
    preds: pd.DataFrame,
    method: str = "sigmoid",
    min_samples: int = 500,
) -> pd.DataFrame | None:
    """Apply calibration within each CV fold.

    For each fold:
    - Use all other folds as calibration training data
    - Apply calibrated transform to this fold's predictions

    Platt scaling: fit LogisticRegression on predicted probabilities.
    Isotonic: fit IsotonicRegression on predicted probabilities.

    Args:
        preds: CV predictions with columns [window_start, fold, label, y_prob]
        method: "sigmoid" (Platt) or "isotonic"
        min_samples: minimum samples per calibration training set for isotonic

    Returns:
        DataFrame with calibrated y_prob, or None if calibration is not feasible.
    """
    folds = sorted(preds["fold"].unique())
    if len(folds) < 2:
        print("  Error: need at least 2 folds for calibration")
        return None

    calibrated_parts = []

    for test_fold in folds:
        train_mask = preds["fold"] != test_fold
        test_mask = preds["fold"] == test_fold

        train_data = preds[train_mask]
        test_data = preds[test_mask]

        n_train = len(train_data)

        # Isotonic needs sufficient samples
        if method == "isotonic" and n_train < min_samples:
            print(f"  Fold {test_fold}: insufficient samples for isotonic "
                  f"({n_train} < {min_samples}), skipping")
            return None

        train_probs = train_data["y_prob"].values
        train_labels = train_data["label"].values
        test_probs = test_data["y_prob"].values

        try:
            if method == "sigmoid":
                # Platt scaling: logistic regression on predicted probabilities
                cal = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
                cal.fit(train_probs.reshape(-1, 1), train_labels)
                cal_probs = cal.predict_proba(test_probs.reshape(-1, 1))[:, 1]
            else:
                # Isotonic regression
                cal = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip")
                cal.fit(train_probs, train_labels)
                cal_probs = cal.predict(test_probs)

            fold_result = test_data.copy()
            fold_result["y_prob"] = cal_probs
            calibrated_parts.append(fold_result)

        except Exception as e:
            print(f"  Fold {test_fold}: calibration failed — {e}")
            return None

    return pd.concat(calibrated_parts, ignore_index=True)


def evaluate_calibration(
    run_id: str,
    methods: list[str] | None = None,
    bet_size: float = 10.0,
    threshold_up: float = 0.55,
    threshold_down: float = 0.45,
) -> dict:
    """Evaluate calibration methods for a model run.

    Returns dict with before/after metrics for each calibration method.
    """
    if methods is None:
        methods = ["sigmoid", "isotonic"]

    preds = load_cv_predictions(run_id)

    # Baseline metrics
    base_auc = float(roc_auc_score(preds["label"], preds["y_prob"]))
    base_trades = run_backtest(preds, bet_size=bet_size,
                                threshold_up=threshold_up,
                                threshold_down=threshold_down)
    if base_trades.empty:
        base_sharpe = 0.0
        base_n_trades = 0
    else:
        base_sharpe = float(
            base_trades["pnl"].mean() / (base_trades["pnl"].std() + 1e-10)
            * np.sqrt(len(base_trades))
        )
        base_n_trades = len(base_trades)

    result = {
        "run_id": run_id,
        "baseline": {
            "auc": base_auc,
            "sharpe": base_sharpe,
            "n_trades": base_n_trades,
        },
        "calibrations": {},
    }

    for method in methods:
        method_name = "platt" if method == "sigmoid" else method
        print(f"\n  Evaluating {method_name} calibration...")

        cal_preds = calibrate_cv_predictions(preds, method=method)
        if cal_preds is None:
            result["calibrations"][method_name] = {
                "status": "skipped",
                "reason": "insufficient samples or calibration failed",
            }
            continue

        cal_auc = float(roc_auc_score(cal_preds["label"], cal_preds["y_prob"]))
        cal_trades = run_backtest(cal_preds, bet_size=bet_size,
                                  threshold_up=threshold_up,
                                  threshold_down=threshold_down)
        if cal_trades.empty:
            cal_sharpe = 0.0
            cal_n_trades = 0
        else:
            cal_sharpe = float(
                cal_trades["pnl"].mean() / (cal_trades["pnl"].std() + 1e-10)
                * np.sqrt(len(cal_trades))
            )
            cal_n_trades = len(cal_trades)

        result["calibrations"][method_name] = {
            "status": "completed",
            "auc": cal_auc,
            "sharpe": cal_sharpe,
            "n_trades": cal_n_trades,
            "auc_delta": cal_auc - base_auc,
            "sharpe_delta": cal_sharpe - base_sharpe,
        }

    return result


def print_report(result: dict) -> None:
    """Print calibration evaluation report."""
    print("\n" + "=" * 60)
    print("CALIBRATION EVALUATION")
    print("=" * 60)

    base = result["baseline"]
    print(f"\n  Run: {result['run_id']}")
    print(f"\n  {'Method':<16s}  {'AUC':>8s}  {'Sharpe':>8s}  {'Trades':>8s}  {'ΔAUC':>8s}  {'ΔSharpe':>8s}")
    print(f"  {'─' * 16}  {'─' * 8}  {'─' * 8}  {'─' * 8}  {'─' * 8}  {'─' * 8}")
    print(f"  {'Baseline':<16s}  {base['auc']:>8.4f}  {base['sharpe']:>8.3f}  {base['n_trades']:>8d}  {'—':>8s}  {'—':>8s}")

    for name, cal in result["calibrations"].items():
        if cal["status"] == "skipped":
            print(f"  {name:<16s}  {'skipped':>8s}  {'—':>8s}  {'—':>8s}  {'—':>8s}  {'—':>8s}")
        else:
            print(f"  {name:<16s}  {cal['auc']:>8.4f}  {cal['sharpe']:>8.3f}  "
                  f"{cal['n_trades']:>8d}  {cal['auc_delta']:>+8.4f}  {cal['sharpe_delta']:>+8.3f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate probability calibration")
    parser.add_argument("run_id", help="Run ID to evaluate")
    parser.add_argument("--methods", nargs="+", default=["sigmoid", "isotonic"],
                        choices=["sigmoid", "isotonic"],
                        help="Calibration methods to try")
    parser.add_argument("--bet-size", type=float, default=10.0)
    parser.add_argument("--threshold-up", type=float, default=0.55)
    parser.add_argument("--threshold-down", type=float, default=0.45)
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    try:
        result = evaluate_calibration(
            args.run_id,
            methods=args.methods,
            bet_size=args.bet_size,
            threshold_up=args.threshold_up,
            threshold_down=args.threshold_down,
        )
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
