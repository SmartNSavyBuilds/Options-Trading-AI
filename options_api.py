from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import sys
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv('OPTIONS_API_OUTPUTS_DIR', PROJECT_DIR / 'outputs')).resolve()
_database_setting = Path(os.getenv('OPTION_SAFETY_DB', 'outputs/options_safety.sqlite3'))
DATABASE_PATH = (_database_setting if _database_setting.is_absolute() else PROJECT_DIR / _database_setting).resolve()
WEB_DIR = PROJECT_DIR / 'web'
JWT_ISSUER = 'options-api'
COOKIE_NAME = 'options_api_session'
TOKEN_TTL_SECONDS = 15 * 60
PASSWORD_SCRYPT_N = 1 << 14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_SCRYPT_BYTES = 32

CSV_FILES = {
    'monitor': 'monitor_status.csv',
    'account': 'broker_account_status.csv',
    'positions': 'broker_positions.csv',
    'performance': 'performance_summary.csv',
    'execution_quality': 'execution_quality.csv',
    'alerts': 'alerts_feed.csv',
    'execution_log': 'execution_log.csv',
    'exits': 'exit_recommendations.csv',
}

app = FastAPI(title='Options Monitoring API', version='1.0.0', docs_url=None, redoc_url=None)
_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5


class LoginRequest(BaseModel):
    email: str
    password: str


@app.middleware('http')
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    no_store = request.url.path.startswith('/api/') or request.url.path in {'/login', '/logout'}
    response.headers['Cache-Control'] = 'no-store' if no_store else 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "manifest-src 'self'; worker-src 'self'; base-uri 'self'; frame-ancestors 'none'"
    )
    return response


def hash_password(password: str) -> str:
    if not password or len(password) < 12:
        raise ValueError('Password must contain at least 12 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode('utf-8'), salt=salt, n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P, dklen=PASSWORD_SCRYPT_BYTES,
    )
    salt_text = base64.urlsafe_b64encode(salt).decode('ascii').rstrip('=')
    digest_text = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return f'scrypt${salt_text}${digest_text}'


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt_text, digest_text = stored_hash.split('$')
        if algorithm != 'scrypt':
            return False
        salt = _decode_b64(salt_text)
        expected = _decode_b64(digest_text)
        if not (16 <= len(salt) <= 64 and len(expected) == PASSWORD_SCRYPT_BYTES):
            return False
        actual = hashlib.scrypt(
            password.encode('utf-8'), salt=salt, n=PASSWORD_SCRYPT_N,
            r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P, dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _auth_config() -> tuple[str, str, str] | None:
    email = os.getenv('OPTIONS_API_EMAIL', '').strip()
    password_hash = os.getenv('OPTIONS_API_PASSWORD_HASH', '').strip()
    jwt_secret = os.getenv('OPTIONS_API_JWT_SECRET', '')
    if not email or not password_hash or len(jwt_secret.encode('utf-8')) < 32:
        return None
    return email, password_hash, jwt_secret


def _allowed_origins() -> set[str]:
    configured = os.getenv(
        'OPTIONS_API_ALLOWED_ORIGINS',
        'https://trade.simplestudiotrades.com,http://127.0.0.1:8504,http://localhost:8504',
    )
    return {origin.strip().rstrip('/') for origin in configured.split(',') if origin.strip()}


def _check_origin(request: Request) -> None:
    origin = request.headers.get('origin', '').strip().rstrip('/')
    if not origin or origin not in _allowed_origins():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Request origin is not allowed.')


def _check_login_rate(ip_address: str) -> None:
    now = time.monotonic()
    attempts = _failed_logins[ip_address]
    while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many login attempts.')


def _read_csv(dataset: str, limit: int = 500) -> dict:
    filename = CSV_FILES[dataset]
    path = OUTPUT_DIR / filename
    if not path.is_file():
        return {'available': False, 'updated_at': None, 'rows': []}
    try:
        stat_result = path.stat()
        if stat_result.st_size == 0:
            return {'available': False, 'updated_at': None, 'rows': []}
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            rows = deque(
                ({key: value if value != '' else None for key, value in row.items() if key} for row in reader),
                maxlen=max(0, limit),
            )
        updated_at = datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat()
        return {'available': True, 'updated_at': updated_at, 'rows': list(rows)}
    except (OSError, UnicodeError, csv.Error):
        return {'available': False, 'updated_at': None, 'rows': []}


def _read_json_file(filename: str) -> dict | None:
    path = OUTPUT_DIR / filename
    try:
        with path.open('r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _sqlite_readonly_connection() -> sqlite3.Connection:
    uri = f'{DATABASE_PATH.as_uri()}?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.execute('PRAGMA query_only = ON')
    return connection


def _latest_iv_ranks() -> list[dict]:
    if not DATABASE_PATH.is_file():
        return []
    try:
        today = date.today()
        with closing(_sqlite_readonly_connection()) as connection:
            rows = connection.execute(
                'SELECT symbol, date, atm_iv_30d FROM iv_history '
                'WHERE date >= ? AND date <= ? ORDER BY symbol, date',
                ((today - timedelta(days=364)).isoformat(), today.isoformat()),
            ).fetchall()
    except sqlite3.Error:
        return []

    by_symbol: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for symbol, observed_date, raw_iv in rows:
        try:
            value = float(raw_iv)
        except (TypeError, ValueError):
            continue
        if symbol and observed_date and math.isfinite(value) and value > 0:
            by_symbol[str(symbol).upper()].append((str(observed_date), value))

    result = []
    for symbol, history in sorted(by_symbol.items()):
        history.sort(key=lambda item: item[0])
        latest_date, latest_iv = history[-1]
        prior_values = [value for observed, value in history if observed < latest_date]
        iv_rank = None
        if len(prior_values) >= 20 and min(prior_values) != max(prior_values):
            iv_rank = round((latest_iv - min(prior_values)) / (max(prior_values) - min(prior_values)) * 100, 2)
        result.append({
            'symbol': symbol,
            'observed_date': latest_date,
            'atm_iv_30d': latest_iv,
            'iv_rank': iv_rank,
            'observations': len(prior_values),
        })
    return result


async def require_user(request: Request) -> str:
    config = _auth_config()
    if config is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='API authentication is not configured.')
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required.')
    try:
        payload = jwt.decode(token, config[2], algorithms=['HS256'], issuer=JWT_ISSUER)
        subject = payload.get('sub')
        if not isinstance(subject, str) or not subject:
            raise jwt.InvalidTokenError('Missing subject')
        return subject
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Session is invalid or expired.') from exc


@app.get('/api/v1/health/live')
async def live() -> dict:
    return {'status': 'ok'}


@app.get('/api/v1/health/ready')
async def ready() -> dict:
    output_ready = OUTPUT_DIR.is_dir()
    database_ready = DATABASE_PATH.is_file()
    auth_ready = _auth_config() is not None
    result = {
        'status': 'ready' if output_ready and database_ready and auth_ready else 'not_ready',
        'outputs_available': output_ready,
        'iv_database_available': database_ready,
        'authentication_configured': auth_ready,
    }
    if result['status'] != 'ready':
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result)
    return result


@app.get('/login', include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(WEB_DIR / 'index.html')


@app.post('/login', include_in_schema=False)
@app.post('/api/v1/auth/login')
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    _check_origin(request)
    ip_address = request.client.host if request.client else 'unknown'
    _check_login_rate(ip_address)
    config = _auth_config()
    if config is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='API authentication is not configured.')
    email_ok = hmac.compare_digest(body.email.strip().lower(), config[0].lower())
    password_ok = _verify_password(body.password, config[1])
    if not (email_ok and password_ok):
        _failed_logins[ip_address].append(time.monotonic())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Incorrect email or password.')

    _failed_logins.pop(ip_address, None)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {'sub': config[0], 'iss': JWT_ISSUER, 'iat': now, 'exp': now + timedelta(seconds=TOKEN_TTL_SECONDS)},
        config[2], algorithm='HS256',
    )
    response.set_cookie(
        COOKIE_NAME, token, max_age=TOKEN_TTL_SECONDS, httponly=True,
        secure=os.getenv('OPTIONS_API_COOKIE_SECURE', 'true').strip().lower() in {'1', 'true', 'yes'},
        samesite='strict', path='/',
    )
    return {'authenticated': True, 'expires_in': TOKEN_TTL_SECONDS}


@app.get('/api/v1/auth/session')
async def session(user: str = Depends(require_user)) -> dict:
    return {'authenticated': True, 'email': user}


@app.post('/api/v1/auth/logout')
async def logout(request: Request, response: Response, _user: str = Depends(require_user)) -> dict:
    _check_origin(request)
    response.delete_cookie(COOKIE_NAME, path='/', httponly=True, samesite='strict')
    return {'authenticated': False}


@app.get('/api/v1/system/status')
async def system_status(_user: str = Depends(require_user)) -> dict:
    monitor = _read_csv('monitor', limit=1)
    account = _read_csv('account', limit=1)
    monitor_age_seconds = None
    if monitor['updated_at']:
        try:
            updated_at = datetime.fromisoformat(monitor['updated_at'])
            monitor_age_seconds = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
        except ValueError:
            pass
    try:
        max_age_seconds = max(1, int(os.getenv('OPTIONS_API_MONITOR_MAX_AGE_SECONDS', '1800')))
    except ValueError:
        max_age_seconds = 1800
    monitor_stale = monitor_age_seconds is None or monitor_age_seconds > max_age_seconds
    control_audit = _read_csv_file_row('control_audit_latest.csv')
    pre_live_gate = _read_json_file('pre_live_gate_report.json')
    gate_failed = not pre_live_gate or str(pre_live_gate.get('verdict', '')).lower() != 'pass'
    control_miss = not control_audit or str(control_audit.get('control_miss', '')).strip().lower() not in {'false', '0', 'no'}
    return {
        'monitor': monitor['rows'][0] if monitor['rows'] else None,
        'monitor_updated_at': monitor['updated_at'],
        'monitor_age_seconds': monitor_age_seconds,
        'monitor_stale': monitor_stale,
        'account': account['rows'][0] if account['rows'] else None,
        'account_updated_at': account['updated_at'],
        'control_audit': control_audit,
        'pre_live_gate': pre_live_gate,
        'health': 'review_required' if monitor_stale or control_miss or gate_failed else 'nominal',
    }


def _read_csv_file_row(filename: str) -> dict | None:
    if filename != 'control_audit_latest.csv':
        return None
    data = _read_csv_path(OUTPUT_DIR / filename)
    return data['rows'][0] if data['rows'] else None


def _read_csv_path(path: Path) -> dict:
    if not path.is_file():
        return {'available': False, 'updated_at': None, 'rows': []}
    try:
        stat_result = path.stat()
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))[:1]
        return {
            'available': bool(rows),
            'updated_at': datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(),
            'rows': [{key: value if value != '' else None for key, value in row.items()} for row in rows],
        }
    except (OSError, UnicodeError, csv.Error):
        return {'available': False, 'updated_at': None, 'rows': []}


def _control_miss_active() -> bool:
    row = _read_csv_file_row('control_audit_latest.csv')
    return not row or str(row.get('control_miss', '')).strip().lower() not in {'false', '0', 'no'}


@app.get('/api/v1/positions')
async def positions(
    limit: int = Query(default=100, ge=1, le=500),
    _user: str = Depends(require_user),
) -> dict:
    return _read_csv('positions', limit=limit)


@app.get('/api/v1/performance')
async def performance(_user: str = Depends(require_user)) -> dict:
    return {
        'summary': _read_csv('performance', limit=1),
        'execution_quality': _read_csv('execution_quality', limit=20),
    }


@app.get('/api/v1/iv-ranks')
async def iv_ranks(_user: str = Depends(require_user)) -> dict:
    return {'available': DATABASE_PATH.is_file(), 'rows': _latest_iv_ranks()}


@app.get('/api/v1/guardrails')
async def guardrails(
    limit: int = Query(default=100, ge=1, le=500),
    _user: str = Depends(require_user),
) -> dict:
    log = _read_csv('execution_log', limit=500)
    blocks = [
        row for row in log['rows']
        if str(row.get('status', '')).lower() in {'blocked', 'rejected'}
        or str(row.get('rejection_code', '')).startswith('REJECTED_')
    ]
    alerts = _read_csv('alerts', limit=limit)
    exits = _read_csv('exits', limit=limit)
    return {
        'blocks': blocks[-limit:],
        'alerts': alerts['rows'],
        'exit_recommendations': exits['rows'],
        'updated_at': log['updated_at'],
    }


app.mount('/', StaticFiles(directory=WEB_DIR, html=True), name='web')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Options monitoring API utilities')
    parser.add_argument('--hash-password', action='store_true', help='Prompt for and print a scrypt password hash')
    arguments = parser.parse_args()
    if arguments.hash_password:
        password = getpass('New API password (12+ characters): ')
        confirmation = getpass('Confirm API password: ')
        if password != confirmation:
            raise SystemExit('Passwords did not match.')
        print(hash_password(password))
    else:
        import uvicorn

        uvicorn.run('options_api:app', host='0.0.0.0', port=8080)