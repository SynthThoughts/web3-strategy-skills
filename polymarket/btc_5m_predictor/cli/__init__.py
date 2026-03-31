"""BTC 5-minute predictor CLI - unified orchestration layer."""

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc",
        description="BTC 5-minute predictor ML iteration CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- data ---
    data_parser = subparsers.add_parser("data", help="Data management")
    data_sub = data_parser.add_subparsers(dest="data_action")

    data_sub.add_parser("status", help="Show data source coverage and gaps")

    fetch_p = data_sub.add_parser("fetch", help="Fetch data from sources")
    fetch_p.add_argument("--source", required=True, help="Data source name (klines_1m, futures, coinbase, eth, klines_30m, klines_4h, all)")
    fetch_p.add_argument("--days", type=int, default=None, help="Number of days to fetch")

    data_sub.add_parser("sync", help="Sync data from VPS to local").add_argument("--full", action="store_true", help="Full sync instead of incremental")

    data_sub.add_parser("health", help="Check VPS data collection health")

    data_sub.add_parser("validate", help="Validate local data integrity")

    # --- feature ---
    feat_parser = subparsers.add_parser("feature", help="Feature engineering tools")
    feat_sub = feat_parser.add_subparsers(dest="feature_action")

    val_p = feat_sub.add_parser("validate", help="Validate a specific feature")
    val_p.add_argument("name", help="Feature name to validate")

    exp_p = feat_sub.add_parser("explore", help="Batch feature quality report")
    exp_p.add_argument("--category", default=None, help="Filter by feature category")

    # --- train ---
    train_parser = subparsers.add_parser("train", help="Train models")
    train_parser.add_argument("--sample-start", default=None, help="Training data start date (YYYY-MM-DD)")
    train_parser.add_argument("--sample-end", default=None, help="Training data end date (YYYY-MM-DD)")
    train_parser.add_argument("--label-threshold", type=float, default=None, help="Label threshold percentage")
    train_parser.add_argument("--features-include", default=None, help="Comma-separated feature names or category names to include")
    train_parser.add_argument("--features-exclude", default=None, help="Comma-separated feature names or category names to exclude")
    train_parser.add_argument("--loss-function", default="Logloss", help="CatBoost loss function (Logloss, CrossEntropy)")
    train_parser.add_argument("--eval-metric", default="AUC", help="CatBoost eval metric (AUC, F1, Accuracy, BalancedAccuracy)")
    train_parser.add_argument("--parent", default=None, help="Parent run_id for lineage tracking")
    train_parser.add_argument("--tags", default=None, help="Comma-separated experiment tags")

    # --- experiment ---
    exp_parser = subparsers.add_parser("experiment", help="Experiment tracking")
    exp_sub = exp_parser.add_subparsers(dest="experiment_action")

    list_p = exp_sub.add_parser("list", help="List experiments")
    list_p.add_argument("--sort-by", default="created_at", help="Sort by metric (created_at, cv_mean_auc, bt_sharpe, bt_win_rate, bt_total_pnl)")
    list_p.add_argument("--top", type=int, default=10, help="Number of results to show")
    list_p.add_argument("--status", default=None, help="Filter by status")
    list_p.add_argument("--tags", default=None, help="Filter by tags (comma-separated)")
    list_p.add_argument("--json", action="store_true", help="Output as JSON")

    cmp_p = exp_sub.add_parser("compare", help="Compare two experiments")
    cmp_p.add_argument("id1", help="First run_id")
    cmp_p.add_argument("id2", help="Second run_id")
    cmp_p.add_argument("--json", action="store_true", help="Output as JSON")

    explain_p = exp_sub.add_parser("explain", help="SHAP feature explanation")
    explain_p.add_argument("id", help="Run ID to explain")
    explain_p.add_argument("--slice", action="store_true", help="Include market state slice analysis")

    # --- deploy ---
    dep_parser = subparsers.add_parser("deploy", help="Deployment management")
    dep_sub = dep_parser.add_subparsers(dest="deploy_action")

    promote_p = dep_sub.add_parser("promote", help="Promote model to production")
    promote_p.add_argument("run_id", help="Run ID to promote")

    shadow_p = dep_sub.add_parser("shadow", help="Manage shadow/challenger models")
    shadow_sub = shadow_p.add_subparsers(dest="shadow_action")
    shadow_sub.add_parser("add", help="Add shadow model").add_argument("run_id", help="Run ID to shadow")
    shadow_sub.add_parser("remove", help="Remove shadow model").add_argument("version", help="Model version to remove")
    shadow_sub.add_parser("list", help="List shadow models")

    dep_sub.add_parser("compare", help="Compare shadow vs champion performance")

    # --- monitor ---
    mon_parser = subparsers.add_parser("monitor", help="Model monitoring")
    mon_sub = mon_parser.add_subparsers(dest="monitor_action")

    mon_sub.add_parser("drift", help="Feature and prediction drift report")
    mon_sub.add_parser("diagnose", help="Online performance attribution analysis")
    mon_sub.add_parser("retrain-check", help="Retrain recommendation based on drift + data")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # Dispatch to subcommand modules
    if args.command == "data":
        from cli.cmd_data import run
        return run(args)
    elif args.command == "feature":
        from cli.cmd_feature import run
        return run(args)
    elif args.command == "train":
        from cli.cmd_train import run
        return run(args)
    elif args.command == "experiment":
        from cli.cmd_experiment import run
        return run(args)
    elif args.command == "deploy":
        from cli.cmd_deploy import run
        return run(args)
    elif args.command == "monitor":
        from cli.cmd_monitor import run
        return run(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
