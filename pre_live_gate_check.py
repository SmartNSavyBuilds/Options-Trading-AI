from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

REPORT_FILE = OUTPUT_DIR / 'pre_live_gate_report.json'


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _to_datetime(value: Any) -> datetime | None:
    try:
        ts = pd.to_datetime(value, errors='coerce', utc=True)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if hasattr(ts, 'to_pydatetime'):
        return ts.to_pydatetime()
    return None


def _age_minutes(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return 999999.0
    return max((now - ts).total_seconds() / 60.0, 0.0)


def _read_first_valid_row(path: Path) -> dict[str, Any]:
    df = _safe_read_csv(path)
    if df.empty:
        return {}
    for _, row in df.iterrows():
        if any(str(v).strip() for v in row.values if not pd.isna(v)):
            return row.to_dict()
    return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'true', '1', 'yes', 'y', 'on'}:
        return True
    if text in {'false', '0', 'no', 'n', 'off'}:
        return False
    return default


def _resolve_worker_health(now: datetime, heartbeat_max_seconds: int) -> dict[str, Any]:
    """Resolve worker heartbeat/success from the freshest available status artifact."""
    worker_row = _read_first_valid_row(OUTPUT_DIR / 'worker_status.csv')
    monitor_row = _read_first_valid_row(OUTPUT_DIR / 'monitor_status.csv')

    worker_ts = _to_datetime(worker_row.get('checked_at_utc') or worker_row.get('last_run_utc')) if worker_row else None
    monitor_ts = _to_datetime(monitor_row.get('last_run_utc')) if monitor_row else None

    heartbeat_source = 'none'
    effective_row: dict[str, Any] = {}
    effective_ts: datetime | None = None

    if worker_ts and monitor_ts:
        if monitor_ts >= worker_ts:
            heartbeat_source = 'monitor_status'
            effective_row = monitor_row
            effective_ts = monitor_ts
        else:
            heartbeat_source = 'worker_status'
            effective_row = worker_row
            effective_ts = worker_ts
    elif monitor_ts:
        heartbeat_source = 'monitor_status'
        effective_row = monitor_row
        effective_ts = monitor_ts
    elif worker_ts:
        heartbeat_source = 'worker_status'
        effective_row = worker_row
        effective_ts = worker_ts

    age_minutes = _age_minutes(effective_ts, now)
    heartbeat_recent = age_minutes <= (heartbeat_max_seconds / 60.0)

    if heartbeat_source == 'worker_status':
        last_success = _as_bool(effective_row.get('success'), False)
        failed_step = str(effective_row.get('failed_step', '')).strip()
        failed_status = str(effective_row.get('failed_status', '')).strip()
    elif heartbeat_source == 'monitor_status':
        monitor_status = str(effective_row.get('monitor_status', '')).strip().lower()
        last_success = monitor_status == 'running'
        failed_step = 'market_monitor_cycle' if monitor_status else ''
        failed_status = monitor_status
    else:
        last_success = False
        failed_step = ''
        failed_status = ''

    return {
        'heartbeat_source': heartbeat_source,
        'age_minutes': age_minutes,
        'heartbeat_recent': heartbeat_recent,
        'last_success': last_success,
        'failed_step': failed_step,
        'failed_status': failed_status,
    }


def build_report() -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    worker_health = _resolve_worker_health(now, int(os.getenv('WORKER_HEALTH_MAX_AGE_SECONDS', '1800') or 1800))
    control_row = _read_first_valid_row(OUTPUT_DIR / 'control_audit_latest.csv')
    account_row = _read_first_valid_row(OUTPUT_DIR / 'broker_account_status.csv')
    live_preflight = {}
    live_preflight_path = OUTPUT_DIR / 'live_trading_preflight.json'
    if live_preflight_path.exists() and live_preflight_path.stat().st_size > 0:
        try:
            live_preflight = json.loads(live_preflight_path.read_text(encoding='utf-8'))
        except Exception:
            live_preflight = {}

    heartbeat_max_seconds = int(os.getenv('WORKER_HEALTH_MAX_AGE_SECONDS', '1800') or 1800)
    worker_age_minutes = float(worker_health['age_minutes'])
    worker_heartbeat_recent = bool(worker_health['heartbeat_recent'])
    worker_last_success = bool(worker_health['last_success'])
    worker_failed_step = str(worker_health['failed_step'])
    worker_failed_status = str(worker_health['failed_status'])
    worker_heartbeat_source = str(worker_health['heartbeat_source'])

    control_present = bool(control_row)
    control_miss = _as_bool(control_row.get('control_miss'), True) if control_present else True
    if control_present:
        reasons_value = control_row.get('control_miss_reasons', '')
        control_reasons = '' if pd.isna(reasons_value) else str(reasons_value).strip()
    else:
        control_reasons = 'missing_control_audit'
    queue_auto_held_count = int(pd.to_numeric(control_row.get('queue_auto_held_count', 0), errors='coerce') or 0) if control_present else 0
    # Treat explicit control reasons or queued auto-holds as a control miss,
    # even when the boolean column is stale or incorrectly serialized.
    if control_present and (bool(control_reasons) or queue_auto_held_count > 0):
        control_miss = True

    broker_connected = False
    broker_status = 'unknown'
    broker_detail = ''
    if account_row:
        broker_status = str(account_row.get('connection_status', 'unknown')).strip().lower()
        broker_detail = str(account_row.get('detail', '')).strip()
        broker_connected = broker_status == 'connected'

    live_preflight_pass = bool(live_preflight.get('pass', False)) if live_preflight else False

    checks = {
        'worker_heartbeat_recent': {
            'pass': worker_heartbeat_recent,
            'age_minutes': round(worker_age_minutes, 2),
            'max_age_minutes': round(heartbeat_max_seconds / 60.0, 2),
            'source': worker_heartbeat_source,
        },
        'worker_last_success': {
            'pass': worker_last_success,
            'failed_step': worker_failed_step,
            'failed_status': worker_failed_status,
        },
        'control_audit_present': {
            'pass': control_present,
        },
        'control_miss_clear': {
            'pass': control_present and not control_miss,
            'reasons': control_reasons,
            'queue_auto_held_count': queue_auto_held_count,
            'control_miss': control_miss,
        },
        'broker_connected': {
            'pass': broker_connected,
            'status': broker_status,
            'detail': broker_detail,
        },
        'live_preflight': {
            'pass': live_preflight_pass,
            'present': bool(live_preflight),
        },
    }

    required_for_pass = [
        'worker_heartbeat_recent',
        'worker_last_success',
        'control_audit_present',
        'control_miss_clear',
        'broker_connected',
    ]
    overall_pass = all(bool(checks[name]['pass']) for name in required_for_pass)

    report = {
        'checked_at_utc': now.isoformat(),
        'verdict': 'pass' if overall_pass else 'fail',
        'required_checks': required_for_pass,
        'checks': checks,
        'notes': {
            'live_preflight_required_for_live_mode': True,
            'live_preflight_passed': live_preflight_pass,
            'trading_mode': str(os.getenv('TRADING_MODE', 'paper')).strip().lower(),
            'worker_heartbeat_source': worker_heartbeat_source,
        },
    }
    return report


def main() -> None:
    report = build_report()
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding='utf-8')

    print('Pre-live gate report written:')
    print(REPORT_FILE)
    print()
    print(json.dumps(report, indent=2))

    if report.get('verdict') != 'pass':
        sys.exit(1)


if __name__ == '__main__':
    main()
