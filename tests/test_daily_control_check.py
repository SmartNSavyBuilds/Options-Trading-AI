from __future__ import annotations

from datetime import datetime, timezone

import daily_control_check as control


def _write_csv(path, header: str, row: str) -> None:
    path.write_text(f"{header}\n{row}\n", encoding='utf-8')


def test_compute_row_reads_journaled_at_utc_for_performance_age(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    now = datetime.now(timezone.utc).isoformat()

    _write_csv(
        outputs / 'monitor_status.csv',
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        f'{now},running,connected,open,0,ok',
    )
    _write_csv(
        outputs / 'performance_summary.csv',
        'journaled_at_utc,open_positions,total_market_value,total_unrealized_pl',
        f'{now},0,0.0,0.0',
    )
    _write_csv(
        outputs / 'broker_account_status.csv',
        'synced_at_utc,equity',
        f'{now},100000',
    )

    monkeypatch.setattr(control, 'OUTPUT_DIR', outputs)

    row = control._compute_row()

    assert row['performance_stale'] is False
    assert float(row['performance_age_minutes']) < 5.0


def test_compute_row_flags_broker_auth_unauthorized(tmp_path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    now = datetime.now(timezone.utc).isoformat()

    _write_csv(
        outputs / 'monitor_status.csv',
        'last_run_utc,monitor_status,connection_status,market_status,open_positions,note',
        f'{now},running,connected,open,0,ok',
    )
    _write_csv(
        outputs / 'performance_summary.csv',
        'journaled_at_utc,open_positions,total_market_value,total_unrealized_pl',
        f'{now},0,0.0,0.0',
    )
    _write_csv(
        outputs / 'broker_account_status.csv',
        'synced_at_utc,connection_status,detail,equity',
        f'{now},unauthorized,401 Client Error: Unauthorized for url: https://paper-api.alpaca.markets/v2/account,100000',
    )

    monkeypatch.setattr(control, 'OUTPUT_DIR', outputs)

    row = control._compute_row()

    assert row['broker_auth_unauthorized'] is True
    assert 'broker_auth_unauthorized' in row['control_miss_reasons']
