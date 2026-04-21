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

# US market hours in UTC: regular session 13:30–20:00, pre-market starts ~12:00
_PREMARKET_OPEN_UTC = 12   # 8am ET
_MARKET_CLOSE_UTC   = 21   # 5pm ET (includes after-hours buffer)


def _sleep_seconds(normal_interval: int, market_status: str) -> int:
    """Return a smart sleep duration based on market hours.

    - Market open  → use the configured interval (default 15 min)
    - Market closed during the trading day window → 30 min
    - Overnight (outside 12:00–21:00 UTC) → 60 min; no execution possible
    """
    hour_utc = datetime.now(timezone.utc).hour
    if market_status == 'open':
        return max(normal_interval, 60)
    if _PREMARKET_OPEN_UTC <= hour_utc < _MARKET_CLOSE_UTC:
        # Closed but within the trading-day window (halted, early-close, etc.)
        return max(normal_interval, 60)
    # Overnight — nothing will execute; sleep 1 hour
    return 3600


def run_cycle() -> str:
    """Run one full monitor cycle. Returns the detected market_status string."""
    started = datetime.now(timezone.utc)
    config = TradingConfig.from_env()

    account, positions = sync_broker_state(config)

    market_status = 'unknown'
    connection_status = 'unknown'
    if not account.empty:
        row = account.iloc[0]
        market_status = str(row.get('market_status', 'unknown'))
        connection_status = str(row.get('connection_status', 'unknown'))

    # --- Analytics and scanning always run (useful for pre-market prep) ---
    run_scanner()
    run_discovery()
    run_multi_asset_report()
    run_performance_journal()
    run_queue_builder()       # build/refresh the trade queue any time
    run_exit_rules()          # evaluate exit thresholds any time

    # --- Live execution only during market hours ---
    hour_utc = started.hour
    market_is_active = (market_status == 'open') or (
        _PREMARKET_OPEN_UTC <= hour_utc < _MARKET_CLOSE_UTC
    )
    if market_is_active:
        run_exit_execution()   # autonomously act on auto_approved exits
        run_paper_execution()  # autonomously submit approved paper trade entries
        run_scanner()          # second pass after execution to refresh signals
        execution_note = 'Full cycle: analytics + live execution completed.'
    else:
        print(f'Market is {market_status} (UTC hour {hour_utc}) — skipping live execution.')
        execution_note = f'Reduced cycle: analytics only (market {market_status}).'

    status = pd.DataFrame(
        [
            {
                'last_run_utc': started.isoformat(),
                'monitor_status': 'running',
                'connection_status': connection_status,
                'market_status': market_status,
                'open_positions': int(len(positions)) if not positions.empty else 0,
                'note': execution_note,
            }
        ]
    )
    status.to_csv(STATUS_FILE, index=False)
    print('Market monitor cycle completed.')
    print(status.to_string(index=False))
    return market_status


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
        market_status = run_cycle()
        sleep_secs = _sleep_seconds(args.interval_seconds, market_status)
        print(f'Sleeping {sleep_secs}s until next cycle (market: {market_status}).')
        time.sleep(sleep_secs)


if __name__ == '__main__':
    main()
