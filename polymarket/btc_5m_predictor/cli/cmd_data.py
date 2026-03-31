"""Data management commands: status, fetch, sync, health, validate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone


def run(args: argparse.Namespace) -> int:
    if args.data_action is None:
        print("Usage: btc data {status|fetch|sync|health|validate}")
        return 1

    if args.data_action == "status":
        return _status()
    elif args.data_action == "fetch":
        return _fetch(args.source, args.days)
    elif args.data_action == "sync":
        return _sync(getattr(args, "full", False))
    elif args.data_action == "health":
        return _health()
    elif args.data_action == "validate":
        return _validate()

    return 1


# ---------------------------------------------------------------------------
# Source registry: name -> (import_path, function_name, default_days)
# ---------------------------------------------------------------------------
_FETCH_SOURCES: dict[str, tuple[str, str, int]] = {
    "klines_1m": ("data.fetch_binance_klines", "fetch_incremental", 90),
    "klines_30m": ("data.fetch_binance_klines", "fetch_incremental_30m", 90),
    "klines_4h": ("data.fetch_binance_klines", "fetch_incremental_4h", 90),
    "futures": ("data.fetch_futures_data", "fetch_all_incremental", 90),
    "coinbase": ("data.fetch_coinbase", "fetch_incremental", 90),
    "hyperliquid": ("data.fetch_hyperliquid", "fetch_incremental", 30),
}


def _status() -> int:
    """Show data coverage: row counts and time ranges for all tables."""
    from db import get_data_coverage

    coverage = get_data_coverage()
    if not coverage:
        print("No data tables found.")
        return 0

    now = datetime.now(timezone.utc)
    print("\n=== Data Coverage ===\n")
    print(f"  {'Table':<30s}  {'Rows':>10s}  {'From':>20s}  {'To':>20s}  {'Gap':>8s}")
    print(f"  {'─' * 30}  {'─' * 10}  {'─' * 20}  {'─' * 20}  {'─' * 8}")

    for table, info in sorted(coverage.items()):
        count = info["count"]
        min_t = info.get("min_time")
        max_t = info.get("max_time")

        if count == 0:
            print(f"  {table:<30s}  {count:>10,d}  {'—':>20s}  {'—':>20s}  {'—':>8s}")
            continue

        min_str = min_t.strftime("%Y-%m-%d %H:%M") if min_t else "—"
        max_str = max_t.strftime("%Y-%m-%d %H:%M") if max_t else "—"

        # Gap = time since last data point
        if max_t:
            gap_hours = (now - max_t).total_seconds() / 3600
            gap_str = f"{gap_hours:.1f}h"
        else:
            gap_str = "—"

        print(f"  {table:<30s}  {count:>10,d}  {min_str:>20s}  {max_str:>20s}  {gap_str:>8s}")

    print()
    return 0


def _fetch(source: str, days: int | None) -> int:
    """Fetch data from a specific source or all sources."""
    if source == "all":
        total = 0
        for name in _FETCH_SOURCES:
            print(f"--- Fetching {name} ---")
            ret = _fetch_one(name, days)
            if ret != 0:
                return ret
            total += 1
        print(f"\nAll {total} sources fetched.")
        return 0

    if source not in _FETCH_SOURCES:
        print(f"Error: unknown source '{source}'")
        print(f"Available sources: {', '.join(sorted(_FETCH_SOURCES))} or 'all'")
        return 1

    return _fetch_one(source, days)


def _fetch_one(source: str, days: int | None) -> int:
    """Fetch from a single source."""
    import importlib

    module_path, func_name, default_days = _FETCH_SOURCES[source]
    d = days if days is not None else default_days

    try:
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        rows = func(days=d)
        print(f"  {source}: {rows} rows inserted")
        return 0
    except Exception as e:
        print(f"  Error fetching {source}: {e}")
        return 1


def _sync(full: bool) -> int:
    """Sync data from VPS using sync_data.py."""
    from service.sync_data import pull_all

    mode = "full" if full else "incremental"
    print(f"Syncing data from VPS ({mode})...")

    try:
        results = pull_all(full=full)
        total = sum(results.values())
        print(f"\nSync complete: {total} total rows imported")
        for table, rows in sorted(results.items()):
            if rows > 0:
                print(f"  {table}: {rows} rows")
        return 0
    except Exception as e:
        print(f"Error syncing data: {e}")
        return 1


def _health() -> int:
    """Check VPS data collector health via SSH."""
    try:
        result = subprocess.run(
            [
                "ssh", "openclaw",
                "cd /opt/btc-5m-predictor && uv run python service/data_collector.py --status",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"VPS health check failed (exit {result.returncode}):")
            print(result.stderr or result.stdout)
            return 1

        print("=== VPS Data Collector Status ===\n")
        print(result.stdout)

        # Parse output to check for stale sources (gap > 1 hour)
        alerts = []
        for line in result.stdout.splitlines():
            if "gap:" in line:
                try:
                    gap_part = line.split("gap:")[1].strip().rstrip(")")
                    gap_h = float(gap_part.replace("h", ""))
                    if gap_h > 1.0:
                        table = line.split()[0].strip()
                        alerts.append(f"  ALERT: {table} — last data {gap_h:.1f}h ago")
                except (ValueError, IndexError):
                    pass

        if alerts:
            print("\n=== Alerts ===")
            for a in alerts:
                print(a)

        return 0
    except subprocess.TimeoutExpired:
        print("Error: SSH connection to VPS timed out (30s)")
        return 1
    except FileNotFoundError:
        print("Error: ssh command not found")
        return 1
    except Exception as e:
        print(f"Error connecting to VPS: {e}")
        return 1


def _validate() -> int:
    """Validate local data integrity: continuity, value ranges, alignment."""
    from db import get_connection

    con = get_connection(read_only=True)
    issues: list[str] = []

    try:
        # (a) Time continuity — check for gaps > 10 minutes in klines_1m
        print("Checking time continuity...")
        gaps = con.execute("""
            SELECT open_time,
                   LEAD(open_time) OVER (ORDER BY open_time) AS next_time,
                   DATEDIFF('minute', open_time,
                            LEAD(open_time) OVER (ORDER BY open_time)) AS gap_min
            FROM klines_1m
            ORDER BY open_time
        """).fetchdf()
        big_gaps = gaps[gaps["gap_min"] > 10].head(20)
        if len(big_gaps) > 0:
            issues.append(f"  WARNING: {len(big_gaps)} time gaps > 10 min in klines_1m")
            for _, row in big_gaps.iterrows():
                issues.append(
                    f"    {row['open_time']} → {row['next_time']} ({row['gap_min']} min)"
                )

        # (b) Value range — BTC price in [1000, 500000], volume non-negative
        print("Checking value ranges...")
        range_check = con.execute("""
            SELECT
                MIN(close) AS min_close,
                MAX(close) AS max_close,
                SUM(CASE WHEN close < 1000 OR close > 500000 THEN 1 ELSE 0 END) AS price_outliers,
                SUM(CASE WHEN volume < 0 THEN 1 ELSE 0 END) AS neg_volume
            FROM klines_1m
        """).fetchone()

        if range_check[2] > 0:
            issues.append(
                f"  ALERT: {range_check[2]} rows with BTC price outside [1000, 500000] "
                f"(range: {range_check[0]:.2f} - {range_check[1]:.2f})"
            )
        if range_check[3] > 0:
            issues.append(f"  ALERT: {range_check[3]} rows with negative volume")

        # (c) Time alignment — klines_1m vs futures tables
        print("Checking time alignment...")
        for ftable in [
            "futures_funding_rate",
            "futures_open_interest",
            "futures_taker_volume",
        ]:
            try:
                align = con.execute(f"""
                    WITH k AS (
                        SELECT DISTINCT DATE_TRUNC('hour', open_time) AS hour
                        FROM klines_1m
                    ),
                    f AS (
                        SELECT DISTINCT DATE_TRUNC('hour', datetime) AS hour
                        FROM {ftable}
                    )
                    SELECT
                        (SELECT COUNT(*) FROM k) AS kline_hours,
                        (SELECT COUNT(*) FROM f) AS futures_hours,
                        (SELECT COUNT(*) FROM k INNER JOIN f ON k.hour = f.hour) AS matched
                """).fetchone()

                if align[0] > 0 and align[1] > 0:
                    match_pct = align[2] / min(align[0], align[1]) * 100
                    if match_pct < 95:
                        issues.append(
                            f"  WARNING: {ftable} alignment with klines_1m: "
                            f"{match_pct:.1f}% ({align[2]}/{min(align[0], align[1])} hours)"
                        )
            except Exception:
                pass  # table may not exist

    finally:
        con.close()

    # Report
    print("\n=== Validation Results ===\n")
    if not issues:
        print("  PASS — all checks passed")
    else:
        for issue in issues:
            print(issue)
        print(f"\n  {len(issues)} issue(s) found")

    return 0
