from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from src.quant.db import quant_database_path
from src.quant.ingest import ingest_history
from src.quant.iv_population import derive_atm_30d_observations, populate_iv_percentiles
from src.quant.providers.thetadata import ThetaDataProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Backfill EOD option data into the isolated Quant database.')
    parser.add_argument('--symbols', nargs='+', required=True, help='Underlying symbols, for example SPY QQQ')
    parser.add_argument('--start-date', type=_parse_date, required=True)
    parser.add_argument('--end-date', type=_parse_date, required=True)
    parser.add_argument('--database', type=Path, default=None)
    parser.add_argument('--max-pages-per-symbol', type=int, default=None)
    arguments = parser.parse_args(argv)
    database = arguments.database or quant_database_path()
    provider = ThetaDataProvider()
    summary = ingest_history(
        provider,
        arguments.symbols,
        arguments.start_date,
        arguments.end_date,
        interval='eod',
        database=database,
        max_pages_per_symbol=arguments.max_pages_per_symbol,
    )
    print(
        f'Pages: {summary.pages_ingested}; records: {summary.records_ingested}; '
        f'rejected: {summary.records_rejected}; completed: {len(summary.completed_symbols)}; '
        f'already complete: {len(summary.skipped_symbols)}'
    )
    requested = {symbol.strip().upper() for symbol in arguments.symbols if symbol.strip()}
    finished = set(summary.completed_symbols) | set(summary.skipped_symbols)
    if not requested.issubset(finished):
        print('Backfill paused with saved cursors; rerun the same command to resume.')
        return 2
    derived = derive_atm_30d_observations(database, source=provider.name, symbols=requested)
    ranked = populate_iv_percentiles(database, source=provider.name, symbols=requested)
    print(f'30-day IV observations: {derived}; percentile rows: {ranked}; database: {database}')
    return 0


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Dates must use YYYY-MM-DD format') from exc


if __name__ == '__main__':
    raise SystemExit(main())