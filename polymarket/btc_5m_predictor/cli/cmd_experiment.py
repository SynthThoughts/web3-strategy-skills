"""Experiment tracking commands: list, compare, explain."""

from __future__ import annotations

import argparse
import json


def run(args: argparse.Namespace) -> int:
    if args.experiment_action is None:
        print("Usage: btc experiment {list|compare|explain}")
        return 1

    if args.experiment_action == "list":
        return _list(args)
    elif args.experiment_action == "compare":
        return _compare(args.id1, args.id2, getattr(args, "json", False))
    elif args.experiment_action == "explain":
        return _explain(args.id, getattr(args, "slice", False))

    return 1


# ---------------------------------------------------------------------------
# List experiments
# ---------------------------------------------------------------------------

_LIST_COLUMNS = [
    "run_id", "created_at", "n_features", "cv_mean_auc",
    "bt_sharpe", "bt_win_rate", "bt_total_pnl", "status", "tags",
]

_SORT_CHOICES = [
    "created_at", "cv_mean_auc", "bt_sharpe", "bt_win_rate",
    "bt_total_pnl", "n_features",
]


def _list(args: argparse.Namespace) -> int:
    """List model runs from model_runs table."""
    from db import get_connection

    sort_by = getattr(args, "sort_by", "created_at")
    top_n = getattr(args, "top", 20)
    status_filter = getattr(args, "status", None)
    as_json = getattr(args, "json", False)

    if sort_by not in _SORT_CHOICES:
        print(f"Error: --sort-by must be one of: {', '.join(_SORT_CHOICES)}")
        return 1

    con = get_connection(read_only=True)
    try:
        cols = ", ".join(_LIST_COLUMNS)
        where = ""
        params: list = []
        if status_filter:
            where = "WHERE status = ?"
            params.append(status_filter)

        query = (
            f"SELECT {cols} FROM model_runs {where} "
            f"ORDER BY {sort_by} DESC NULLS LAST "
            f"LIMIT ?"
        )
        params.append(top_n)

        rows = con.execute(query, params).fetchall()

        if not rows:
            print("No experiments found.")
            return 0

        if as_json:
            result = []
            for row in rows:
                result.append(dict(zip(_LIST_COLUMNS, row)))
            print(json.dumps(result, indent=2, default=str))
            return 0

        # Markdown table output
        print(f"\n=== Experiments (top {top_n} by {sort_by}) ===\n")
        header = (
            f"  {'run_id':<24s}  {'created_at':>16s}  {'feat':>4s}  "
            f"{'cv_auc':>7s}  {'sharpe':>7s}  {'win%':>6s}  "
            f"{'pnl':>10s}  {'status':>10s}  {'tags'}"
        )
        print(header)
        print(f"  {'─' * 24}  {'─' * 16}  {'─' * 4}  {'─' * 7}  {'─' * 7}  {'─' * 6}  {'─' * 10}  {'─' * 10}  {'─' * 10}")

        for row in rows:
            rid, created, nf, cv_auc, sharpe, wr, pnl, st, tags = row
            created_s = str(created)[:16] if created else "—"
            nf_s = str(nf) if nf is not None else "—"
            cv_s = f"{cv_auc:.4f}" if cv_auc is not None else "—"
            sh_s = f"{sharpe:.3f}" if sharpe is not None else "—"
            wr_s = f"{wr:.1%}" if wr is not None else "—"
            pnl_s = f"${pnl:.2f}" if pnl is not None else "—"
            st_s = st or "—"
            tags_s = tags or ""
            print(
                f"  {rid:<24s}  {created_s:>16s}  {nf_s:>4s}  "
                f"{cv_s:>7s}  {sh_s:>7s}  {wr_s:>6s}  "
                f"{pnl_s:>10s}  {st_s:>10s}  {tags_s}"
            )

        print()
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Compare two experiments
# ---------------------------------------------------------------------------

_METRIC_COLS = [
    ("cv_mean_auc", "CV AUC", ".4f"),
    ("cv_std_auc", "CV Std", ".4f"),
    ("cv_mean_acc", "CV Acc", ".4f"),
    ("cv_mean_brier", "CV Brier", ".4f"),
    ("bt_sharpe", "Sharpe", ".3f"),
    ("bt_win_rate", "Win Rate", ".1%"),
    ("bt_total_pnl", "Total PnL", ".2f"),
    ("bt_max_drawdown", "Max DD", ".2f"),
    ("bt_profit_factor", "Profit Factor", ".3f"),
    ("bt_total_trades", "Trades", "d"),
    ("train_auc", "Train AUC", ".4f"),
    ("overfit_train_cv_gap", "Train-CV Gap", ".4f"),
    ("overfit_cv_ho_gap", "CV-HO Gap", ".4f"),
    ("cv_fold_std", "Fold Std", ".4f"),
]


def _compare(id1: str, id2: str, as_json: bool) -> int:
    """Compare two model runs side by side."""
    from db import get_connection

    con = get_connection(read_only=True)
    try:
        # Fetch both runs
        cols = [c[0] for c in _METRIC_COLS]
        extra = ["n_features", "n_samples", "loss_function", "eval_metric",
                 "feature_set", "best_params", "created_at"]
        all_cols = ", ".join(cols + extra)

        r1 = con.execute(
            f"SELECT {all_cols} FROM model_runs WHERE run_id = ?", [id1]
        ).fetchone()
        r2 = con.execute(
            f"SELECT {all_cols} FROM model_runs WHERE run_id = ?", [id2]
        ).fetchone()

        if r1 is None:
            print(f"Error: run_id '{id1}' not found")
            return 1
        if r2 is None:
            print(f"Error: run_id '{id2}' not found")
            return 1

        col_names = cols + extra
        d1 = dict(zip(col_names, r1))
        d2 = dict(zip(col_names, r2))

        if as_json:
            result = {"run_1": {id1: d1}, "run_2": {id2: d2}, "diff": {}}
            for col, label, fmt in _METRIC_COLS:
                v1, v2 = d1.get(col), d2.get(col)
                if v1 is not None and v2 is not None:
                    result["diff"][col] = v2 - v1
            print(json.dumps(result, indent=2, default=str))
            return 0

        # Markdown comparison
        print(f"\n=== Experiment Comparison ===\n")
        print(f"  Run 1: {id1}  (created: {str(d1.get('created_at', ''))[:16]})")
        print(f"  Run 2: {id2}  (created: {str(d2.get('created_at', ''))[:16]})")
        print()

        # Metrics table
        print(f"  {'Metric':<16s}  {id1[-12:]:>12s}  {id2[-12:]:>12s}  {'Delta':>10s}")
        print(f"  {'─' * 16}  {'─' * 12}  {'─' * 12}  {'─' * 10}")

        for col, label, fmt in _METRIC_COLS:
            v1 = d1.get(col)
            v2 = d2.get(col)
            s1 = f"{v1:{fmt}}" if v1 is not None else "—"
            s2 = f"{v2:{fmt}}" if v2 is not None else "—"

            if v1 is not None and v2 is not None:
                delta = v2 - v1
                sign = "+" if delta > 0 else ""
                ds = f"{sign}{delta:{fmt}}"
            else:
                ds = "—"

            print(f"  {label:<16s}  {s1:>12s}  {s2:>12s}  {ds:>10s}")

        # Hyperparameters diff
        print(f"\n  --- Hyperparameters ---")
        for key in ["loss_function", "eval_metric", "n_features", "n_samples"]:
            v1 = d1.get(key)
            v2 = d2.get(key)
            if v1 != v2:
                print(f"  {key}: {v1} → {v2}")
            else:
                print(f"  {key}: {v1}")

        # Feature set diff
        fs1 = d1.get("feature_set")
        fs2 = d2.get("feature_set")
        if fs1 or fs2:
            f1 = set(json.loads(fs1)) if isinstance(fs1, str) else set(fs1 or [])
            f2 = set(json.loads(fs2)) if isinstance(fs2, str) else set(fs2 or [])
            added = f2 - f1
            removed = f1 - f2
            if added or removed:
                print(f"\n  --- Feature Set Diff ---")
                if added:
                    print(f"  Added ({len(added)}): {', '.join(sorted(added)[:10])}")
                if removed:
                    print(f"  Removed ({len(removed)}): {', '.join(sorted(removed)[:10])}")
                print(f"  Common: {len(f1 & f2)}")

        print()
        return 0
    finally:
        con.close()


def _explain(run_id: str, with_slice: bool) -> int:
    """SHAP feature explanation for a model run."""
    from db import get_connection
    from pathlib import Path

    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT version, feature_set FROM model_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
    finally:
        con.close()

    if row is None:
        print(f"Error: run_id '{run_id}' not found")
        return 1

    version, feature_set_raw = row[0], row[1]

    # Resolve feature list
    if isinstance(feature_set_raw, str):
        feature_cols = json.loads(feature_set_raw)
    elif feature_set_raw:
        feature_cols = list(feature_set_raw)
    else:
        print(f"Error: no feature_set recorded for run '{run_id}'")
        return 1

    # Find model file
    model_dir = Path(f"models/{version}")
    model_path = model_dir / "model.cbm"
    if not model_path.exists():
        print(f"Error: model file not found at {model_path}")
        return 1

    # Load data for SHAP computation
    try:
        import db
        from data import build_features

        con = db.get_connection(read_only=True)
        try:
            df_1m = con.execute("SELECT * FROM klines_1m ORDER BY open_time").fetchdf()
        finally:
            con.close()

        df_feat = build_features.build(df_1m)

        # Filter to columns that exist
        available = [c for c in feature_cols if c in df_feat.columns]
        if not available:
            print("Error: none of the recorded features exist in built feature data")
            return 1

        from training.explain import compute_shap, format_shap_report
        result = compute_shap(model_path, available, df_feat.dropna(subset=available))
        print(format_shap_report(result))

        if with_slice:
            from training.explain import compute_market_slices, format_slice_report
            slice_result = compute_market_slices(df_feat)
            print(format_slice_report(slice_result))

    except Exception as e:
        print(f"Error computing SHAP: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0
