from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


def safety_database_path(project_dir: Path) -> Path:
    configured = Path(os.getenv('OPTION_SAFETY_DB', 'outputs/options_safety.sqlite3'))
    return configured if configured.is_absolute() else project_dir / configured


def record_iv(database: Path, symbol: str, observed_date: date, atm_iv_30d: float) -> None:
    value = float(atm_iv_30d)
    ticker = str(symbol).strip().upper()
    if not ticker or not math.isfinite(value) or value <= 0:
        raise ValueError('A ticker and positive finite implied volatility are required')
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS iv_history '
            '(symbol TEXT NOT NULL, date TEXT NOT NULL, atm_iv_30d REAL NOT NULL, '
            'PRIMARY KEY (symbol, date))'
        )
        connection.execute(
            'INSERT INTO iv_history (symbol, date, atm_iv_30d) VALUES (?, ?, ?) '
            'ON CONFLICT(symbol, date) DO UPDATE SET atm_iv_30d = excluded.atm_iv_30d',
            (ticker, observed_date.isoformat(), value),
        )


def calculate_iv_rank(database: Path, symbol: str, current_iv: float, as_of: date) -> tuple[float | None, int]:
    value = float(current_iv)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Current implied volatility must be positive and finite')
    if not database.is_file():
        return None, 0
    with closing(sqlite3.connect(database)) as connection:
        try:
            rows = connection.execute(
                'SELECT atm_iv_30d FROM iv_history WHERE symbol = ? AND date >= ? AND date < ? ORDER BY date',
                (str(symbol).strip().upper(), (as_of - timedelta(days=364)).isoformat(), as_of.isoformat()),
            ).fetchall()
        except sqlite3.OperationalError:
            return None, 0
    values = [float(row[0]) for row in rows if row[0] is not None and math.isfinite(float(row[0])) and float(row[0]) > 0]
    if len(values) < 20 or min(values) == max(values):
        return None, len(values)
    return (value - min(values)) / (max(values) - min(values)) * 100.0, len(values)


def realized_volatility_proxy_rank(closes: pd.Series) -> float | None:
    prices = pd.to_numeric(closes, errors='coerce')
    prices = prices.where(prices > 0)
    realized = (prices.pct_change(fill_method=None).rolling(20).std() * math.sqrt(252)).dropna().tail(252)
    realized = realized[realized.map(math.isfinite)]
    if len(realized) < 20 or float(realized.min()) == float(realized.max()):
        return None
    return (float(realized.iloc[-1]) - float(realized.min())) / (float(realized.max()) - float(realized.min())) * 100.0