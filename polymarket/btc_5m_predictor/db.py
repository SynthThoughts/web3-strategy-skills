"""DuckDB database layer for the BTC 5-minute predictor pipeline."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

from config import DATA_DIR, DB_PATH, PARQUET_FILE

# Mapping from futures table name to parquet file
_RAW_DIR = DATA_DIR / "raw"
_FUTURES_TABLES = {
    "futures_funding_rate": _RAW_DIR / "btcusdt_funding_rate.parquet",
    "futures_open_interest": _RAW_DIR / "btcusdt_open_interest.parquet",
    "futures_top_ls_account": _RAW_DIR / "btcusdt_top_ls_account.parquet",
    "futures_top_ls_position": _RAW_DIR / "btcusdt_top_ls_position.parquet",
    "futures_global_ls": _RAW_DIR / "btcusdt_global_ls.parquet",
    "futures_taker_volume": _RAW_DIR / "btcusdt_taker_volume.parquet",
}

_VALID_FUTURES_TABLES = set(_FUTURES_TABLES.keys())


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(read_only: bool = False, *, retries: int = 5, backoff: float = 1.0) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection with retry on lock conflict.

    DuckDB only allows one writer process at a time. When multiple services
    (data_collector, feature_generator, live_monitor) compete for the lock,
    this retries with exponential backoff instead of failing immediately.
    """
    for attempt in range(retries):
        try:
            return duckdb.connect(str(DB_PATH))
        except duckdb.IOException as e:
            if "Could not set lock" in str(e) and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                logger.debug("DB lock conflict, retry %d/%d in %.1fs", attempt + 1, retries, wait)
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist, then auto-import parquet files
    into any empty tables."""
    con = get_connection()
    try:
        # -- klines_1m
        con.execute("""
            CREATE TABLE IF NOT EXISTS klines_1m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE,
                close_time  TIMESTAMPTZ,
                quote_volume    DOUBLE,
                trades          INT,
                taker_buy_base  DOUBLE,
                taker_buy_quote DOUBLE
            )
        """)

        # -- klines_30m (behavioral state features)
        con.execute("""
            CREATE TABLE IF NOT EXISTS klines_30m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE,
                close_time  TIMESTAMPTZ,
                quote_volume    DOUBLE,
                trades          INT,
                taker_buy_base  DOUBLE,
                taker_buy_quote DOUBLE
            )
        """)

        # -- klines_4h (multi-timeframe features)
        con.execute("""
            CREATE TABLE IF NOT EXISTS klines_4h (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE,
                close_time  TIMESTAMPTZ,
                quote_volume    DOUBLE,
                trades          INT,
                taker_buy_base  DOUBLE,
                taker_buy_quote DOUBLE
            )
        """)

        # -- eth_klines_1m (cross-asset features)
        con.execute("""
            CREATE TABLE IF NOT EXISTS eth_klines_1m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE,
                close_time  TIMESTAMPTZ,
                quote_volume    DOUBLE,
                trades          INT,
                taker_buy_base  DOUBLE,
                taker_buy_quote DOUBLE
            )
        """)

        # -- coinbase_klines_1m (Coinbase premium calculation)
        con.execute("""
            CREATE TABLE IF NOT EXISTS coinbase_klines_1m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE
            )
        """)

        # -- futures tables with funding-rate schema
        con.execute("""
            CREATE TABLE IF NOT EXISTS futures_funding_rate (
                datetime    TIMESTAMPTZ PRIMARY KEY,
                fundingRate DOUBLE,
                markPrice   DOUBLE
            )
        """)

        # -- futures tables with open interest schema
        con.execute("""
            CREATE TABLE IF NOT EXISTS futures_open_interest (
                datetime            TIMESTAMPTZ PRIMARY KEY,
                sumOpenInterest     DOUBLE,
                sumOpenInterestValue DOUBLE
            )
        """)

        # -- futures tables with long/short ratio schema
        for tbl in ("futures_top_ls_account", "futures_top_ls_position", "futures_global_ls"):
            con.execute(f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    datetime       TIMESTAMPTZ PRIMARY KEY,
                    longShortRatio DOUBLE,
                    longAccount    DOUBLE,
                    shortAccount   DOUBLE
                )
            """)

        # -- futures taker buy/sell volume
        con.execute("""
            CREATE TABLE IF NOT EXISTS futures_taker_volume (
                datetime     TIMESTAMPTZ PRIMARY KEY,
                buySellRatio DOUBLE,
                buyVol       DOUBLE,
                sellVol      DOUBLE
            )
        """)

        # -- Hyperliquid 1m candles (~3d API history, accumulated continuously)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_klines_1m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE
            )
        """)

        # -- Hyperliquid 5m candles (~17d API history)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_klines_5m (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE
            )
        """)

        # -- Hyperliquid 1h candles (~180d API history)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_klines_1h (
                open_time   TIMESTAMPTZ PRIMARY KEY,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      DOUBLE
            )
        """)

        # -- Hyperliquid funding rate (hourly)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_funding_rate (
                datetime    TIMESTAMPTZ PRIMARY KEY,
                fundingRate DOUBLE,
                premium     DOUBLE
            )
        """)

        # -- Hyperliquid asset context snapshots (OI, funding, premium, prices)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_asset_ctx (
                datetime    TIMESTAMPTZ PRIMARY KEY,
                funding     DOUBLE,
                openInterest DOUBLE,
                premium     DOUBLE,
                oraclePx    DOUBLE,
                markPx      DOUBLE,
                midPx       DOUBLE,
                dayNtlVlm   DOUBLE,
                dayBaseVlm  DOUBLE,
                impactBid   DOUBLE,
                impactAsk   DOUBLE
            )
        """)

        # -- Hyperliquid L2 orderbook snapshots (depth + imbalance)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_orderbook (
                datetime    TIMESTAMPTZ PRIMARY KEY,
                bestBid     DOUBLE,
                bestAsk     DOUBLE,
                spread_bps  DOUBLE,
                bidDepth5   DOUBLE,
                askDepth5   DOUBLE,
                bidDepth10  DOUBLE,
                askDepth10  DOUBLE,
                imbalance5  DOUBLE,
                imbalance10 DOUBLE,
                bidLevels   INTEGER,
                askLevels   INTEGER
            )
        """)

        # -- Hyperliquid liquidation events (from recentTrades with zero hash)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_liquidations (
                datetime    TIMESTAMPTZ,
                side        VARCHAR,
                price       DOUBLE,
                size        DOUBLE,
                notional    DOUBLE,
                PRIMARY KEY (datetime, side, price, size)
            )
        """)

        # -- Cross-exchange predicted funding rates (HL/Binance/Bybit)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hl_predicted_fundings (
                datetime    TIMESTAMPTZ PRIMARY KEY,
                hl_rate     DOUBLE,
                hl_next_time TIMESTAMPTZ,
                bin_rate    DOUBLE,
                bybit_rate  DOUBLE
            )
        """)

        # -- liquidations (Binance forced liquidation events)
        con.execute("""
            CREATE TABLE IF NOT EXISTS liquidations (
                event_time      TIMESTAMPTZ,
                symbol          VARCHAR,
                side            VARCHAR,
                order_type      VARCHAR,
                time_in_force   VARCHAR,
                original_qty    DOUBLE,
                price           DOUBLE,
                avg_price       DOUBLE,
                order_status    VARCHAR,
                last_filled_qty DOUBLE,
                filled_qty      DOUBLE,
                trade_time      TIMESTAMPTZ PRIMARY KEY
            )
        """)

        # -- orderbook_snapshots (periodic depth snapshots)
        con.execute("""
            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                snapshot_time   TIMESTAMPTZ PRIMARY KEY,
                best_bid        DOUBLE,
                best_ask        DOUBLE,
                bid_depth_5     DOUBLE,
                ask_depth_5     DOUBLE,
                bid_depth_10    DOUBLE,
                ask_depth_10    DOUBLE,
                bid_depth_20    DOUBLE,
                ask_depth_20    DOUBLE,
                imbalance_5     DOUBLE,
                imbalance_10    DOUBLE,
                imbalance_20    DOUBLE,
                spread_bps      DOUBLE,
                mid_price       DOUBLE
            )
        """)

        # -- features_latest (precomputed feature vectors for prediction)
        con.execute("""
            CREATE TABLE IF NOT EXISTS features_latest (
                window_start    TIMESTAMPTZ PRIMARY KEY,
                computed_at     TIMESTAMPTZ,
                features_json   VARCHAR
            )
        """)

        # -- model_runs
        con.execute("""
            CREATE TABLE IF NOT EXISTS model_runs (
                run_id          VARCHAR PRIMARY KEY,
                created_at      TIMESTAMPTZ,
                data_start      TIMESTAMPTZ,
                data_end        TIMESTAMPTZ,
                n_samples       INT,
                n_features      INT,
                optuna_trials   INT,
                best_cv_auc     DOUBLE,
                best_params     JSON,
                cv_mean_auc     DOUBLE,
                cv_std_auc      DOUBLE,
                cv_mean_acc     DOUBLE,
                cv_mean_brier   DOUBLE,
                cv_folds        INT,
                bt_total_trades INT,
                bt_win_rate     DOUBLE,
                bt_total_pnl    DOUBLE,
                bt_max_drawdown DOUBLE,
                bt_sharpe       DOUBLE,
                bt_profit_factor DOUBLE,
                model_path      VARCHAR,
                status          VARCHAR DEFAULT 'completed'
            )
        """)

        # -- feature_importance
        con.execute("""
            CREATE TABLE IF NOT EXISTS feature_importance (
                run_id     VARCHAR,
                feature    VARCHAR,
                importance DOUBLE,
                rank       INT,
                PRIMARY KEY (run_id, feature)
            )
        """)

        # -- cv_predictions
        con.execute("""
            CREATE TABLE IF NOT EXISTS cv_predictions (
                run_id       VARCHAR,
                window_start TIMESTAMPTZ,
                fold         INT,
                label        INT,
                y_prob       DOUBLE,
                open_price   DOUBLE,
                close_price  DOUBLE
            )
        """)

        # -- backtest_trades
        con.execute("""
            CREATE TABLE IF NOT EXISTS backtest_trades (
                run_id       VARCHAR,
                window_start TIMESTAMPTZ,
                direction    VARCHAR,
                prob         DOUBLE,
                correct      BOOLEAN,
                buy_price    DOUBLE,
                fee_usd      DOUBLE,
                pnl          DOUBLE
            )
        """)

        # -- live_predictions: migrate old schema if needed
        try:
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'live_predictions'"
            ).fetchall()]
            if cols and ("predicted_at" not in cols or "bet" not in cols or "bet_size" not in cols):
                con.execute("DROP TABLE live_predictions")
        except Exception:
            pass

        # -- live_predictions (real-time model signals + actual outcomes)
        # Records ALL predictions; `bet` flag distinguishes actual bets
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_predictions (
                window_start    TIMESTAMPTZ,
                predicted_at    TIMESTAMPTZ,
                prob_up         DOUBLE,
                direction       VARCHAR,
                confidence      DOUBLE,
                ev              DOUBLE,
                market_price    DOUBLE,
                bet             BOOLEAN DEFAULT false,
                bet_size        DOUBLE,
                btc_open        DOUBLE,
                btc_close       DOUBLE,
                actual          VARCHAR,
                correct         BOOLEAN,
                status          VARCHAR DEFAULT 'pending',
                PRIMARY KEY (window_start, predicted_at)
            )
        """)

        # -- pipeline_heartbeat (service health tracking)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_heartbeat (
                service_name    VARCHAR PRIMARY KEY,
                last_heartbeat  TIMESTAMPTZ NOT NULL,
                last_window_key TIMESTAMPTZ,
                status          VARCHAR DEFAULT 'alive',
                details_json    VARCHAR
            )
        """)

        # -- pm_market_prices (Polymarket intra-window price snapshots)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pm_market_prices (
                snapshot_time   TIMESTAMPTZ,
                window_start    TIMESTAMPTZ,
                slug            VARCHAR,
                up_price        DOUBLE,
                down_price      DOUBLE,
                up_best_bid     DOUBLE,
                up_best_ask     DOUBLE,
                down_best_bid   DOUBLE,
                down_best_ask   DOUBLE,
                PRIMARY KEY (snapshot_time, window_start)
            )
        """)

        # -- features_latest: add data_through columns (idempotent)
        for col in ("data_through_1m", "data_through_30m", "data_through_4h"):
            try:
                con.execute(
                    f"ALTER TABLE features_latest ADD COLUMN {col} TIMESTAMPTZ"
                )
            except duckdb.CatalogException:
                pass  # column already exists

        # -- model_runs: add new columns for iteration platform (idempotent)
        _new_model_run_cols = {
            "feature_set": "JSON",
            "parent_run_id": "VARCHAR",
            "tags": "VARCHAR",
            "loss_function": "VARCHAR",
            "eval_metric": "VARCHAR",
            "train_auc": "DOUBLE",
            "overfit_train_cv_gap": "DOUBLE",
            "overfit_cv_ho_gap": "DOUBLE",
            "cv_fold_std": "DOUBLE",
            "ho_auc": "DOUBLE",
        }
        for col, dtype in _new_model_run_cols.items():
            try:
                con.execute(
                    f"ALTER TABLE model_runs ADD COLUMN {col} {dtype}"
                )
            except duckdb.CatalogException:
                pass  # column already exists

        # -- Auto-import parquet files into empty tables -------------------

        # Helper: get column names for a table
        def _table_cols(table: str) -> list[str]:
            rows = con.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{table}' ORDER BY ordinal_position"
            ).fetchall()
            return [r[0] for r in rows]

        # klines_1m
        count = con.execute("SELECT count(*) FROM klines_1m").fetchone()[0]
        if count == 0 and PARQUET_FILE.exists():
            cols = ", ".join(_table_cols("klines_1m"))
            con.execute(
                f"INSERT OR IGNORE INTO klines_1m "
                f"SELECT {cols} FROM read_parquet(?)",
                [str(PARQUET_FILE)],
            )

        # futures tables
        for tbl, pq_path in _FUTURES_TABLES.items():
            count = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            if count == 0 and pq_path.exists():
                cols = ", ".join(_table_cols(tbl))
                con.execute(
                    f"INSERT OR IGNORE INTO {tbl} "
                    f"SELECT {cols} FROM read_parquet(?)",
                    [str(pq_path)],
                )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Market data (klines)
# ---------------------------------------------------------------------------

def get_latest_kline_time() -> datetime | None:
    """Return the most recent open_time in klines_1m, or None if empty."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(open_time) FROM klines_1m"
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_klines(df: pd.DataFrame) -> int:
    """Insert kline rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM klines_1m").fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO klines_1m SELECT * FROM df"
        )
        after = con.execute("SELECT count(*) FROM klines_1m").fetchone()[0]
        return after - before
    finally:
        con.close()


def get_latest_kline_30m_time() -> datetime | None:
    """Return the most recent open_time in klines_30m, or None if empty."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(open_time) FROM klines_30m"
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_klines_30m(df: pd.DataFrame) -> int:
    """Insert 30m kline rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM klines_30m").fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO klines_30m SELECT * FROM df"
        )
        after = con.execute("SELECT count(*) FROM klines_30m").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_klines_30m(
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Read 30m klines, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses: list[str] = []
        params: list = []
        if start is not None:
            clauses.append("open_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("open_time <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM klines_30m{where} ORDER BY open_time", params
        ).fetchdf()
    finally:
        con.close()


def get_latest_kline_4h_time() -> datetime | None:
    """Return the most recent open_time in klines_4h, or None if empty."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(open_time) FROM klines_4h"
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_klines_4h(df: pd.DataFrame) -> int:
    """Insert 4h kline rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM klines_4h").fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO klines_4h SELECT * FROM df"
        )
        after = con.execute("SELECT count(*) FROM klines_4h").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_klines_4h(
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Read 4h klines, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses: list[str] = []
        params: list = []
        if start is not None:
            clauses.append("open_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("open_time <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM klines_4h{where} ORDER BY open_time", params
        ).fetchdf()
    finally:
        con.close()


def get_latest_eth_kline_time() -> datetime | None:
    """Return the most recent open_time in eth_klines_1m, or None if empty."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(open_time) FROM eth_klines_1m"
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_eth_klines(df: pd.DataFrame) -> int:
    """Insert ETH 1m kline rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM eth_klines_1m").fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO eth_klines_1m SELECT * FROM df"
        )
        after = con.execute("SELECT count(*) FROM eth_klines_1m").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_eth_klines(
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Read ETH 1m klines, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses: list[str] = []
        params: list = []
        if start is not None:
            clauses.append("open_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("open_time <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM eth_klines_1m{where} ORDER BY open_time", params
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Coinbase klines
# ---------------------------------------------------------------------------

def get_latest_coinbase_kline_time() -> datetime | None:
    """Return the most recent open_time in coinbase_klines_1m, or None if empty."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(open_time) FROM coinbase_klines_1m"
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_coinbase_klines(df: pd.DataFrame) -> int:
    """Insert Coinbase 1m kline rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    # Ensure column order matches DB schema
    col_order = ["open_time", "open", "high", "low", "close", "volume"]
    df = df[col_order]
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM coinbase_klines_1m").fetchone()[0]
        con.execute(
            "INSERT OR IGNORE INTO coinbase_klines_1m SELECT * FROM df"
        )
        after = con.execute("SELECT count(*) FROM coinbase_klines_1m").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_coinbase_klines(
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Read Coinbase 1m klines, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses: list[str] = []
        params: list = []
        if start is not None:
            clauses.append("open_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("open_time <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM coinbase_klines_1m{where} ORDER BY open_time", params
        ).fetchdf()
    finally:
        con.close()


def read_klines(
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Read klines, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses: list[str] = []
        params: list = []
        if start is not None:
            clauses.append("open_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("open_time <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM klines_1m{where} ORDER BY open_time", params
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Futures data (generic by table name)
# ---------------------------------------------------------------------------

def _validate_futures_table(table: str) -> None:
    if table not in _VALID_FUTURES_TABLES:
        raise ValueError(
            f"Invalid futures table '{table}'. "
            f"Must be one of: {sorted(_VALID_FUTURES_TABLES)}"
        )


def get_latest_futures_time(table: str) -> datetime | None:
    """Return the most recent datetime in the given futures table."""
    _validate_futures_table(table)
    con = get_connection(read_only=True)
    try:
        row = con.execute(f"SELECT max(datetime) FROM {table}").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_futures(table: str, df: pd.DataFrame) -> int:
    """Insert futures rows, ignoring duplicates. Returns count of new rows."""
    _validate_futures_table(table)
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        con.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM df")
        after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_futures(table: str) -> pd.DataFrame:
    """Read all rows from a futures table, ordered by datetime."""
    _validate_futures_table(table)
    con = get_connection(read_only=True)
    try:
        return con.execute(
            f"SELECT * FROM {table} ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Hyperliquid klines + funding
# ---------------------------------------------------------------------------

def insert_hl_klines_1m(df: pd.DataFrame) -> int:
    """Insert Hyperliquid 1m candle rows, ignoring duplicates."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_klines_1m").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_klines_1m SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_klines_1m").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_klines_1m() -> pd.DataFrame:
    """Read all Hyperliquid 1m candles, ordered by open_time."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_klines_1m ORDER BY open_time"
        ).fetchdf()
    finally:
        con.close()


def get_latest_hl_kline_1m_time() -> datetime | None:
    """Return the most recent open_time in the hl_klines_1m table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(open_time) FROM hl_klines_1m").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_hl_klines(df: pd.DataFrame) -> int:
    """Insert Hyperliquid 5m candle rows, ignoring duplicates."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_klines_5m").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_klines_5m SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_klines_5m").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_klines() -> pd.DataFrame:
    """Read all Hyperliquid 1m candles, ordered by open_time."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_klines_5m ORDER BY open_time"
        ).fetchdf()
    finally:
        con.close()


def get_latest_hl_kline_time() -> datetime | None:
    """Return the most recent open_time in the hl_klines_5m table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(open_time) FROM hl_klines_5m").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_hl_klines_1h(df: pd.DataFrame) -> int:
    """Insert Hyperliquid 1h candle rows, ignoring duplicates."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_klines_1h").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_klines_1h SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_klines_1h").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_klines_1h() -> pd.DataFrame:
    """Read all Hyperliquid 1h candles, ordered by open_time."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_klines_1h ORDER BY open_time"
        ).fetchdf()
    finally:
        con.close()


def get_latest_hl_kline_1h_time() -> datetime | None:
    """Return the most recent open_time in the hl_klines_1h table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(open_time) FROM hl_klines_1h").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_hl_funding(df: pd.DataFrame) -> int:
    """Insert Hyperliquid funding rows, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_funding_rate").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_funding_rate SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_funding_rate").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_funding() -> pd.DataFrame:
    """Read all Hyperliquid funding rate rows, ordered by datetime."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_funding_rate ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


def get_latest_hl_funding_time() -> datetime | None:
    """Return the most recent datetime in the hl_funding_rate table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(datetime) FROM hl_funding_rate").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def insert_hl_asset_ctx(row: dict) -> int:
    """Insert a single Hyperliquid asset context snapshot. Returns 1 if inserted."""
    df = pd.DataFrame([row])  # noqa: F841 — used by DuckDB
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_asset_ctx").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_asset_ctx SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_asset_ctx").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_asset_ctx() -> pd.DataFrame:
    """Read all Hyperliquid asset context snapshots, ordered by datetime."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_asset_ctx ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


def insert_hl_orderbook(row: dict) -> int:
    """Insert a single Hyperliquid orderbook snapshot. Returns 1 if inserted."""
    df = pd.DataFrame([row])  # noqa: F841 — used by DuckDB
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_orderbook").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_orderbook SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_orderbook").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_orderbook() -> pd.DataFrame:
    """Read all Hyperliquid orderbook snapshots, ordered by datetime."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_orderbook ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


def insert_hl_liquidations(rows: list[dict]) -> int:
    """Insert Hyperliquid liquidation events, ignoring duplicates."""
    if not rows:
        return 0
    df = pd.DataFrame(rows)  # noqa: F841 — used by DuckDB
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_liquidations").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_liquidations SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_liquidations").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_liquidations() -> pd.DataFrame:
    """Read all Hyperliquid liquidation events, ordered by datetime."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_liquidations ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


def insert_hl_predicted_fundings(row: dict) -> int:
    """Insert a predicted fundings snapshot. Returns 1 if inserted."""
    # Ensure column order matches table schema
    cols = ["datetime", "hl_rate", "hl_next_time", "bin_rate", "bybit_rate"]
    df = pd.DataFrame([{c: row.get(c) for c in cols}])  # noqa: F841 — used by DuckDB
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM hl_predicted_fundings").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO hl_predicted_fundings SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM hl_predicted_fundings").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_hl_predicted_fundings() -> pd.DataFrame:
    """Read all predicted funding snapshots, ordered by datetime."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM hl_predicted_fundings ORDER BY datetime"
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Liquidations
# ---------------------------------------------------------------------------

def insert_liquidations(df: pd.DataFrame) -> int:
    """Insert liquidation events, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM liquidations").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO liquidations SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM liquidations").fetchone()[0]
        return after - before
    finally:
        con.close()


def get_latest_liquidation_time() -> datetime | None:
    """Return the most recent trade_time in the liquidations table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(trade_time) FROM liquidations").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def read_liquidations(start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Read liquidation events, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses = []
        params = []
        if start:
            clauses.append("trade_time >= ?")
            params.append(start)
        if end:
            clauses.append("trade_time <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM liquidations {where} ORDER BY trade_time", params
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Orderbook snapshots
# ---------------------------------------------------------------------------

def insert_orderbook_snapshots(df: pd.DataFrame) -> int:
    """Insert orderbook snapshots, ignoring duplicates. Returns count of new rows."""
    if df.empty:
        return 0
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM orderbook_snapshots").fetchone()[0]
        con.execute("INSERT OR IGNORE INTO orderbook_snapshots SELECT * FROM df")
        after = con.execute("SELECT count(*) FROM orderbook_snapshots").fetchone()[0]
        return after - before
    finally:
        con.close()


def get_latest_orderbook_time() -> datetime | None:
    """Return the most recent snapshot_time in the orderbook_snapshots table."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("SELECT max(snapshot_time) FROM orderbook_snapshots").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def read_orderbook_snapshots(start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Read orderbook snapshots, optionally filtered by time range."""
    con = get_connection(read_only=True)
    try:
        clauses = []
        params = []
        if start:
            clauses.append("snapshot_time >= ?")
            params.append(start)
        if end:
            clauses.append("snapshot_time <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return con.execute(
            f"SELECT * FROM orderbook_snapshots {where} ORDER BY snapshot_time", params
        ).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Model runs
# ---------------------------------------------------------------------------

def insert_model_run(run: dict, run_id: str | None = None) -> str:
    """Insert a model run record. Returns the generated run_id.

    Uses named-column INSERT so new columns added via ALTER TABLE
    are written correctly (not silently left NULL).
    """
    if run_id is None:
        run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    con = get_connection()
    try:
        # Ensure best_params is JSON string
        best_params = run.get("best_params")
        if best_params is not None and not isinstance(best_params, str):
            best_params = json.dumps(best_params)

        # Ensure feature_set is JSON string
        feature_set = run.get("feature_set")
        if feature_set is not None and not isinstance(feature_set, str):
            feature_set = json.dumps(feature_set)

        # Build row dict with all known columns
        row = {
            "run_id": run_id,
            "created_at": run.get("created_at", datetime.now(UTC)),
            "data_start": run.get("data_start"),
            "data_end": run.get("data_end"),
            "n_samples": run.get("n_samples"),
            "n_features": run.get("n_features"),
            "optuna_trials": run.get("optuna_trials"),
            "best_cv_auc": run.get("best_cv_auc"),
            "best_params": best_params,
            "cv_mean_auc": run.get("cv_mean_auc"),
            "cv_std_auc": run.get("cv_std_auc"),
            "cv_mean_acc": run.get("cv_mean_acc"),
            "cv_mean_brier": run.get("cv_mean_brier"),
            "cv_folds": run.get("cv_folds"),
            "bt_total_trades": run.get("bt_total_trades"),
            "bt_win_rate": run.get("bt_win_rate"),
            "bt_total_pnl": run.get("bt_total_pnl"),
            "bt_max_drawdown": run.get("bt_max_drawdown"),
            "bt_sharpe": run.get("bt_sharpe"),
            "bt_profit_factor": run.get("bt_profit_factor"),
            "model_path": run.get("model_path"),
            "status": run.get("status", "completed"),
            # New iteration-platform columns
            "feature_set": feature_set,
            "parent_run_id": run.get("parent_run_id"),
            "tags": run.get("tags"),
            "loss_function": run.get("loss_function"),
            "eval_metric": run.get("eval_metric"),
            "train_auc": run.get("train_auc"),
            "overfit_train_cv_gap": run.get("overfit_train_cv_gap"),
            "overfit_cv_ho_gap": run.get("overfit_cv_ho_gap"),
            "cv_fold_std": run.get("cv_fold_std"),
            "ho_auc": run.get("ho_auc"),
        }

        # Only INSERT columns that have non-None values
        filled = {k: v for k, v in row.items() if v is not None}
        columns = ", ".join(filled.keys())
        placeholders = ", ".join(["?"] * len(filled))

        con.execute(
            f"INSERT INTO model_runs ({columns}) VALUES ({placeholders})",
            list(filled.values()),
        )
        return run_id
    finally:
        con.close()


def insert_feature_importance(run_id: str, importance: pd.Series) -> None:
    """Insert feature importance scores for a run.

    Args:
        run_id: The model run identifier.
        importance: Series with feature names as index, importance as values.
    """
    if importance.empty:
        return
    df = (
        importance.reset_index()
        .rename(columns={"index": "feature", 0: "importance"})
    )
    # Handle case where Series already has a name
    if "importance" not in df.columns:
        df.columns = ["feature", "importance"]
    df = df.sort_values("importance", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    df["run_id"] = run_id
    df = df[["run_id", "feature", "importance", "rank"]]

    con = get_connection()
    try:
        con.execute(
            "INSERT OR IGNORE INTO feature_importance SELECT * FROM df"
        )
    finally:
        con.close()


def insert_cv_predictions(run_id: str, preds: pd.DataFrame) -> None:
    """Insert cross-validation predictions for a run."""
    if preds.empty:
        return
    df = preds.copy()
    df["run_id"] = run_id
    df = df[["run_id", "window_start", "fold", "label", "y_prob",
             "open_price", "close_price"]]

    con = get_connection()
    try:
        con.execute("INSERT INTO cv_predictions SELECT * FROM df")
    finally:
        con.close()


def insert_backtest_trades(run_id: str, trades: pd.DataFrame) -> None:
    """Insert backtest trade records for a run."""
    if trades.empty:
        return
    df = trades.copy()
    df["run_id"] = run_id
    df = df[["run_id", "window_start", "direction", "prob", "correct",
             "buy_price", "fee_usd", "pnl"]]

    con = get_connection()
    try:
        con.execute("INSERT INTO backtest_trades SELECT * FROM df")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Query helpers (dashboard)
# ---------------------------------------------------------------------------

def get_all_runs() -> pd.DataFrame:
    """Return all model runs, most recent first."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM model_runs ORDER BY created_at DESC"
        ).fetchdf()
    finally:
        con.close()


def get_run_detail(run_id: str) -> dict:
    """Return a single model run as a dict, or empty dict if not found."""
    con = get_connection(read_only=True)
    try:
        df = con.execute(
            "SELECT * FROM model_runs WHERE run_id = ?", [run_id]
        ).fetchdf()
        if df.empty:
            return {}
        return df.iloc[0].to_dict()
    finally:
        con.close()


def get_feature_importance_for_run(run_id: str) -> pd.DataFrame:
    """Return feature importance for a run, ordered by rank."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM feature_importance "
            "WHERE run_id = ? ORDER BY rank",
            [run_id],
        ).fetchdf()
    finally:
        con.close()


def get_cv_predictions_for_run(run_id: str) -> pd.DataFrame:
    """Return CV predictions for a run, ordered by window_start."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM cv_predictions "
            "WHERE run_id = ? ORDER BY window_start",
            [run_id],
        ).fetchdf()
    finally:
        con.close()


def get_backtest_trades_for_run(run_id: str) -> pd.DataFrame:
    """Return backtest trades for a run, ordered by window_start."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM backtest_trades "
            "WHERE run_id = ? ORDER BY window_start",
            [run_id],
        ).fetchdf()
    finally:
        con.close()


def get_latest_run_id() -> str | None:
    """Return the run_id of the most recent model run, or None."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT run_id FROM model_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Live predictions
# ---------------------------------------------------------------------------

def insert_live_prediction(row: dict) -> None:
    """Insert a pending live prediction.

    For challenger models, the predicted_at is offset by microseconds to avoid
    PK conflict with the champion's prediction on the same (window_start, predicted_at).
    """
    _ensure_challenger_tables()  # ensure model_id column exists
    predicted_at = row["predicted_at"]
    model_id = row.get("model_id", "champion")
    # Challenger rows: offset predicted_at by model index to avoid PK clash
    if row.get("is_challenger"):
        offset_us = row.get("challenger_index", 1)
        predicted_at = predicted_at + pd.Timedelta(microseconds=offset_us)
    con = get_connection()
    try:
        con.execute(
            """INSERT OR IGNORE INTO live_predictions
               (window_start, predicted_at, prob_up, direction, confidence,
                ev, market_price, bet, bet_size, model_id, order_id,
                kelly_f, decay, market_start, market_end, bankroll, shap_top, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                row["window_start"],
                predicted_at,
                row["prob_up"],
                row["direction"],
                row["confidence"],
                row["ev"],
                row["market_price"],
                row.get("bet", False),
                row.get("bet_size"),
                model_id,
                row.get("order_id"),
                row.get("kelly_f"),
                row.get("decay"),
                row.get("market_start"),
                row.get("market_end"),
                row.get("bankroll"),
                row.get("shap_top"),
                "placed" if row.get("order_id") else "pending",
            ],
        )
    finally:
        con.close()


def get_cumulative_pnl() -> float:
    """Calculate cumulative PnL from all resolved bets.

    PnL per bet:
      win:  shares * $1 - bet_size - fee
      loss: -bet_size - fee
    where fee = bet_size * price * (1 - price) * 0.25
    """
    con = get_connection(read_only=True)
    try:
        rows = con.execute("""
            SELECT bet_size, market_price, direction, correct
            FROM live_predictions
            WHERE bet = true AND status = 'resolved' AND correct IS NOT NULL
        """).fetchall()
    finally:
        con.close()

    total = 0.0
    for bet_size, mkt_price, direction, correct in rows:
        if bet_size is None or mkt_price is None:
            continue
        price = mkt_price if direction == "up" else 1 - mkt_price
        fee = bet_size * price * (1 - price) * 0.25
        if correct:
            shares = bet_size / price
            total += shares * 1.0 - bet_size - fee
        else:
            total -= bet_size + fee
    return total


def get_active_exposure() -> float:
    """Sum of bet_size for all unresolved bets (money locked in positions)."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(bet_size), 0) FROM live_predictions "
            "WHERE bet = true AND status IN ('pending', 'placed')"
        ).fetchone()
        return float(row[0])
    finally:
        con.close()


def get_best_ev_for_window(window_start: datetime) -> float | None:
    """Return the highest EV among existing bets for this window, or None."""
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT max(ev) FROM live_predictions WHERE window_start = ?",
            [window_start],
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def resolve_live_prediction(window_start: datetime, btc_open: float, btc_close: float, actual: str | None = None) -> None:
    """Fill in the actual outcome for all pending predictions in this window."""
    if actual is None:
        actual = "up" if btc_close >= btc_open else "down"
    con = get_connection()
    try:
        # Update all predictions for this window
        con.execute(
            """UPDATE live_predictions
               SET btc_open = ?, btc_close = ?, actual = ?,
                   correct = (direction = ?), status = 'resolved'
               WHERE window_start = ? AND status IN ('pending', 'placed')""",
            [btc_open, btc_close, actual, actual, window_start],
        )
    finally:
        con.close()


def get_live_predictions(limit: int = 200) -> pd.DataFrame:
    """Return recent live predictions, newest first."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM live_predictions ORDER BY window_start DESC LIMIT ?",
            [limit],
        ).fetchdf()
    finally:
        con.close()


def get_live_stats() -> dict:
    """Return summary stats for all predictions and bets separately."""
    con = get_connection(read_only=True)
    try:
        row = con.execute("""
            SELECT
                count(*) as total,
                count(*) FILTER (WHERE status = 'pending') as pending,
                -- All predictions accuracy
                count(*) FILTER (WHERE correct = true) as pred_correct,
                count(*) FILTER (WHERE correct = false) as pred_wrong,
                -- Bets only
                count(*) FILTER (WHERE bet = true) as bet_total,
                count(*) FILTER (WHERE bet = true AND correct = true) as bet_wins,
                count(*) FILTER (WHERE bet = true AND correct = false) as bet_losses
            FROM live_predictions
        """).fetchone()
        pred_resolved = row[2] + row[3]
        bet_resolved = row[5] + row[6]
        return {
            "total": row[0],
            "pending": row[1],
            "pred_correct": row[2],
            "pred_wrong": row[3],
            "pred_accuracy": row[2] / pred_resolved if pred_resolved > 0 else None,
            "bet_total": row[4],
            "bet_wins": row[5],
            "bet_losses": row[6],
            "bet_win_rate": row[5] / bet_resolved if bet_resolved > 0 else None,
            # Keep legacy keys for _resolve_pending running stats
            "wins": row[5],
            "losses": row[6],
            "win_rate": row[5] / bet_resolved if bet_resolved > 0 else None,
        }
    finally:
        con.close()


def get_pending_bets() -> pd.DataFrame:
    """Return pending bets (bet=true, status='pending'), oldest first."""
    con = get_connection(read_only=True)
    try:
        return con.execute(
            """SELECT * FROM live_predictions
               WHERE bet = true AND status = 'pending'
               ORDER BY window_start ASC, predicted_at ASC"""
        ).fetchdf()
    finally:
        con.close()


def mark_bet_placed(window_start: datetime, predicted_at: datetime, order_id: str) -> None:
    """Mark a pending bet as placed (order submitted to CLOB)."""
    con = get_connection()
    try:
        con.execute(
            """UPDATE live_predictions
               SET status = 'placed'
               WHERE window_start = ? AND predicted_at = ?""",
            [window_start, predicted_at],
        )
    finally:
        con.close()


def insert_features(
    window_start: datetime,
    features: dict,
    *,
    data_through_1m: datetime | None = None,
    data_through_30m: datetime | None = None,
    data_through_4h: datetime | None = None,
) -> None:
    """Insert or update precomputed features for a window."""
    con = get_connection()
    try:
        con.execute(
            """INSERT OR REPLACE INTO features_latest
               (window_start, computed_at, features_json,
                data_through_1m, data_through_30m, data_through_4h)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [window_start, datetime.now(UTC), json.dumps(features),
             data_through_1m, data_through_30m, data_through_4h],
        )
    finally:
        con.close()


def get_latest_features(n: int = 1) -> list[dict]:
    """Return the N most recent precomputed feature rows.

    Each dict has keys: window_start, computed_at, and all feature columns.
    """
    con = get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT window_start, computed_at, features_json "
            "FROM features_latest ORDER BY window_start DESC LIMIT ?",
            [n],
        ).fetchall()
        result = []
        for ws, ca, fj in rows:
            entry = json.loads(fj)
            entry["window_start"] = ws
            entry["computed_at"] = ca
            result.append(entry)
        return result
    finally:
        con.close()


def get_features_for_window(
    window_start: datetime,
    min_computed_at: datetime | None = None,
    min_data_through_1m: datetime | None = None,
) -> tuple[dict | None, dict | None]:
    """Return precomputed features for a specific window.

    Returns:
        (features_dict, meta_dict) or (None, None).
        meta_dict contains: computed_at, data_through_1m, data_through_30m, data_through_4h.

    Args:
        window_start: The 5-minute window boundary.
        min_computed_at: Only return features computed at or after this timestamp.
        min_data_through_1m: Only return features whose 1m data covers at least
            this timestamp.  This is the primary freshness gate — it ensures the
            features were computed from klines that include the latest closed candle.
    """
    con = get_connection(read_only=True)
    try:
        conditions = ["window_start = ?"]
        params: list = [window_start]
        if min_computed_at is not None:
            conditions.append("computed_at >= ?")
            params.append(min_computed_at)
        if min_data_through_1m is not None:
            conditions.append("data_through_1m >= ?")
            params.append(min_data_through_1m)

        where = " AND ".join(conditions)
        row = con.execute(
            f"SELECT features_json, computed_at, "
            f"       data_through_1m, data_through_30m, data_through_4h "
            f"FROM features_latest WHERE {where}",
            params,
        ).fetchone()
        if row is None:
            return None, None
        meta = {
            "computed_at": row[1],
            "data_through_1m": row[2],
            "data_through_30m": row[3],
            "data_through_4h": row[4],
        }
        return json.loads(row[0]), meta
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Pipeline heartbeat
# ---------------------------------------------------------------------------

def upsert_heartbeat(
    service_name: str,
    *,
    window_key: datetime | None = None,
    details: dict | None = None,
) -> None:
    """Record a service heartbeat (alive signal)."""
    con = get_connection()
    try:
        con.execute(
            """INSERT OR REPLACE INTO pipeline_heartbeat
               (service_name, last_heartbeat, last_window_key, status, details_json)
               VALUES (?, ?, ?, 'alive', ?)""",
            [service_name, datetime.now(UTC), window_key,
             json.dumps(details) if details else None],
        )
    finally:
        con.close()


def check_service_health(
    service_name: str, *, max_stale_seconds: int = 180
) -> tuple[bool, str]:
    """Check whether a service is healthy (heartbeat within threshold).

    Returns:
        (healthy, reason)
    """
    con = get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT last_heartbeat, status FROM pipeline_heartbeat "
            "WHERE service_name = ?",
            [service_name],
        ).fetchone()
        if row is None:
            return False, f"{service_name}: no heartbeat record"
        last_hb = row[0]
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - last_hb).total_seconds()
        if age > max_stale_seconds:
            return False, f"{service_name}: heartbeat {age:.0f}s stale (limit {max_stale_seconds}s)"
        return True, "ok"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Polymarket intra-window price snapshots
# ---------------------------------------------------------------------------

def insert_pm_market_price(row: dict) -> int:
    """Insert a Polymarket market price snapshot. Returns 1 if inserted, 0 if dup."""
    con = get_connection()
    try:
        before = con.execute("SELECT count(*) FROM pm_market_prices").fetchone()[0]
        con.execute(
            """INSERT OR IGNORE INTO pm_market_prices
               (snapshot_time, window_start, slug, up_price, down_price,
                up_best_bid, up_best_ask, down_best_bid, down_best_ask)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                row["snapshot_time"],
                row["window_start"],
                row["slug"],
                row["up_price"],
                row["down_price"],
                row.get("up_best_bid", 0.0),
                row.get("up_best_ask", 0.0),
                row.get("down_best_bid", 0.0),
                row.get("down_best_ask", 0.0),
            ],
        )
        after = con.execute("SELECT count(*) FROM pm_market_prices").fetchone()[0]
        return after - before
    finally:
        con.close()


def read_pm_market_prices(
    window_start: datetime | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    """Read Polymarket price snapshots, optionally filtered by window or time range."""
    con = get_connection(read_only=True)
    try:
        wheres = []
        params: list = []
        if window_start is not None:
            wheres.append("window_start = ?")
            params.append(window_start)
        if since is not None:
            wheres.append("snapshot_time >= ?")
            params.append(since)
        where_clause = " AND ".join(wheres)
        if where_clause:
            where_clause = f"WHERE {where_clause}"
        return con.execute(
            f"SELECT * FROM pm_market_prices {where_clause} ORDER BY snapshot_time",
            params,
        ).fetchdf()
    finally:
        con.close()


def get_data_coverage() -> dict:
    """Return row count, min and max timestamps for every data table.

    Returns:
        {table_name: {"count": int, "min_time": datetime, "max_time": datetime}}
    """
    tables_time_col = {
        "klines_1m": "open_time",
        "klines_30m": "open_time",
        "klines_4h": "open_time",
        "eth_klines_1m": "open_time",
        "coinbase_klines_1m": "open_time",
        **{tbl: "datetime" for tbl in _FUTURES_TABLES},
    }
    result: dict = {}
    con = get_connection(read_only=True)
    try:
        for tbl, col in tables_time_col.items():
            row = con.execute(
                f"SELECT count(*), min({col}), max({col}) FROM {tbl}"
            ).fetchone()
            result[tbl] = {
                "count": row[0],
                "min_time": row[1],
                "max_time": row[2],
            }
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Challenger model management
# ---------------------------------------------------------------------------

def _ensure_challenger_tables() -> None:
    """Create challenger tables if they don't exist."""
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS challenger_models (
                model_id        VARCHAR PRIMARY KEY,
                model_type      VARCHAR,
                model_path      VARCHAR,
                features_path   VARCHAR,
                created_at      TIMESTAMPTZ,
                arena_run_id    VARCHAR,
                status          VARCHAR DEFAULT 'active'
            )
        """)
        # Add missing columns to live_predictions
        try:
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'live_predictions'"
            ).fetchall()]
            if "model_id" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN model_id VARCHAR DEFAULT 'champion'"
                )
            if "order_id" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN order_id VARCHAR"
                )
            if "kelly_f" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN kelly_f DOUBLE"
                )
            if "decay" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN decay DOUBLE"
                )
            if "market_start" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN market_start TIMESTAMPTZ"
                )
            if "market_end" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN market_end TIMESTAMPTZ"
                )
            if "bankroll" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN bankroll DOUBLE"
                )
            if "shap_top" not in cols:
                con.execute(
                    "ALTER TABLE live_predictions ADD COLUMN shap_top VARCHAR"
                )
        except Exception:
            pass
    finally:
        con.close()


def register_challenger(
    model_id: str,
    model_type: str,
    model_path: str,
    features_path: str,
    arena_run_id: str = "",
) -> None:
    """Register a new challenger model."""
    _ensure_challenger_tables()
    con = get_connection()
    try:
        con.execute(
            """INSERT OR REPLACE INTO challenger_models
               (model_id, model_type, model_path, features_path, created_at, arena_run_id, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            [model_id, model_type, model_path, features_path,
             datetime.now(UTC), arena_run_id],
        )
    finally:
        con.close()


def get_active_challengers() -> pd.DataFrame:
    """Return all active challenger models."""
    _ensure_challenger_tables()
    con = get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT * FROM challenger_models WHERE status = 'active' ORDER BY created_at"
        ).fetchdf()
    finally:
        con.close()


def deactivate_challenger(model_id: str) -> None:
    """Deactivate a challenger model."""
    con = get_connection()
    try:
        con.execute(
            "UPDATE challenger_models SET status = 'inactive' WHERE model_id = ?",
            [model_id],
        )
    finally:
        con.close()


def promote_challenger(model_id: str) -> None:
    """Mark a challenger as promoted."""
    con = get_connection()
    try:
        con.execute(
            "UPDATE challenger_models SET status = 'promoted' WHERE model_id = ?",
            [model_id],
        )
    finally:
        con.close()


def get_model_comparison() -> pd.DataFrame:
    """Compare champion vs all challengers on resolved live predictions."""
    _ensure_challenger_tables()
    con = get_connection(read_only=True)
    try:
        return con.execute("""
            SELECT
                COALESCE(model_id, 'champion') as model_id,
                count(*) as total,
                count(*) FILTER (WHERE status = 'resolved') as resolved,
                count(*) FILTER (WHERE correct = true) as correct,
                count(*) FILTER (WHERE correct = false) as wrong,
                ROUND(count(*) FILTER (WHERE correct = true) * 100.0 /
                      NULLIF(count(*) FILTER (WHERE correct = true) +
                             count(*) FILTER (WHERE correct = false), 0), 1) as accuracy_pct,
                count(*) FILTER (WHERE bet = true) as bets,
                count(*) FILTER (WHERE bet = true AND correct = true) as bet_wins,
                count(*) FILTER (WHERE bet = true AND correct = false) as bet_losses,
                ROUND(count(*) FILTER (WHERE bet = true AND correct = true) * 100.0 /
                      NULLIF(count(*) FILTER (WHERE bet = true AND correct = true) +
                             count(*) FILTER (WHERE bet = true AND correct = false), 0), 1) as bet_wr_pct
            FROM live_predictions
            GROUP BY model_id
            ORDER BY accuracy_pct DESC NULLS LAST
        """).fetchdf()
    finally:
        con.close()
