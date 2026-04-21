from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import time

import pandas as pd

from app import main as run_scanner
from discover_opportunities import main as run_discovery
from evaluate_exit_rules import main as run_exit_rules
from execute_exit_trades import main as run_exit_execution
from execute_paper_trades import main as run_paper_execution
from multi_asset_report import main as run_multi_asset_report
from paper_trade import main as run_queue_builder
from performance_journal import main as run_performance_journal
from src.execution import TradingConfig, sync_broker_state


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)
STATUS_FILE = OUTPUT_DIR / 'monitor_status.csv'


def run_cycle() -> None:
    started = datetime.now(timezone.utc)
    config = TradingConfig.from_env()

    account, positions = sync_broker_state(config)
    run_scanner()
    run_discovery()
    run_multi_asset_report()
    run_exit_rules()
    run_exit_execution()   # autonomously act on auto_approved exits
    run_performance_journal()
    run_scanner()
    run_queue_builder()
    run_paper_execution()  # autonomously submit approved paper trade entries

    market_status = 'unknown'
    connection_status = 'unknown'
    if not account.empty:
        row = account.iloc[0]
        market_status = str(row.get('market_status', 'unknown'))
        connection_status = str(row.get('connection_status', 'unknown'))

    status = pd.DataFrame(
        [
            {
                'last_run_utc': started.isoformat(),
                'monitor_status': 'running',
                'connection_status': connection_status,
                'market_status': market_status,
                'open_positions': int(len(positions)) if not positions.empty else 0,
                'note': 'Scanner, discovery, exit execution, paper trade execution, and analytics completed autonomously.',
            }
        ]
    )
    status.to_csv(STATUS_FILE, index=False)
    print('Market monitor cycle completed.')
    print(status.to_string(index=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run the market monitoring workflow once or in a loop.')
    parser.add_argument('--loop', action='store_true', help='Run continuously in a timed loop.')
    parser.add_argument('--interval-seconds', type=int, default=900, help='Delay between cycles when --loop is enabled.')
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.loop:
        run_cycle()
        return

    while True:
        run_cycle()
        time.sleep(max(args.interval_seconds, 60))


if __name__ == '__main__':
    main()
