"""Train command: wraps train_pipeline with parameterization."""

from __future__ import annotations

import argparse
import sys


def run(args: argparse.Namespace) -> int:
    """Execute model training with CLI parameters."""
    from training.leakage_check import LeakageError

    # Parse feature lists
    features_include = None
    features_exclude = None
    if getattr(args, "features_include", None):
        features_include = [s.strip() for s in args.features_include.split(",")]
    if getattr(args, "features_exclude", None):
        features_exclude = [s.strip() for s in args.features_exclude.split(",")]

    kwargs = {
        "sample_start": getattr(args, "sample_start", None),
        "sample_end": getattr(args, "sample_end", None),
        "features_include": features_include,
        "features_exclude": features_exclude,
        "loss_function": getattr(args, "loss_function", "Logloss"),
        "eval_metric": getattr(args, "eval_metric", "AUC"),
        "parent_run_id": getattr(args, "parent", None),
        "tags": getattr(args, "tags", None),
    }

    try:
        from training.train_pipeline import main as train_main

        result = train_main(**kwargs)

        print(f"\n--- Training Result ---")
        print(f"  Run ID:     {result['run_id']}")
        print(f"  Version:    {result.get('version', '—')}")
        print(f"  CV AUC:     {result.get('cv_auc', 0):.4f}")
        if result.get("ho_auc") is not None:
            print(f"  HO AUC:     {result['ho_auc']:.4f}")
        print(f"  BT Sharpe:  {result.get('bt_sharpe', 0):.3f}")
        print(f"  Features:   {result.get('n_features', 0)}")

        overfit = result.get("overfit_report")
        if overfit and overfit.warnings:
            print(f"\n  Overfit Warnings:")
            for w in overfit.warnings:
                print(f"    {w}")

        return 0

    except LeakageError as e:
        print(f"\nTRAINING ABORTED — Leakage detected:\n  {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nTraining interrupted by user.")
        return 130
    except Exception as e:
        print(f"\nTraining failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
