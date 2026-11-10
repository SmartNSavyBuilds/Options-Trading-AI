from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

from daily_control_check import main as run_control_audit
from execute_exit_trades import main as run_exit_execution
from execute_paper_trades import main as run_entry_execution
from paper_trade import main as run_queue_builder
from pre_live_gate_check import main as run_gate_report
from src.execution import TradingConfig, sync_broker_state


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

RUN_SUMMARY_FILE = OUTPUT_DIR / 'deterministic_pipeline_last_run.json'
RUN_HISTORY_FILE = OUTPUT_DIR / 'deterministic_pipeline_history.jsonl'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_summary(summary: dict[str, Any]) -> None:
    RUN_SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding='utf-8')


def _load_run_history() -> list[dict[str, Any]]:
    if not RUN_HISTORY_FILE.exists():
        return []

    entries: list[dict[str, Any]] = []
    for raw_line in RUN_HISTORY_FILE.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                entries.append(parsed)
        except json.JSONDecodeError:
            continue
    return entries


def _append_run_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    with RUN_HISTORY_FILE.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, sort_keys=True) + '\n')
    return _load_run_history()


def _compute_success_run_streak(history: list[dict[str, Any]]) -> int:
    streak = 0
    for entry in reversed(history):
        if str(entry.get('status', '')).strip().lower() == 'ok':
            streak += 1
        else:
            break
    return streak


def _compute_success_day_streak(history: list[dict[str, Any]]) -> int:
    successful_days: set[str] = set()
    for entry in history:
        if str(entry.get('status', '')).strip().lower() != 'ok':
            continue
        finished_at = str(entry.get('finished_at_utc', '')).strip()
        if len(finished_at) >= 10:
            successful_days.add(finished_at[:10])

    if not successful_days:
        return 0

    sorted_days = sorted(successful_days)
    cursor = datetime.fromisoformat(sorted_days[-1]).date()
    streak = 0
    for day_text in reversed(sorted_days):
        day = datetime.fromisoformat(day_text).date()
        if day != cursor:
            break
        streak += 1
        cursor = cursor.fromordinal(cursor.toordinal() - 1)
    return streak


def _finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    history = _append_run_history(
        {
            'started_at_utc': summary.get('started_at_utc', ''),
            'finished_at_utc': summary.get('finished_at_utc', ''),
            'status': summary.get('status', 'unknown'),
            'strict_gate': bool(summary.get('strict_gate', False)),
            'require_connected': bool(summary.get('require_connected', True)),
        }
    )
    summary['success_run_streak'] = _compute_success_run_streak(history)
    summary['success_day_streak'] = _compute_success_day_streak(history)
    summary['history_file'] = str(RUN_HISTORY_FILE)
    _write_summary(summary)
    return summary


def _sync_broker_or_raise(require_connected: bool) -> dict[str, Any]:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)

    if account.empty:
        raise RuntimeError('Broker sync returned an empty account snapshot.')

    row = account.iloc[0]
    connection_status = str(row.get('connection_status', 'unknown')).strip().lower()
    detail = str(row.get('detail', '')).strip()

    if require_connected and connection_status != 'connected':
        raise RuntimeError(f'Broker is not connected ({connection_status}): {detail}')

    return {
        'connection_status': connection_status,
        'detail': detail,
        'open_positions': int(len(positions)) if not positions.empty else 0,
    }


def _run_gate_step(strict_gate: bool) -> dict[str, Any]:
    try:
        run_gate_report()
        return {'gate_result': 'pass'}
    except SystemExit as exc:
        code = int(exc.code or 0)
        if code == 0:
            return {'gate_result': 'pass'}
        if strict_gate:
            raise RuntimeError('Pre-live gate report returned fail verdict.') from exc
        return {'gate_result': 'fail'}


def run_pipeline(require_connected: bool, strict_gate: bool) -> dict[str, Any]:
    started_at = _utc_now()
    steps: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        'started_at_utc': started_at,
        'finished_at_utc': '',
        'status': 'running',
        'require_connected': require_connected,
        'strict_gate': strict_gate,
        'steps': steps,
    }
    _write_summary(summary)

    sequence: list[tuple[str, Callable[[], dict[str, Any] | None]]] = [
        ('sync_broker', lambda: _sync_broker_or_raise(require_connected=require_connected)),
        ('build_queue', lambda: (run_queue_builder(), {'queue': 'refreshed'})[1]),
        ('execute_entries', lambda: (run_entry_execution(), {'entries': 'executed'})[1]),
        ('execute_exits', lambda: (run_exit_execution(), {'exits': 'executed'})[1]),
        ('run_controls', lambda: (run_control_audit(), {'controls': 'updated'})[1]),
        ('generate_gate_report', lambda: _run_gate_step(strict_gate=strict_gate)),
    ]

    try:
        for step_name, step_func in sequence:
            step_started = _utc_now()
            print(f'[{step_started}] STEP START: {step_name}')

            detail = step_func() or {}

            step_finished = _utc_now()
            print(f'[{step_finished}] STEP OK: {step_name}')
            steps.append({
                'step': step_name,
                'status': 'ok',
                'started_at_utc': step_started,
                'finished_at_utc': step_finished,
                'detail': detail,
            })
            _write_summary(summary)

        summary['status'] = 'ok'
        summary['finished_at_utc'] = _utc_now()
        return _finalize_summary(summary)

    except Exception as exc:
        finished = _utc_now()
        print(f'[{finished}] STEP FAILED: {type(exc).__name__}: {exc}')
        steps.append({
            'step': step_name,
            'status': 'failed',
            'started_at_utc': step_started,
            'finished_at_utc': finished,
            'detail': {
                'error_type': type(exc).__name__,
                'error_message': str(exc),
                'traceback': traceback.format_exc(limit=10),
            },
        })
        summary['status'] = 'failed'
        summary['finished_at_utc'] = finished
        _finalize_summary(summary)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Run a deterministic operator workflow: sync broker -> build queue -> '
            'execute entries -> execute exits -> run controls -> generate gate report.'
        )
    )
    parser.add_argument(
        '--allow-disconnected',
        action='store_true',
        help='Continue even if broker connection status is not connected.',
    )
    parser.add_argument(
        '--strict-gate',
        action='store_true',
        help='Fail the command when pre_live_gate_check returns a fail verdict.',
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = run_pipeline(
        require_connected=not args.allow_disconnected,
        strict_gate=args.strict_gate,
    )
    print('Deterministic pipeline completed.')
    print(f"Status: {summary.get('status')}")
    print(f'Run summary: {RUN_SUMMARY_FILE}')


if __name__ == '__main__':
    main()