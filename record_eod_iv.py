from __future__ import annotations

import argparse
import math
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient

from src.execution import TradingConfig, choose_best_option_contract, fetch_option_contracts
from src.iv_history import record_iv, safety_database_path
from src.market_data import option_snapshot_metrics, stock_quote_midpoint


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = PROJECT_DIR / 'outputs' / 'options_safety.sqlite3'


def record_snapshots(config: TradingConfig, symbols: list[str], database: Path) -> list[str]:
    if not config.api_key or not config.secret_key:
        raise ValueError('Alpaca market-data credentials are required')
    stock_client = StockHistoricalDataClient(config.api_key, config.secret_key)
    option_client = OptionHistoricalDataClient(config.api_key, config.secret_key)
    failed = []
    for symbol in symbols:
        ticker = str(symbol).strip().upper()
        try:
            midpoint = stock_quote_midpoint(stock_client, ticker)
            contracts = fetch_option_contracts(config, ticker, min_days=25, max_days=35, target_strike=midpoint)
            contract = choose_best_option_contract(
                contracts, ticker, 'call', midpoint, min_days=25, max_days=35,
                target_min_days=25, target_max_days=35, target_days=30,
            )
            if not contract:
                raise ValueError('No suitable near-30-day option contract')
            strike = float(contract.get('strike_price', 0))
            if not math.isfinite(strike) or strike <= 0 or abs(strike - midpoint) / midpoint > 0.05:
                raise ValueError('No near-ATM option contract')
            _, _, implied_volatility = option_snapshot_metrics(option_client, str(contract['symbol']))
            record_iv(database, ticker, datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).date(), implied_volatility)
            print(f'{ticker}: IV snapshot recorded')
        except Exception as exc:
            failed.append(ticker)
            print(f'{ticker}: snapshot failed ({type(exc).__name__})')
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description='Record near-30-day ATM option IV snapshots; no orders are placed.')
    parser.add_argument('symbols', nargs='+', help='Underlying stock symbols')
    parser.add_argument('--database', type=Path, default=None)
    arguments = parser.parse_args()
    market_now = datetime.now(ZoneInfo('America/New_York'))
    if market_now.weekday() >= 5 or not time(15, 55) <= market_now.time() <= time(16, 5):
        parser.error('Record EOD snapshots on weekdays between 15:55 and 16:05 America/New_York.')
    config = TradingConfig.from_env()
    database = arguments.database or safety_database_path(PROJECT_DIR)
    return 1 if record_snapshots(config, arguments.symbols, database) else 0


if __name__ == '__main__':
    raise SystemExit(main())