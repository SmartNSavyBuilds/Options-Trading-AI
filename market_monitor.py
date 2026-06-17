from __future__ import annotations

import argparse
import json
import os
import traceback
import urllib.request
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
FAILURE_LOG_FILE = OUTPUT_DIR / 'monitor_failures.jsonl'

# US market hours in UTC: regular session 13:30–20:00, pre-market starts ~12:00
_PREMARKET_OPEN_UTC = 12   # 8am ET
_MARKET_CLOSE_UTC   = 21   # 5pm ET (includes after-hours buffer)

# Autonomous safety thresholds (override via env)
_MAX_CONSECUTIVE_FAILURES = int(os.getenv('WORKER_MAX_CONSECUTIVE_FAILURES', '5'))
_STALE_HEARTBEAT_SECONDS  = int(os.getenv('WORKER_HEALTH_MAX_AGE_SECONDS', '1800'))  # 30 min
_OPS_WEBHOOK_URL          = os.getenv('OPS_WEBHOOK_URL', '').strip()


def _send_alert(event: str, detail: str) -> None:
    """POST a compact JSON alert to OPS_WEBHOOK_URL (Discord/Slack/generic).
    Silent no-op when the env var is not configured."""
    if not _OPS_WEBHOOK_URL:
        return
    payload = json.dumps({
        'username': 'Trade Desk Worker',
        'content': f'**[{event}]** {detail}',
    }).encode()
    try:
        req = urllib.request.Request(
            _OPS_WEBHOOK_URL,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Never let alert failure break the worker loop


def _log_failure(error: Exception, cycle_num: int) -> None:
    """Append structured failure entry to FAILURE_LOG_FILE for dashboard visibility."""
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'cycle': cycle_num,
        'error': type(error).__name__,
        'detail': str(error)[:400],
    }
    try:
        with FAILURE_LOG_FILE.open('a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def _check_stale_heartbeat() -> bool:
    """Returns True (and fires an alert) if the last heartbeat is older than threshold."""
    if not STATUS_FILE.exists():
        return False  # first-run — no heartbeat yet
    age = time.time() - STATUS_FILE.stat().st_mtime
    if age > _STALE_HEARTBEAT_SECONDS:
        _send_alert(
            'STALE HEARTBEAT',
            f'monitor_status.csv is {int(age / 60)}m old — worker may be stuck or crashed.',
        )
        return True
    return False


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

    consecutive_failures = 0
    cycle_num = 0

    while True:
        cycle_num += 1
        try:
            market_status = run_cycle()
            consecutive_failures = 0  # reset on success
        except Exception as exc:
            consecutive_failures += 1
            _log_failure(exc, cycle_num)
            print(f'[ERROR] Cycle {cycle_num} failed ({consecutive_failures} consecutive): {exc}')
            traceback.print_exc()

            _send_alert(
                'WORKER FAILURE',
                f'Cycle {cycle_num} failed ({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}): {type(exc).__name__}: {str(exc)[:300]}',
            )

            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                _send_alert(
                    'WORKER HALTED',
                    f'Halting loop after {consecutive_failures} consecutive failures. Manual restart required.',
                )
                print(f'[HALT] Too many consecutive failures ({consecutive_failures}). Exiting loop.')
                break

            # Write a degraded status so dashboard shows the problem
            pd.DataFrame([{
                'last_run_utc': datetime.now(timezone.utc).isoformat(),
                'monitor_status': 'error',
                'connection_status': 'unknown',
                'market_status': 'unknown',
                'open_positions': 0,
                'note': f'Cycle {cycle_num} failed: {type(exc).__name__}: {str(exc)[:200]}',
            }]).to_csv(STATUS_FILE, index=False)

            market_status = 'unknown'

        sleep_secs = _sleep_seconds(args.interval_seconds, market_status)
        print(f'Sleeping {sleep_secs}s until next cycle (market: {market_status}).')
        time.sleep(sleep_secs)


if __name__ == '__main__':
    main()
