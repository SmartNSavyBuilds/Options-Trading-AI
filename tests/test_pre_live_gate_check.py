from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pre_live_gate_check as gate


def _write_csv(path, header: str, row: str) -> None:
    path.write_text(f"{header}\n{row}\n", encoding='utf-8')


def test_build_report_prefers_fresh_monitor_status(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    now = datetime.now(timezone.utc)
    stale_worker = (now - timedelta(hours=8)).isoformat()
    fresh_monitor = (now - timedelta(minutes=2)).isoformat()

    _write_csv(
        outputs / 'worker_status.csv',
        'checked_at_utc,success,failed_step,failed_status',
        f'{stale_worker},false,refresh_broker_snapshot.py,missing',
    )
    _write_csv(
        outputs / 'monitor_status.csv',
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        f'{fresh_monitor},running,connected,open,0,ok',
    )
    _write_csv(
        outputs / 'control_audit_latest.csv',
        'checked_at_utc,control_miss,control_miss_reasons,queue_auto_held_count',
        f'{fresh_monitor},false,,0',
    )
    _write_csv(
        outputs / 'broker_account_status.csv',
        'synced_at_utc,connection_status,detail',
        f'{fresh_monitor},connected,ok',
    )

    monkeypatch.setattr(gate, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(gate, 'REPORT_FILE', outputs / 'pre_live_gate_report.json')

    report = gate.build_report()

    assert report['checks']['worker_heartbeat_recent']['pass'] is True
    assert report['checks']['worker_heartbeat_recent']['source'] == 'monitor_status'
    assert report['checks']['worker_last_success']['pass'] is True

def test_build_report_fails_when_control_reasons_present_even_if_flag_false(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    _write_csv(
        outputs / 'monitor_status.csv',
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        '2026-07-06T19:03:26.419403+00:00,running,connected,open,2,Full cycle: analytics + live execution completed.',
    )

    _write_csv(
        outputs / 'control_audit_latest.csv',
        'checked_at_utc,control_miss,control_miss_reasons,queue_auto_held_count',
        '2026-07-06T19:05:32.295661+00:00,False,protective_exit_fill_rate,0',
    )

    _write_csv(
        outputs / 'broker_account_status.csv',
        'synced_at_utc,broker,mode,connection_status,market_status,equity,cash,buying_power,portfolio_value,options_approved_level,options_trading_level,options_buying_power,detail',
        '2026-07-06T19:05:32.420296+00:00,alpaca,paper,connected,open,189388.91,186883.91,747535.64,189388.91,3,3,186883.91,ACTIVE',
    )

    monkeypatch.setattr(gate, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(gate, 'REPORT_FILE', outputs / 'pre_live_gate_report.json')

    report = gate.build_report()

    assert report['verdict'] == 'fail'
    assert report['checks']['control_miss_clear']['pass'] is False
    assert report['checks']['control_miss_clear']['control_miss'] is True
    assert report['checks']['control_miss_clear']['reasons'] == 'protective_exit_fill_rate'


def test_build_report_uses_worker_status_when_monitor_missing(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    now = datetime.now(timezone.utc)
    fresh_worker = (now - timedelta(minutes=1)).isoformat()

    _write_csv(
        outputs / 'worker_status.csv',
        'checked_at_utc,success,failed_step,failed_status',
        f'{fresh_worker},true,,',
    )
    _write_csv(
        outputs / 'control_audit_latest.csv',
        'checked_at_utc,control_miss,control_miss_reasons,queue_auto_held_count',
        f'{fresh_worker},false,,0',
    )
    _write_csv(
        outputs / 'broker_account_status.csv',
        'synced_at_utc,connection_status,detail',
        f'{fresh_worker},connected,ok',
    )

    monkeypatch.setattr(gate, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(gate, 'REPORT_FILE', outputs / 'pre_live_gate_report.json')

    report = gate.build_report()

    assert report['checks']['worker_heartbeat_recent']['pass'] is True
    assert report['checks']['worker_heartbeat_recent']['source'] == 'worker_status'
    assert report['checks']['worker_last_success']['pass'] is True

    json.dumps(report)
