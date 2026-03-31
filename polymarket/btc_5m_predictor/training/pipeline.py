"""Full pipeline: fetch -> store -> features -> select -> train -> backtest -> dashboard.

Usage:
    uv run python pipeline.py              # Full pipeline
    uv run python pipeline.py --skip-fetch # Skip data fetching
    uv run python pipeline.py --dashboard  # Only regenerate dashboard
"""

import argparse

import db
from data import fetch_binance_klines, fetch_futures_data
from . import train_pipeline
from models import dashboard


def main():
    parser = argparse.ArgumentParser(description="BTC 5m Predictor Pipeline")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    db.init_db()

    if args.dashboard:
        dashboard.generate()
        return

    # Step 1: Incremental data fetch
    if not args.skip_fetch:
        print("=" * 60)
        print("STEP 1: DATA INGESTION")
        print("=" * 60)
        n_klines = fetch_binance_klines.fetch_incremental()
        n_futures = fetch_futures_data.fetch_all_incremental()
        print(f"  New klines: {n_klines}, New futures records: {n_futures}")

    # Step 2-6: Train pipeline (features -> select -> optuna -> CV -> backtest)
    print("\n" + "=" * 60)
    print("STEP 2: TRAIN PIPELINE")
    print("=" * 60)
    result = train_pipeline.main()
    run_id = result["run_id"]

    # Step 7: Dashboard
    print("\n" + "=" * 60)
    print("STEP 7: DASHBOARD")
    print("=" * 60)
    dashboard.generate(run_id)

    print(f"\nPipeline complete! Run ID: {run_id}")


if __name__ == "__main__":
    main()
