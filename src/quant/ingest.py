from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .chain_store import upsert_chain_records
from .contracts import ChainRecord, to_epoch_ms, utc_now_ms
from .db import connect, init_schema, quant_database_path
from .providers.base import ChainHistoryProvider


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionSummary:
    pages_ingested: int
    records_ingested: int
    records_rejected: int
    completed_symbols: tuple[str, ...]
    skipped_symbols: tuple[str, ...]


def ingest_history(
    provider: ChainHistoryProvider,
    symbols: list[str] | tuple[str, ...],
    start_date: date,
    end_date: date,
    *,
    interval: str = 'eod',
    database: Path | None = None,
    max_pages_per_symbol: int | None = None,
) -> IngestionSummary:
    if start_date > end_date:
        raise ValueError('start_date must be on or before end_date')
    if interval not in {'eod', '1m', 'tick'}:
        raise ValueError('interval must be eod, 1m, or tick')
    if max_pages_per_symbol is not None and max_pages_per_symbol <= 0:
        raise ValueError('max_pages_per_symbol must be positive')
    source = str(provider.name).strip()
    if not source:
        raise ValueError('Provider name is required')

    target = database or quant_database_path()
    init_schema(target)
    pages_ingested = records_ingested = records_rejected = 0
    completed_symbols: list[str] = []
    skipped_symbols: list[str] = []
    for raw_symbol in symbols:
        underlying = str(raw_symbol).strip().upper()
        if not underlying:
            continue
        checkpoint = _get_checkpoint(target, source, interval, underlying, start_date, end_date)
        if checkpoint is not None and bool(checkpoint['complete']):
            skipped_symbols.append(underlying)
            continue
        cursor = None if checkpoint is None else checkpoint['cursor']
        symbol_pages = 0
        while max_pages_per_symbol is None or symbol_pages < max_pages_per_symbol:
            page = provider.fetch_chain_page(underlying, start_date, end_date, interval, cursor)
            if not isinstance(page.next_cursor, (str, type(None))):
                raise TypeError('Provider next_cursor must be a string or None')
            if page.next_cursor is not None and page.next_cursor == cursor:
                raise RuntimeError(f'Provider cursor did not advance for {underlying}')
            records: list[ChainRecord] = []
            rejected = 0
            for payload in page.records:
                try:
                    normalized = dict(payload)
                    normalized.setdefault('underlying', underlying)
                    record = ChainRecord.from_mapping(normalized, source=source, interval=interval)
                    observed_date = datetime.fromtimestamp(record.ts_ms / 1000, tz=timezone.utc).date()
                    if not start_date <= observed_date <= end_date:
                        raise ValueError('Record timestamp falls outside requested date range')
                    records.append(record)
                except (TypeError, ValueError, OverflowError):
                    rejected += 1
                    logger.warning('Rejected malformed historical chain record for %s', underlying)

            connection = connect(target)
            try:
                with connection:
                    upsert_chain_records(connection, records)
                    connection.execute(
                        '''INSERT INTO ingestion_checkpoints (
                            source, interval, underlying, start_date, end_date, cursor, complete,
                            pages_ingested, records_ingested, records_rejected, updated_ts_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(source, interval, underlying, start_date, end_date) DO UPDATE SET
                            cursor=excluded.cursor,
                            complete=excluded.complete,
                            pages_ingested=ingestion_checkpoints.pages_ingested + 1,
                            records_ingested=ingestion_checkpoints.records_ingested + excluded.records_ingested,
                            records_rejected=ingestion_checkpoints.records_rejected + excluded.records_rejected,
                            updated_ts_ms=excluded.updated_ts_ms''',
                        (
                            source, interval, underlying, start_date.isoformat(), end_date.isoformat(),
                            page.next_cursor, int(page.next_cursor is None), len(records), rejected, utc_now_ms(),
                        ),
                    )
            finally:
                connection.close()

            pages_ingested += 1
            records_ingested += len(records)
            records_rejected += rejected
            symbol_pages += 1
            cursor = page.next_cursor
            if cursor is None:
                completed_symbols.append(underlying)
                break

    return IngestionSummary(
        pages_ingested=pages_ingested,
        records_ingested=records_ingested,
        records_rejected=records_rejected,
        completed_symbols=tuple(completed_symbols),
        skipped_symbols=tuple(skipped_symbols),
    )


def _get_checkpoint(
    database: Path,
    source: str,
    interval: str,
    underlying: str,
    start_date: date,
    end_date: date,
) -> Any | None:
    connection = connect(database)
    try:
        return connection.execute(
            '''SELECT cursor, complete FROM ingestion_checkpoints
            WHERE source=? AND interval=? AND underlying=? AND start_date=? AND end_date=?''',
            (source, interval, underlying, start_date.isoformat(), end_date.isoformat()),
        ).fetchone()
    finally:
        connection.close()