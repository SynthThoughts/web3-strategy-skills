"""Monitoring commands: drift, diagnose, retrain-check."""

from __future__ import annotations

import argparse


def run(args: argparse.Namespace) -> int:
    if args.monitor_action is None:
        print("Usage: btc monitor {drift|diagnose|retrain-check}")
        return 1

    if args.monitor_action == "drift":
        return _drift()
    elif args.monitor_action == "diagnose":
        return _diagnose()
    elif args.monitor_action == "retrain-check":
        return _retrain_check()

    return 1


def _drift() -> int:
    """Run feature and prediction drift analysis."""
    from training.drift_monitor import (
        DriftReport,
        compute_feature_drift,
        format_drift_report,
        load_reference_distribution,
    )
    import db

    # Find the latest model version
    con = db.get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT run_id, version, cv_mean_auc FROM model_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print("No model runs found. Train a model first.")
        return 1

    run_id, version, cv_auc = row[0], row[1], row[2]
    print(f"  Analyzing drift for: {version} (run_id: {run_id})")

    # Load reference distribution
    model_dir = f"models/{version}"
    ref_dist = load_reference_distribution(model_dir)
    if ref_dist is None:
        print(f"  No reference_distribution.json found in {model_dir}")
        print("  Run training with the latest pipeline to generate reference distributions.")
        return 1

    # Load live prediction data
    con = db.get_connection(read_only=True)
    try:
        live_df = con.execute(
            "SELECT * FROM live_predictions ORDER BY timestamp DESC LIMIT 5000"
        ).fetchdf()
    except Exception:
        live_df = None
    finally:
        con.close()

    if live_df is None or live_df.empty:
        print("  No live prediction data found. Run `btc data sync` first.")
        return 1

    report = DriftReport(sample_count=len(live_df))

    if len(live_df) < 200:
        report.insufficient_data = True

    # Compute feature drift
    for feat_name, feat_ref in ref_dist.items():
        if feat_name in live_df.columns:
            vals = live_df[feat_name].dropna().tolist()
            result = compute_feature_drift(feat_name, feat_ref, vals if vals else None)
        else:
            result = compute_feature_drift(feat_name, feat_ref, None)
        report.feature_results.append(result)

    print(format_drift_report(report))
    return 0


def _diagnose() -> int:
    """Run three-layer online performance attribution analysis."""
    from training.explain import compute_diagnose, format_diagnose_report
    from training.drift_monitor import load_reference_distribution
    import db

    # Find latest model
    con = db.get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT run_id, version FROM model_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print("No model runs found. Train a model first.")
        return 1

    run_id, version = row[0], row[1]
    print(f"  Analyzing: {version} (run_id: {run_id})")

    # Load reference distribution for feature drift layer
    model_dir = f"models/{version}"
    ref_dist = load_reference_distribution(model_dir)

    # Load live prediction data
    con = db.get_connection(read_only=True)
    try:
        live_df = con.execute(
            "SELECT * FROM live_predictions ORDER BY timestamp DESC LIMIT 5000"
        ).fetchdf()
    except Exception:
        live_df = None
    finally:
        con.close()

    if live_df is None or live_df.empty:
        print("  No live prediction data found. Run `btc data sync` first.")
        return 1

    result = compute_diagnose(live_df, reference_dist=ref_dist)
    print(format_diagnose_report(result))
    return 0


def _retrain_check() -> int:
    """Check whether retraining is recommended."""
    from datetime import datetime, timezone
    from training.drift_monitor import (
        DriftReport,
        compute_feature_drift,
        compute_retrain_recommendation,
        format_retrain_recommendation,
        load_reference_distribution,
    )
    import db

    # Find latest model
    con = db.get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT run_id, version, cv_mean_auc, created_at FROM model_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print("No model runs found. Train a model first.")
        return 1

    run_id, version, cv_auc, created_at = row[0], row[1], row[2], row[3]

    # Days since training
    if created_at:
        if hasattr(created_at, 'tzinfo') and created_at.tzinfo is None:
            from datetime import timezone
            created_at = created_at.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
    else:
        days_since = None

    # Count new data since training
    con = db.get_connection(read_only=True)
    try:
        if created_at:
            new_count_row = con.execute(
                "SELECT count(*) FROM klines_1m WHERE open_time > ?",
                [created_at],
            ).fetchone()
            new_data_count = new_count_row[0] if new_count_row else 0
        else:
            new_data_count = 0
    finally:
        con.close()

    # Build a simple drift report (without live predictions if unavailable)
    report = DriftReport()

    model_dir = f"models/{version}"
    ref_dist = load_reference_distribution(model_dir)

    if ref_dist is not None:
        # Try to load live data for drift check
        con = db.get_connection(read_only=True)
        try:
            live_df = con.execute(
                "SELECT * FROM live_predictions ORDER BY timestamp DESC LIMIT 5000"
            ).fetchdf()
        except Exception:
            live_df = None
        finally:
            con.close()

        if live_df is not None and not live_df.empty:
            report.sample_count = len(live_df)
            for feat_name, feat_ref in ref_dist.items():
                if feat_name in live_df.columns:
                    vals = live_df[feat_name].dropna().tolist()
                    result = compute_feature_drift(feat_name, feat_ref, vals if vals else None)
                else:
                    result = compute_feature_drift(feat_name, feat_ref, None)
                report.feature_results.append(result)

    rec = compute_retrain_recommendation(
        drift_report=report,
        cv_auc=cv_auc,
        days_since_training=days_since,
        new_data_count=new_data_count,
    )

    print(format_retrain_recommendation(rec))
    return 0
