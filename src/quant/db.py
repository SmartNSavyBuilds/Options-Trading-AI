from __future__ import annotations

import os
import sqlite3
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 3

_SCHEMA_V1 = (
    '''CREATE TABLE IF NOT EXISTS option_chain_snapshots (
        source TEXT NOT NULL,
        interval TEXT NOT NULL CHECK (interval IN ('eod', '1m', 'tick')),
        ts_ms INTEGER NOT NULL,
        underlying TEXT,
        contract TEXT NOT NULL,
        expiry TEXT,
        strike REAL,
        right TEXT CHECK (right IS NULL OR right IN ('C', 'P')),
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        vwap REAL,
        trade_count INTEGER,
        open_interest INTEGER,
        bid REAL,
        ask REAL,
        mid REAL,
        spread REAL,
        spread_pct REAL,
        bid_size REAL,
        ask_size REAL,
        bid_exchange TEXT,
        ask_exchange TEXT,
        quote_ts_ms INTEGER,
        underlying_px REAL,
        iv REAL,
        delta REAL,
        gamma REAL,
        theta REAL,
        vega REAL,
        rho REAL,
        greeks_source TEXT,
        quality_flags TEXT NOT NULL DEFAULT '[]',
        extra_json TEXT NOT NULL DEFAULT '{}',
        ingested_ts_ms INTEGER NOT NULL,
        PRIMARY KEY (source, interval, contract, ts_ms)
    )''',
    'CREATE INDEX IF NOT EXISTS idx_option_chain_underlying_ts ON option_chain_snapshots (underlying, ts_ms)',
    '''CREATE TABLE IF NOT EXISTS execution_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_order_id TEXT NOT NULL,
        broker_order_id TEXT,
        event TEXT NOT NULL CHECK (event IN ('signal', 'submit', 'replace', 'fill', 'cancel', 'reject')),
        event_ts_ms INTEGER NOT NULL,
        structure TEXT,
        side TEXT,
        qty REAL,
        filled_qty REAL,
        limit_px REAL,
        fill_px REAL,
        combo_bid REAL,
        combo_ask REAL,
        combo_mid REAL,
        combo_spread REAL,
        legs_json TEXT NOT NULL DEFAULT '[]',
        oldest_quote_ts_ms INTEGER,
        quote_lag_ms INTEGER,
        ack_latency_ms INTEGER,
        slippage_bps_vs_signal_mid REAL,
        quality_flags TEXT NOT NULL DEFAULT '[]',
        extra_json TEXT NOT NULL DEFAULT '{}',
        recorded_ts_ms INTEGER NOT NULL,
        UNIQUE (client_order_id, event, event_ts_ms)
    )''',
    'CREATE INDEX IF NOT EXISTS idx_execution_events_order_ts ON execution_events (client_order_id, event_ts_ms)',
    '''CREATE TABLE IF NOT EXISTS iv_observations (
        source TEXT NOT NULL,
        symbol TEXT NOT NULL,
        obs_ts_ms INTEGER NOT NULL,
        tenor_days INTEGER NOT NULL,
        method TEXT NOT NULL,
        iv REAL NOT NULL,
        quality_flags TEXT NOT NULL DEFAULT '[]',
        extra_json TEXT NOT NULL DEFAULT '{}',
        ingested_ts_ms INTEGER NOT NULL,
        PRIMARY KEY (source, symbol, obs_ts_ms, tenor_days, method)
    )''',
    '''CREATE TABLE IF NOT EXISTS vol_surface_fits (
        symbol TEXT NOT NULL,
        as_of_ts_ms INTEGER NOT NULL,
        expiry TEXT NOT NULL,
        model TEXT NOT NULL,
        params_json TEXT NOT NULL,
        rmse REAL,
        arb_violations TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (symbol, as_of_ts_ms, expiry, model)
    )''',
    '''CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id TEXT PRIMARY KEY,
        config_json TEXT NOT NULL,
        code_version TEXT,
        data_window TEXT,
        created_ts_ms INTEGER NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS backtest_trades (
        run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
        fold INTEGER NOT NULL,
        sample TEXT NOT NULL CHECK (sample IN ('IS', 'OOS')),
        entry_ts_ms INTEGER NOT NULL,
        exit_ts_ms INTEGER,
        symbol TEXT NOT NULL,
        structure TEXT,
        pnl REAL,
        max_loss REAL,
        costs REAL,
        PRIMARY KEY (run_id, fold, entry_ts_ms, symbol)
    )''',
    '''CREATE TABLE IF NOT EXISTS edge_reports (
        run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
        metric TEXT NOT NULL,
        value REAL,
        ci_low REAL,
        ci_high REAL,
        PRIMARY KEY (run_id, metric)
    )''',
    '''CREATE TABLE IF NOT EXISTS portfolio_greeks (
        as_of_ts_ms INTEGER PRIMARY KEY,
        delta REAL,
        gamma REAL,
        vega REAL,
        theta REAL,
        beta_weighted_delta REAL,
        inputs_stale INTEGER NOT NULL DEFAULT 0 CHECK (inputs_stale IN (0, 1))
    )''',
)

_SCHEMA_V2 = (
    '''CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
        source TEXT NOT NULL,
        interval TEXT NOT NULL CHECK (interval IN ('eod', '1m', 'tick')),
        underlying TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        cursor TEXT,
        complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
        pages_ingested INTEGER NOT NULL DEFAULT 0,
        records_ingested INTEGER NOT NULL DEFAULT 0,
        records_rejected INTEGER NOT NULL DEFAULT 0,
        updated_ts_ms INTEGER NOT NULL,
        PRIMARY KEY (source, interval, underlying, start_date, end_date)
    )''',
)

_SCHEMA_V3 = (
    '''CREATE TABLE IF NOT EXISTS iv_percentiles (
        source TEXT NOT NULL,
        symbol TEXT NOT NULL,
        obs_ts_ms INTEGER NOT NULL,
        tenor_days INTEGER NOT NULL,
        method TEXT NOT NULL,
        window_size INTEGER NOT NULL,
        observations_used INTEGER NOT NULL,
        percentile REAL NOT NULL CHECK (percentile >= 0 AND percentile <= 100),
        computed_ts_ms INTEGER NOT NULL,
        PRIMARY KEY (source, symbol, obs_ts_ms, tenor_days, method, window_size)
    )''',
    'CREATE INDEX IF NOT EXISTS idx_iv_observations_history ON iv_observations (source, symbol, tenor_days, method, obs_ts_ms)',
)


def quant_database_path(project_dir: Path = PROJECT_DIR) -> Path:
    configured = Path(os.getenv('QUANT_DB', 'outputs/quant.sqlite3'))
    return configured if configured.is_absolute() else project_dir / configured


def connect(database: Path | None = None) -> sqlite3.Connection:
    path = database or quant_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout = 2000')
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA journal_mode = WAL')
    return connection


def init_schema(database: Path | None = None) -> None:
    connection = connect(database)
    try:
        version = int(connection.execute('PRAGMA user_version').fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'Quant database schema version {version} is newer than supported version {SCHEMA_VERSION}')
        if version < 1:
            with connection:
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version = 1')
            version = 1
        if version < 2:
            with connection:
                for statement in _SCHEMA_V2:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version = 2')
            version = 2
        if version < 3:
            with connection:
                for statement in _SCHEMA_V3:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version = 3')
    finally:
        connection.close()