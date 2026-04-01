"""Evaluate multi-seed ensemble potential without changing deployment architecture.

Trains the same model with different random seeds, averages CV predictions,
and evaluates whether ensembling improves AUC/Sharpe over single-seed.
This is evaluation-only — no deployable ensemble is produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from training.backtest import run_backtest


def load_cv_predictions(run_id: str) -> pd.DataFrame:
    """Load CV predictions for a given run_id from DuckDB."""
    import db
    preds = db.get_cv_predictions_for_run(run_id)
    if preds.empty:
        raise ValueError(f"No CV predictions found for run_id '{run_id}'")
    return preds


def ensemble_predictions(run_ids: list[str]) -> pd.DataFrame:
    """Average CV predictions across multiple runs.

    All runs must share the same CV fold structure and window_start values.
    Returns a DataFrame with averaged y_prob.
    """
    all_preds = []
    for rid in run_ids:
        preds = load_cv_predictions(rid)
        preds = preds.rename(columns={"y_prob": f"y_prob_{rid}"})
        all_preds.append(preds)

    # Merge on window_start + fold to align predictions
    merged = all_preds[0]
    for p in all_preds[1:]:
        prob_col = [c for c in p.columns if c.startswith("y_prob_")][0]
        merged = merged.merge(
            p[["window_start", "fold", prob_col]],
            on=["window_start", "fold"],
            how="inner",
        )

    # Average probabilities
    prob_cols = [c for c in merged.columns if c.startswith("y_prob_")]
    merged["y_prob"] = merged[prob_cols].mean(axis=1)

    # Warn if any seed has notably different AUC
    for col in prob_cols:
        auc = roc_auc_score(merged["label"], merged[col])
        seed_id = col.replace("y_prob_", "")
        if auc < 0.55:
            print(f"  WARNING: {seed_id} has low AUC ({auc:.4f})")

    # Keep standard columns
    result = merged[["window_start", "fold", "label", "y_prob"]].copy()
    if "open_price" in merged.columns:
        result["open_price"] = merged["open_price"]
    if "close_price" in merged.columns:
        result["close_price"] = merged["close_price"]

    return result


def evaluate_ensemble(
    run_ids: list[str],
    bet_size: float = 10.0,
    threshold_up: float = 0.55,
    threshold_down: float = 0.45,
) -> dict:
    """Evaluate ensemble of multiple runs vs individual runs.

    Returns dict with individual and ensemble AUC/Sharpe metrics.
    """
    # Individual metrics
    individual_metrics = []
    for rid in run_ids:
        preds = load_cv_predictions(rid)
        auc = roc_auc_score(preds["label"], preds["y_prob"])
        trades = run_backtest(preds, bet_size=bet_size,
                              threshold_up=threshold_up,
                              threshold_down=threshold_down)
        if trades.empty:
            sharpe = 0.0
            n_trades = 0
        else:
            sharpe = float(
                trades["pnl"].mean() / (trades["pnl"].std() + 1e-10)
                * np.sqrt(len(trades))
            )
            n_trades = len(trades)
        individual_metrics.append({
            "run_id": rid,
            "auc": float(auc),
            "sharpe": sharpe,
            "n_trades": n_trades,
        })

    # Ensemble metrics
    ens_preds = ensemble_predictions(run_ids)
    ens_auc = float(roc_auc_score(ens_preds["label"], ens_preds["y_prob"]))
    ens_trades = run_backtest(ens_preds, bet_size=bet_size,
                              threshold_up=threshold_up,
                              threshold_down=threshold_down)
    if ens_trades.empty:
        ens_sharpe = 0.0
        ens_n_trades = 0
    else:
        ens_sharpe = float(
            ens_trades["pnl"].mean() / (ens_trades["pnl"].std() + 1e-10)
            * np.sqrt(len(ens_trades))
        )
        ens_n_trades = len(ens_trades)

    return {
        "individual": individual_metrics,
        "ensemble": {
            "auc": ens_auc,
            "sharpe": ens_sharpe,
            "n_trades": ens_n_trades,
            "n_models": len(run_ids),
        },
    }


def print_report(result: dict) -> None:
    """Print ensemble evaluation report."""
    print("\n" + "=" * 60)
    print("ENSEMBLE EVALUATION")
    print("=" * 60)

    print(f"\n  {'Run ID':<28s}  {'AUC':>8s}  {'Sharpe':>8s}  {'Trades':>8s}")
    print(f"  {'─' * 28}  {'─' * 8}  {'─' * 8}  {'─' * 8}")

    for m in result["individual"]:
        print(f"  {m['run_id']:<28s}  {m['auc']:>8.4f}  {m['sharpe']:>8.3f}  {m['n_trades']:>8d}")

    ens = result["ensemble"]
    print(f"  {'─' * 28}  {'─' * 8}  {'─' * 8}  {'─' * 8}")
    print(f"  {'ENSEMBLE (avg prob)':28s}  {ens['auc']:>8.4f}  {ens['sharpe']:>8.3f}  {ens['n_trades']:>8d}")

    # Delta vs best individual
    best_auc = max(m["auc"] for m in result["individual"])
    best_sharpe = max(m["sharpe"] for m in result["individual"])
    print(f"\n  Ensemble vs best individual:")
    print(f"    AUC delta:    {ens['auc'] - best_auc:+.4f}")
    print(f"    Sharpe delta: {ens['sharpe'] - best_sharpe:+.3f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate multi-seed ensemble")
    parser.add_argument("run_ids", nargs="+", help="Run IDs to ensemble")
    parser.add_argument("--bet-size", type=float, default=10.0)
    parser.add_argument("--threshold-up", type=float, default=0.55)
    parser.add_argument("--threshold-down", type=float, default=0.45)
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    if len(args.run_ids) < 2:
        print("Error: need at least 2 run_ids for ensemble evaluation")
        return 1

    try:
        result = evaluate_ensemble(
            args.run_ids,
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
