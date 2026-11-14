from __future__ import annotations

import asyncio
import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

import options_api


def _run_asgi(app, method: str, path: str, **kwargs):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='https://testserver') as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


def _configure_auth(monkeypatch) -> None:
    options_api._failed_logins.clear()
    monkeypatch.setenv('OPTIONS_API_EMAIL', 'owner@example.test')
    monkeypatch.setenv('OPTIONS_API_PASSWORD_HASH', options_api.hash_password('correct horse battery staple'))
    monkeypatch.setenv('OPTIONS_API_JWT_SECRET', 'a-test-secret-at-least-thirty-two-bytes-long')
    monkeypatch.setenv('OPTIONS_API_ALLOWED_ORIGINS', 'https://testserver')
    monkeypatch.setenv('OPTIONS_API_COOKIE_SECURE', 'true')


def _login(client: httpx.AsyncClient, email: str = 'owner@example.test', password: str = 'correct horse battery staple'):
    return client.post(
        '/api/v1/auth/login', json={'email': email, 'password': password},
        headers={'Origin': 'https://testserver'},
    )


def test_auth_requires_config_and_protects_read_endpoints(monkeypatch) -> None:
    monkeypatch.delenv('OPTIONS_API_EMAIL', raising=False)
    monkeypatch.delenv('OPTIONS_API_PASSWORD_HASH', raising=False)
    monkeypatch.delenv('OPTIONS_API_JWT_SECRET', raising=False)
    response = _run_asgi(options_api.app, 'GET', '/api/v1/positions')
    assert response.status_code == 503

    _configure_auth(monkeypatch)
    response = _run_asgi(options_api.app, 'GET', '/api/v1/positions')
    assert response.status_code == 401


def test_login_sets_secure_http_only_cookie_and_session(monkeypatch) -> None:
    _configure_auth(monkeypatch)

    async def request_flow():
        transport = httpx.ASGITransport(app=options_api.app)
        async with httpx.AsyncClient(transport=transport, base_url='https://testserver') as client:
            response = await client.post(
                '/login', json={'email': ' OWNER@example.test ', 'password': 'correct horse battery staple'},
                headers={'Origin': 'https://testserver'},
            )
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            cookie = response.headers['set-cookie'].lower()
            assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie
            session = await client.get('/api/v1/auth/session')
            assert session.status_code == 200
            assert session.json() == {'authenticated': True, 'email': 'owner@example.test'}

    asyncio.run(request_flow())


def test_login_rejects_bad_password_origin_and_rate_limited_attempts(monkeypatch) -> None:
    _configure_auth(monkeypatch)
    bad_origin = _run_asgi(
        options_api.app, 'POST', '/api/v1/auth/login',
        json={'email': 'owner@example.test', 'password': 'correct horse battery staple'},
        headers={'Origin': 'https://evil.example'},
    )
    assert bad_origin.status_code == 403

    async def request_flow():
        transport = httpx.ASGITransport(app=options_api.app)
        async with httpx.AsyncClient(transport=transport, base_url='https://testserver') as client:
            for _ in range(options_api._LOGIN_MAX_ATTEMPTS):
                response = await _login(client, password='wrong password value')
                assert response.status_code == 401
            limited = await _login(client, password='wrong password value')
            assert limited.status_code == 429

    asyncio.run(request_flow())


def test_read_endpoints_use_fixed_datasets_and_read_only_iv_database(tmp_path: Path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    database = outputs / 'options_safety.sqlite3'
    monkeypatch.setattr(options_api, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(options_api, 'DATABASE_PATH', database)
    _configure_auth(monkeypatch)

    with (outputs / 'broker_positions.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['symbol', 'side', 'qty'])
        writer.writeheader()
        writer.writerow({'symbol': 'XLF261123C00058000', 'side': 'long', 'qty': 1})
    with (outputs / 'execution_log.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['status', 'rejection_code', 'detail'])
        writer.writeheader()
        writer.writerow({'status': 'blocked', 'rejection_code': 'REJECTED_SPREAD_WIDTH', 'detail': 'wide'})

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE iv_history (symbol TEXT, date TEXT, atm_iv_30d REAL)')
        for offset in range(24, 0, -1):
            connection.execute(
                'INSERT INTO iv_history VALUES (?, ?, ?)',
                ('XLF', (date.today() - timedelta(days=offset)).isoformat(), 0.1 + offset / 100),
            )
        connection.commit()
    with sqlite3.connect(database) as connection:
        original_rows = connection.execute('SELECT COUNT(*) FROM iv_history').fetchone()[0]

    async def request_flow():
        transport = httpx.ASGITransport(app=options_api.app)
        async with httpx.AsyncClient(transport=transport, base_url='https://testserver') as client:
            login_response = await _login(client)
            assert login_response.status_code == 200
            positions_response = await client.get('/api/v1/positions')
            assert positions_response.json()['rows'][0]['symbol'] == 'XLF261123C00058000'
            blocks_response = await client.get('/api/v1/guardrails')
            assert blocks_response.json()['blocks'][0]['rejection_code'] == 'REJECTED_SPREAD_WIDTH'
            ranks_response = await client.get('/api/v1/iv-ranks')
            assert ranks_response.status_code == 200
            assert ranks_response.json()['rows'][0]['symbol'] == 'XLF'
            assert ranks_response.json()['rows'][0]['observations'] == 23

    asyncio.run(request_flow())
    connection = sqlite3.connect(database)
    assert connection.execute('SELECT COUNT(*) FROM iv_history').fetchone()[0] == original_rows
    connection.close()


def test_csv_reader_does_not_accept_a_request_path(tmp_path: Path, monkeypatch) -> None:
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    monkeypatch.setattr(options_api, 'OUTPUT_DIR', outputs)
    assert options_api.CSV_FILES['positions'] == 'broker_positions.csv'
    with pytest.raises(KeyError):
        options_api._read_csv('../.env')


def test_static_pwa_shell_is_served_without_exposing_api_data() -> None:
    page = _run_asgi(options_api.app, 'GET', '/')
    assert page.status_code == 200
    assert 'Options Monitor' in page.text
    script = _run_asgi(options_api.app, 'GET', '/app.js')
    assert script.status_code == 200
    assert '/api/v1/positions' in script.text
    login_page = _run_asgi(options_api.app, 'GET', '/login')
    assert login_page.status_code == 200
    assert 'Options Monitor' in login_page.text
    protected = _run_asgi(options_api.app, 'GET', '/api/v1/positions')
    assert protected.status_code in {401, 503}


def test_missing_control_audit_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(options_api, 'OUTPUT_DIR', tmp_path)
    assert options_api._control_miss_active()