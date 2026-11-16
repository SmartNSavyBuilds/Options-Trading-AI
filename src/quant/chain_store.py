from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .contracts import ChainRecord


_COLUMNS = (
    'source', 'interval', 'ts_ms', 'underlying', 'contract', 'expiry', 'strike', 'right', 'open', 'high', 'low',
    'close', 'volume', 'vwap', 'trade_count', 'open_interest', 'bid', 'ask', 'mid', 'spread', 'spread_pct',
    'bid_size', 'ask_size',
    'bid_exchange', 'ask_exchange', 'quote_ts_ms', 'underlying_px', 'iv', 'delta', 'gamma', 'theta', 'vega',
    'rho', 'greeks_source', 'quality_flags', 'extra_json', 'ingested_ts_ms',
)
_INSERT_SQL = (
    f"INSERT INTO option_chain_snapshots ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
    'ON CONFLICT(source, interval, contract, ts_ms) DO UPDATE SET '
    + ', '.join(f'{column}=excluded.{column}' for column in _COLUMNS if column not in {'source', 'interval', 'contract', 'ts_ms'})
)


def upsert_chain_records(connection: sqlite3.Connection, records: Iterable[ChainRecord]) -> int:
    rows = [record.to_row() for record in records]
    if not rows:
        return 0
    connection.executemany(_INSERT_SQL, [tuple(row[column] for column in _COLUMNS) for row in rows])
    return len(rows)