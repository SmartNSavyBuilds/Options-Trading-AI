from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

try:
    import dashboard
except Exception as exc:  # the dashboard script reads generated outputs at import time
    pytest.skip(f'dashboard needs generated outputs to import: {exc!r}', allow_module_level=True)


def _write_csv(path, header: str, row: str) -> None:
    path.write_text(f"{header}\n{row}\n", encoding='utf-8')


def _age_file(path, seconds_ago: int) -> None:
    timestamp = time.time() - seconds_ago
    os.utime(path, (timestamp, timestamp))


def test_worker_health_flags_stale_monitor_heartbeat(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    monitor_file = outputs / 'monitor_status.csv'
    _write_csv(
        monitor_file,
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        f'{datetime.now(timezone.utc).isoformat()},running,connected,open,0,ok',
    )
    _age_file(monitor_file, 45 * 60)

    monkeypatch.setattr(dashboard, 'OUTPUT_DIR', outputs)

    label, color, note = dashboard._get_worker_health()

    assert label.startswith('Stale')
    assert 'monitor status' in note.lower()


def test_worker_health_uses_fresh_worker_status_fallback(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    stale_monitor = outputs / 'monitor_status.csv'
    fresh_worker = outputs / 'worker_status.csv'

    _write_csv(
        stale_monitor,
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        f'{(datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()},running,connected,open,0,ok',
    )
    _write_csv(
        fresh_worker,
        'checked_at_utc,success,failed_step,failed_status',
        f'{datetime.now(timezone.utc).isoformat()},true,,',
    )
    _age_file(stale_monitor, 4 * 60 * 60)

    monkeypatch.setattr(dashboard, 'OUTPUT_DIR', outputs)

    label, color, note = dashboard._get_worker_health()

    assert label == 'Running (just updated)'
    assert color == '#4ade80'


def test_worker_health_reports_missing_heartbeat(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    monkeypatch.setattr(dashboard, 'OUTPUT_DIR', outputs)

    label, color, note = dashboard._get_worker_health()

    assert label == 'No heartbeat'
    assert note == 'Worker has not run yet.'
