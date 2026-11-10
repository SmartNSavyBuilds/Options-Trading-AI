from __future__ import annotations

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

from pathlib import Path

import pandas as pd

from src.execution import TradingConfig, sync_broker_state


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)
ACCOUNT_FILE = OUTPUT_DIR / 'broker_account_status.csv'
POSITIONS_FILE = OUTPUT_DIR / 'broker_positions.csv'


def _ensure_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in frame.columns:
            frame[col] = ''
    return frame[columns]


def main() -> None:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)

    account = _ensure_columns(
        account.copy(),
        [
            'synced_at_utc',
            'broker',
            'mode',
            'connection_status',
            'market_status',
            'equity',
            'cash',
            'buying_power',
            'portfolio_value',
            'options_approved_level',
            'options_trading_level',
            'options_buying_power',
            'detail',
        ],
    )
    positions = _ensure_columns(
        positions.copy(),
        ['synced_at_utc', 'symbol', 'side', 'qty', 'market_value', 'unrealized_pl'],
    )

    account.to_csv(ACCOUNT_FILE, index=False)
    positions.to_csv(POSITIONS_FILE, index=False)

    print('Broker snapshot refreshed:')
    print(ACCOUNT_FILE)
    print(POSITIONS_FILE)
    if not account.empty:
        print()
        print(account.to_string(index=False))
    if not positions.empty:
        print()
        print(positions.to_string(index=False))


if __name__ == '__main__':
    main()
