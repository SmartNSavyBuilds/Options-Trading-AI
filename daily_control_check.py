from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

CONTROL_LATEST_FILE = OUTPUT_DIR / 'control_audit_latest.csv'
CONTROL_HISTORY_FILE = OUTPUT_DIR / 'control_audit_history.csv'
PORTFOLIO_DAILY_CHECKS_FILE = OUTPUT_DIR / 'portfolio_daily_checks.csv'


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _to_datetime(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
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


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _build_empty_row(now: datetime) -> dict[str, Any]:
    return {
        'checked_at_utc': now.isoformat(),
        'equity': 0.0,
        'daily_change_pct': 0.0,
        'daily_change_usd': 0.0,
        'open_positions': 0,
        'monitor_age_minutes': 999999,
        'monitor_stale': True,
        'performance_age_minutes': 999999,
        'performance_stale': True,
        'error_count_24h': 0,
        'blocked_count_24h': 0,
        'blocked_count_threshold_24h': int(os.getenv('CONTROL_BLOCKED_COUNT_THRESHOLD_24H', '10') or 10),
        'recent_expired_48h': 0,
        'recent_expired_threshold_48h': int(os.getenv('CONTROL_RECENT_EXPIRED_THRESHOLD_48H', '1') or 1),
        'protective_exit_fill_rate_pct': 0.0,
        'protective_exit_routed_count': 0,
        'protective_exit_min_fill_rate_pct': float(os.getenv('PROTECTIVE_EXIT_MIN_FILL_RATE_PCT', '10') or 10),
        'protective_exit_min_routed_count': int(os.getenv('PROTECTIVE_EXIT_MIN_ROUTED_COUNT', '10') or 10),
        'largest_position_symbol': '',
        'largest_position_pct': 0.0,
        'max_single_name_exposure_pct': float(os.getenv('MAX_SINGLE_NAME_EXPOSURE_PCT', '20') or 20),
        'total_exposure_pct': 0.0,
        'max_total_exposure_pct': float(os.getenv('MAX_TOTAL_EXPOSURE_PCT', '80') or 80),
        'concentration_breach': False,
        'control_miss': True,
        'control_miss_reasons': 'missing_required_inputs',
        'affected_by_controls': True,
        'queue_auto_held_count': 0,
        'queue_update_status': 'missing_required_inputs',
        'broker_auth_unauthorized': False,
    }


def _filter_actionable_blocked(blocked: pd.DataFrame) -> pd.DataFrame:
    if blocked.empty:
        return blocked
    detail_col = blocked.get('detail', pd.Series('', index=blocked.index)).astype(str).str.lower()
    note_col = blocked.get('note', pd.Series('', index=blocked.index)).astype(str).str.lower()
    ignore_patterns = [
        'waiting for approval',
        'preview is not ready',
        'preview not ready',
        'auto_submit is disabled',
        'auto submit is disabled',
    ]
    mask = pd.Series(True, index=blocked.index)
    for phrase in ignore_patterns:
        mask &= ~detail_col.str.contains(phrase, na=False)
        mask &= ~note_col.str.contains(phrase, na=False)
    return blocked[mask]


def _compute_row() -> dict[str, Any]:
    now = _now_utc()
    row = _build_empty_row(now)

    account = _safe_read_csv(OUTPUT_DIR / 'broker_account_status.csv')
    positions = _safe_read_csv(OUTPUT_DIR / 'broker_positions.csv')
    monitor = _safe_read_csv(OUTPUT_DIR / 'monitor_status.csv')
    performance = _safe_read_csv(OUTPUT_DIR / 'performance_summary.csv')
    execution_log = _safe_read_csv(OUTPUT_DIR / 'execution_log.csv')
    queue = _safe_read_csv(OUTPUT_DIR / 'paper_trade_queue.csv')

    monitor_ts = _to_datetime(monitor.iloc[0].get('last_run_utc')) if not monitor.empty else None
    performance_ts = None
    if not performance.empty:
        # Support both legacy and current performance summary schemas.
        performance_ts = _to_datetime(
            performance.iloc[0].get('as_of_utc')
            or performance.iloc[0].get('journaled_at_utc')
            or performance.iloc[0].get('checked_at_utc')
        )

    monitor_age_minutes = _age_minutes(monitor_ts, now)
    performance_age_minutes = _age_minutes(performance_ts, now)

    monitor_sla = float(os.getenv('CONTROL_MONITOR_STALE_MINUTES', '30') or 30)
    performance_sla = float(os.getenv('CONTROL_PERFORMANCE_STALE_MINUTES', '120') or 120)

    row['monitor_age_minutes'] = round(monitor_age_minutes, 2)
    row['monitor_stale'] = monitor_age_minutes > monitor_sla
    row['performance_age_minutes'] = round(performance_age_minutes, 2)
    row['performance_stale'] = performance_age_minutes > performance_sla

    if not account.empty:
        a = account.iloc[0]
        row['equity'] = float(pd.to_numeric(a.get('equity', 0.0), errors='coerce') or 0.0)
        connection_status = str(a.get('connection_status', '')).strip().lower()
        detail = str(a.get('detail', '')).strip().lower()
        row['broker_auth_unauthorized'] = connection_status == 'unauthorized' or 'unauthorized' in detail or '401' in detail

    if not positions.empty:
        working = positions.copy()
        working['market_value'] = pd.to_numeric(working.get('market_value', 0.0), errors='coerce').fillna(0.0).abs()
        row['open_positions'] = int(len(working))
        equity = max(float(row['equity']) or 0.0, 1.0)
        working['exposure_pct'] = working['market_value'] / equity * 100.0
        largest = working.sort_values('exposure_pct', ascending=False).iloc[0]
        row['largest_position_symbol'] = str(largest.get('symbol', ''))
        row['largest_position_pct'] = round(float(largest.get('exposure_pct', 0.0)), 2)
        row['total_exposure_pct'] = round(float(working['exposure_pct'].sum()), 2)

    max_single_name = float(os.getenv('MAX_SINGLE_NAME_EXPOSURE_PCT', '20') or 20)
    max_total = float(os.getenv('MAX_TOTAL_EXPOSURE_PCT', '80') or 80)
    row['max_single_name_exposure_pct'] = max_single_name
    row['max_total_exposure_pct'] = max_total
    concentration_breach = (float(row['largest_position_pct']) > max_single_name) or (float(row['total_exposure_pct']) > max_total)
    row['concentration_breach'] = concentration_breach

    error_count_24h = 0
    blocked_count_24h = 0
    recent_expired_48h = 0
    protective_exit_fill_rate = 0.0
    protective_exit_routed_count = 0

    if not execution_log.empty:
        log = execution_log.copy()
        ts_col = None
        for candidate in ['submitted_at_utc', 'timestamp', 'last_submitted_at']:
            if candidate in log.columns:
                ts_col = candidate
                break
        if ts_col is not None:
            log['parsed_ts'] = pd.to_datetime(log[ts_col], errors='coerce', utc=True)
        else:
            log['parsed_ts'] = pd.NaT

        recent_24h = log[log['parsed_ts'] >= (now - timedelta(hours=24))]
        recent_48h = log[log['parsed_ts'] >= (now - timedelta(hours=48))]

        status_col = recent_24h.get('status', pd.Series('', index=recent_24h.index)).astype(str).str.lower()
        detail_col_24h = recent_24h.get('detail', pd.Series('', index=recent_24h.index)).astype(str).str.lower()
        note_col_24h = recent_24h.get('note', pd.Series('', index=recent_24h.index)).astype(str).str.lower()

        error_like = status_col.isin({'error', 'rejected', 'missing_credentials'})
        error_like |= detail_col_24h.str.contains('unauthorized|forbidden|timeout|connection error', na=False)
        error_count_24h = int(error_like.sum())

        blocked = recent_24h[status_col == 'blocked']
        blocked = _filter_actionable_blocked(blocked)
        blocked_count_24h = int(len(blocked))

        status_col_48h = recent_48h.get('status', pd.Series('', index=recent_48h.index)).astype(str).str.lower()
        detail_col_48h = recent_48h.get('detail', pd.Series('', index=recent_48h.index)).astype(str).str.lower()
        note_col_48h = recent_48h.get('note', pd.Series('', index=recent_48h.index)).astype(str).str.lower()
        expired_mask = status_col_48h.str.contains('expired', na=False)
        expired_mask |= detail_col_48h.str.contains('expired', na=False)
        expired_mask |= note_col_48h.str.contains('expired', na=False)
        recent_expired_48h = int(expired_mask.sum())

        exit_mask = recent_24h.get('execution_style', pd.Series('', index=recent_24h.index)).astype(str).str.lower().eq('protective_exit_paper')
        exits = recent_24h[exit_mask]
        protective_exit_routed_count = int(len(exits))
        if protective_exit_routed_count > 0:
            good_statuses = {'new', 'accepted', 'filled', 'partially_filled', 'pending_new', 'submitted'}
            good = exits.get('status', pd.Series('', index=exits.index)).astype(str).str.lower().isin(good_statuses)
            protective_exit_fill_rate = float(good.sum()) / float(protective_exit_routed_count) * 100.0

    row['error_count_24h'] = error_count_24h
    row['blocked_count_24h'] = blocked_count_24h
    row['blocked_count_threshold_24h'] = int(os.getenv('CONTROL_BLOCKED_COUNT_THRESHOLD_24H', '10') or 10)
    row['recent_expired_48h'] = recent_expired_48h
    row['recent_expired_threshold_48h'] = int(os.getenv('CONTROL_RECENT_EXPIRED_THRESHOLD_48H', '1') or 1)
    row['protective_exit_fill_rate_pct'] = round(protective_exit_fill_rate, 2)
    row['protective_exit_routed_count'] = protective_exit_routed_count
    row['protective_exit_min_fill_rate_pct'] = float(os.getenv('PROTECTIVE_EXIT_MIN_FILL_RATE_PCT', '10') or 10)
    row['protective_exit_min_routed_count'] = int(os.getenv('PROTECTIVE_EXIT_MIN_ROUTED_COUNT', '10') or 10)

    queue_auto_held_count = 0
    queue_update_status = 'ok'
    if queue.empty:
        queue_update_status = 'queue_unavailable' if not (OUTPUT_DIR / 'paper_trade_queue.csv').exists() else 'queue_empty'
    else:
        if 'guardrail_status' in queue.columns:
            queue_auto_held_count = int(queue['guardrail_status'].astype(str).str.lower().eq('blocked').sum())
        elif 'order_status' in queue.columns:
            queue_auto_held_count = int(queue['order_status'].astype(str).str.lower().str.contains('hold|blocked', na=False).sum())
    row['queue_auto_held_count'] = queue_auto_held_count
    row['queue_update_status'] = queue_update_status

    reasons: list[str] = []

    if row['monitor_stale']:
        reasons.append('monitor_stale')
    if row['performance_stale']:
        reasons.append('performance_stale')
    if row.get('broker_auth_unauthorized', False):
        reasons.append('broker_auth_unauthorized')
    if concentration_breach:
        reasons.append('concentration_breach')
    if error_count_24h > int(row['blocked_count_threshold_24h']):
        reasons.append('error_count_24h')
    if blocked_count_24h > int(row['blocked_count_threshold_24h']):
        reasons.append('blocked_count_24h')
    if recent_expired_48h > int(row['recent_expired_threshold_48h']):
        reasons.append('recent_expired_48h')
    min_routed = int(row['protective_exit_min_routed_count'])
    min_fill = float(row['protective_exit_min_fill_rate_pct'])
    if protective_exit_routed_count >= min_routed and protective_exit_fill_rate < min_fill:
        reasons.append('protective_exit_fill_rate')

    monitor_stale_hold_enabled = _bool_env('MONITOR_STALE_HOLD_ENABLED', False)
    if row['monitor_stale'] and monitor_stale_hold_enabled:
        reasons.append('monitor_stale_hold')

    row['control_miss'] = bool(reasons)
    row['control_miss_reasons'] = '|'.join(reasons)
    row['affected_by_controls'] = bool(reasons or queue_auto_held_count > 0)

    return row


def _write_outputs(row: dict[str, Any]) -> None:
    latest_df = pd.DataFrame([row])
    latest_df.to_csv(CONTROL_LATEST_FILE, index=False)

    history_existing = _safe_read_csv(CONTROL_HISTORY_FILE)
    history_df = pd.concat([history_existing, latest_df], ignore_index=True) if not history_existing.empty else latest_df
    history_df.to_csv(CONTROL_HISTORY_FILE, index=False)

    checks_existing = _safe_read_csv(PORTFOLIO_DAILY_CHECKS_FILE)
    checks_df = pd.concat([checks_existing, latest_df], ignore_index=True) if not checks_existing.empty else latest_df
    checks_df.to_csv(PORTFOLIO_DAILY_CHECKS_FILE, index=False)


def main() -> None:
    row = _compute_row()
    _write_outputs(row)

    print('Daily control audit generated:')
    print(CONTROL_LATEST_FILE)
    print(CONTROL_HISTORY_FILE)
    print(PORTFOLIO_DAILY_CHECKS_FILE)
    print()
    print(pd.DataFrame([row]).to_string(index=False))


if __name__ == '__main__':
    main()
