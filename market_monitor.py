from __future__ import annotations

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

import argparse
import json
import os
import socket
import tempfile
import traceback
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
import time

import pandas as pd

from app import main as run_scanner
from daily_control_check import main as run_control_audit
from discover_opportunities import main as run_discovery
from evaluate_exit_rules import main as run_exit_rules
from execute_exit_trades import main as run_exit_execution
from execute_paper_trades import main as run_paper_execution
from multi_asset_report import main as run_multi_asset_report
from paper_trade import _recover_stale_previous_state, main as run_queue_builder
from performance_journal import main as run_performance_journal
from pre_live_gate_check import REPORT_FILE as PRE_LIVE_GATE_REPORT_FILE, build_report as build_pre_live_gate_report
from src.execution import TradingConfig, sync_broker_state


def _regenerate_discovery_quality() -> None:
    """Regenerate wiki/discovery_quality.json from current queue and market data.

    Called before every queue rebuild so the DQ gate uses live guardrail data
    and avoids the circular DQ-queue dependency where tickers entering the queue
    mid-cycle were scored with guardrail_status=unknown in a previous run.
    """
    try:
        from wiki_tools.discovery_quality import build_discovery_quality
        report = build_discovery_quality()
        print(f'Discovery quality refreshed: avg {report["avg_discovery_quality_score"]}, pass {report["pass_count"]}, block {report["block_count"]}')
    except Exception as exc:
        print(f'[WARN] Discovery quality regeneration failed (non-fatal): {exc}')



PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)
STATUS_FILE = OUTPUT_DIR / 'monitor_status.csv'
STAGE_STATUS_FILE = OUTPUT_DIR / 'monitor_stage_status.csv'
CONNECTIVITY_PREFLIGHT_FILE = OUTPUT_DIR / 'connectivity_preflight.csv'
FAILURE_LOG_FILE = OUTPUT_DIR / 'monitor_failures.jsonl'
# Kept out of the shared outputs mount so the host and container locks don't collide.
LOOP_LOCK_FILE = Path(tempfile.gettempdir()) / 'options_monitor_worker.lock'

# US market hours in UTC: regular session 13:30–20:00, pre-market starts ~12:00
_PREMARKET_OPEN_UTC = 12   # 8am ET
_MARKET_CLOSE_UTC   = 21   # 5pm ET (includes after-hours buffer)

# Autonomous safety thresholds (override via env)
_MAX_CONSECUTIVE_FAILURES = int(os.getenv('WORKER_MAX_CONSECUTIVE_FAILURES', '5'))
_STALE_HEARTBEAT_SECONDS  = int(os.getenv('WORKER_HEALTH_MAX_AGE_SECONDS', '1800'))  # 30 min
_OPS_WEBHOOK_URL          = os.getenv('OPS_WEBHOOK_URL', '').strip()


def _write_stage_status(
    *,
    cycle_started_at: datetime,
    stage: str,
    stage_status: str,
    connection_status: str = 'unknown',
    market_status: str = 'unknown',
    note: str = '',
) -> None:
    row = pd.DataFrame(
        [
            {
                'last_update_utc': datetime.now(timezone.utc).isoformat(),
                'cycle_started_utc': cycle_started_at.isoformat(),
                'stage': stage,
                'stage_status': stage_status,
                'connection_status': connection_status,
                'market_status': market_status,
                'note': note,
            }
        ]
    )
    row.to_csv(STAGE_STATUS_FILE, index=False)


def _run_stage(
    *,
    cycle_started_at: datetime,
    stage: str,
    fn,
    connection_status: str,
    market_status: str,
    note: str = '',
) -> None:
    _write_stage_status(
        cycle_started_at=cycle_started_at,
        stage=stage,
        stage_status='running',
        connection_status=connection_status,
        market_status=market_status,
        note=note,
    )
    try:
        fn()
        _write_stage_status(
            cycle_started_at=cycle_started_at,
            stage=stage,
            stage_status='ok',
            connection_status=connection_status,
            market_status=market_status,
            note=note,
        )
    except Exception as exc:
        _write_stage_status(
            cycle_started_at=cycle_started_at,
            stage=stage,
            stage_status='error',
            connection_status=connection_status,
            market_status=market_status,
            note=f'{type(exc).__name__}: {str(exc)[:220]}',
        )
        raise


def _run_connectivity_preflight(config: TradingConfig) -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    parsed = urllib.parse.urlparse(str(config.base_url or '').strip())
    host = parsed.hostname or ''

    dns_ok = False
    tcp_ok = False
    api_status = 'not_checked'
    detail = ''

    if host:
        try:
            socket.getaddrinfo(host, 443)
            dns_ok = True
        except Exception as exc:
            detail = f'DNS lookup failed: {exc}'

    if dns_ok:
        try:
            with socket.create_connection((host, 443), timeout=5):
                pass
            tcp_ok = True
        except Exception as exc:
            detail = f'TCP connect failed: {exc}'

    if tcp_ok and config.api_key and config.secret_key:
        try:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{config.base_url}/v2/account",
                    headers={
                        'APCA-API-KEY-ID': config.api_key,
                        'APCA-API-SECRET-KEY': config.secret_key,
                    },
                    method='GET',
                ),
                timeout=8,
            )
            api_status = 'connected' if int(response.getcode() or 0) == 200 else 'error'
            if api_status != 'connected':
                detail = f'Unexpected account status code: {response.getcode()}'
        except Exception as exc:
            lowered = str(exc).lower()
            if '401' in lowered or 'unauthorized' in lowered:
                api_status = 'unauthorized'
            elif 'timed out' in lowered or 'name resolution' in lowered or 'failed to establish' in lowered:
                api_status = 'network_error'
            else:
                api_status = 'error'
            detail = str(exc)[:320]
    elif not config.api_key or not config.secret_key:
        api_status = 'missing_credentials'
        detail = 'Alpaca API key pair is missing in environment.'
    elif not dns_ok or not tcp_ok:
        api_status = 'network_error'

    row = {
        'checked_at_utc': timestamp,
        'base_url': str(config.base_url or ''),
        'host': host,
        'dns_ok': str(bool(dns_ok)),
        'tcp_443_ok': str(bool(tcp_ok)),
        'api_status': api_status,
        'detail': detail,
    }
    pd.DataFrame([row]).to_csv(CONNECTIVITY_PREFLIGHT_FILE, index=False)
    return row


def _is_options_contracts_auth_error(error: Exception) -> bool:
    text = str(error or '').lower()
    return '/v2/options/contracts' in text and ('401' in text or 'unauthorized' in text)


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


def _queue_needs_recovery(queue_path: Path | None = None) -> bool:
    """Return True when the queue contains stale hold rows that should be retried."""
    path = queue_path or OUTPUT_DIR / 'paper_trade_queue.csv'
    if not path.exists():
        return False

    try:
        queue = pd.read_csv(path)
    except Exception:
        return False
    if queue.empty or 'approval_status' not in queue.columns or 'queue_refreshed_at_utc' not in queue.columns:
        return False

    if 'approval_status' not in queue.columns:
        return False

    stale_mask = queue['approval_status'].astype(str).str.lower().isin({'hold', 'pending'})
    if not stale_mask.any():
        return False

    refreshed = pd.to_datetime(queue.loc[stale_mask, 'queue_refreshed_at_utc'], utc=True, errors='coerce')
    if refreshed.isna().all():
        return False

    recovery_minutes = int(os.getenv('PAPER_QUEUE_RECOVERY_MINUTES', '60') or 60)
    if recovery_minutes <= 0:
        return False

    age_minutes = ((pd.Timestamp.now(tz='UTC') - refreshed).dt.total_seconds() / 60.0)
    return bool((age_minutes > recovery_minutes).any())


_LOOP_LOCK_HANDLE = None


def _acquire_loop_lock() -> bool:
    """Take an OS-level lock so only one --loop monitor runs per machine or container."""
    global _LOOP_LOCK_HANDLE
    handle = open(LOOP_LOCK_FILE, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print('Another market monitor loop is already running here. Exiting this instance.')
        return False
    _LOOP_LOCK_HANDLE = handle
    return True


def _release_loop_lock() -> None:
    global _LOOP_LOCK_HANDLE
    if _LOOP_LOCK_HANDLE is not None:
        _LOOP_LOCK_HANDLE.close()
        _LOOP_LOCK_HANDLE = None


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

    _write_stage_status(
        cycle_started_at=started,
        stage='cycle_start',
        stage_status='running',
        note='Cycle initialization started.',
    )

    preflight = _run_connectivity_preflight(config)
    _write_stage_status(
        cycle_started_at=started,
        stage='connectivity_preflight',
        stage_status='ok' if preflight.get('api_status') in {'connected', 'unauthorized', 'missing_credentials'} else 'error',
        connection_status=preflight.get('api_status', 'unknown'),
        note=preflight.get('detail', ''),
    )

    _write_stage_status(cycle_started_at=started, stage='broker_sync', stage_status='running')
    account, positions = sync_broker_state(config)

    market_status = 'unknown'
    connection_status = 'unknown'
    if not account.empty:
        row = account.iloc[0]
        market_status = str(row.get('market_status', 'unknown'))
        connection_status = str(row.get('connection_status', 'unknown'))

    _write_stage_status(
        cycle_started_at=started,
        stage='broker_sync',
        stage_status='ok',
        connection_status=connection_status,
        market_status=market_status,
    )

    broker_auth_blocked = connection_status == 'unauthorized'
    if broker_auth_blocked:
        detail = str(account.iloc[0].get('detail', '')) if not account.empty else 'unknown broker auth failure'
        _send_alert(
            'BROKER AUTH FAILURE',
            f'Alpaca rejected the current credentials ({detail}). Execution is paused until the API key pair and base URL are fixed.',
        )

    # --- Analytics and scanning always run (useful for pre-market prep) ---
    _run_stage(cycle_started_at=started, stage='scanner_primary', fn=run_scanner, connection_status=connection_status, market_status=market_status)
    _run_stage(cycle_started_at=started, stage='discovery', fn=run_discovery, connection_status=connection_status, market_status=market_status)
    _run_stage(cycle_started_at=started, stage='multi_asset_report', fn=run_multi_asset_report, connection_status=connection_status, market_status=market_status)
    _run_stage(cycle_started_at=started, stage='performance_journal', fn=run_performance_journal, connection_status=connection_status, market_status=market_status)
    _run_stage(cycle_started_at=started, stage='control_audit', fn=run_control_audit, connection_status=connection_status, market_status=market_status)
    if _queue_needs_recovery():
        print('Detected stale paper-trade queue holds; forcing queue rebuild to retry them.')
    _run_stage(cycle_started_at=started, stage='discovery_quality_refresh', fn=_regenerate_discovery_quality, connection_status=connection_status, market_status=market_status, note='Refresh DQ before queue build.')
    _run_stage(cycle_started_at=started, stage='queue_builder', fn=run_queue_builder, connection_status=connection_status, market_status=market_status)
    _run_stage(cycle_started_at=started, stage='exit_rules', fn=run_exit_rules, connection_status=connection_status, market_status=market_status)

    # --- Live execution only during market hours ---
    hour_utc = started.hour
    market_is_active = (market_status == 'open') or (
        _PREMARKET_OPEN_UTC <= hour_utc < _MARKET_CLOSE_UTC
    )
    entry_execution_blocked = False
    if market_is_active:
        if broker_auth_blocked:
            print('Broker auth is unauthorized — skipping paper execution until credentials are fixed.')
            print('Broker auth is unauthorized — skipping exit execution until credentials are fixed.')
            entry_execution_blocked = True
        elif preflight.get('api_status') == 'network_error':
            print('Broker connectivity preflight failed — skipping live execution for this cycle.')
            entry_execution_blocked = True
        else:
            _run_stage(cycle_started_at=started, stage='exit_execution', fn=run_exit_execution, connection_status=connection_status, market_status=market_status)
            try:
                _run_stage(cycle_started_at=started, stage='paper_execution', fn=run_paper_execution, connection_status=connection_status, market_status=market_status)
            except Exception as exc:
                if _is_options_contracts_auth_error(exc):
                    entry_execution_blocked = True
                    detail = str(exc)[:280]
                    print(f'Options contract auth failed; skipping entry execution for this cycle: {detail}')
                    _send_alert(
                        'OPTIONS CONTRACT AUTH FAILURE',
                        f'Entry execution skipped this cycle due to options-contract auth failure: {detail}',
                    )
                else:
                    raise
        _run_stage(cycle_started_at=started, stage='scanner_post_execution', fn=run_scanner, connection_status=connection_status, market_status=market_status, note='Refresh signals after execution window.')
        if broker_auth_blocked:
            execution_note = 'Full cycle: analytics completed, paper execution skipped due to broker auth failure.'
        elif preflight.get('api_status') == 'network_error':
            execution_note = 'Full cycle: analytics completed, live execution skipped due to connectivity preflight failure.'
        elif entry_execution_blocked:
            execution_note = 'Full cycle: analytics completed, entry execution skipped due to options-contract auth failure.'
        else:
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

    gate_report = build_pre_live_gate_report()
    PRE_LIVE_GATE_REPORT_FILE.write_text(json.dumps(gate_report, indent=2), encoding='utf-8')

    print('Market monitor cycle completed.')
    print(status.to_string(index=False))
    print(f"Pre-live gate verdict: {gate_report.get('verdict', 'unknown')}")
    _write_stage_status(
        cycle_started_at=started,
        stage='cycle_complete',
        stage_status='ok',
        connection_status=connection_status,
        market_status=market_status,
        note=execution_note,
    )
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

    if not _acquire_loop_lock():
        return

    consecutive_failures = 0
    cycle_num = 0

    try:
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
    finally:
        _release_loop_lock()


if __name__ == '__main__':
    main()
