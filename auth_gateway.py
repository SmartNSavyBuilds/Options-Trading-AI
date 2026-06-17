"""
auth_gateway.py
---------------
Lightweight FastAPI app that gates access to the Streamlit dashboard.
Runs as a reverse proxy: authenticates a visitor with email + password,
then proxies all subsequent traffic to the Streamlit process.

Quick start (single user self-hosted):
    pip install fastapi uvicorn httpx python-multipart
    GATEWAY_EMAIL=you@example.com GATEWAY_PASSWORD=yourpassword \
        uvicorn auth_gateway:app --host 0.0.0.0 --port 8080

The dashboard must already be running (default: http://localhost:8502).
Set STREAMLIT_ORIGIN to point at a different host/port if needed.

Multi-user future path:
    Replace the single GATEWAY_EMAIL/PASSWORD check with a database
    lookup and issue signed JWT cookies per user instead.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Annotated

import httpx
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

# ── Config ─────────────────────────────────────────────────────────────────

GATEWAY_EMAIL = os.getenv('GATEWAY_EMAIL', '')
GATEWAY_PASSWORD = os.getenv('GATEWAY_PASSWORD', '')
STREAMLIT_ORIGIN = os.getenv('STREAMLIT_ORIGIN', 'http://localhost:8502')
SESSION_COOKIE = 'tradedesk_session'
SESSION_MAX_AGE_SECONDS = int(os.getenv('SESSION_MAX_AGE', str(60 * 60 * 8)))  # 8 hours default
SECRET_KEY = os.getenv('GATEWAY_SECRET_KEY', secrets.token_hex(32))
SESSION_COOKIE_SECURE = str(os.getenv('SESSION_COOKIE_SECURE', 'false')).strip().lower() in {'1', 'true', 'yes', 'on'}
SESSION_COOKIE_DOMAIN = os.getenv('SESSION_COOKIE_DOMAIN', '').strip()

# In-memory session store (replace with Redis or DB for multi-user)
_active_sessions: dict[str, datetime] = {}

app = FastAPI(title='Trade Desk Gateway', docs_url=None, redoc_url=None)

# ── Helpers ─────────────────────────────────────────────────────────────────

def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _session_valid(token: str | None) -> bool:
    if not token or token not in _active_sessions:
        return False
    expires = _active_sessions[token]
    if datetime.now(timezone.utc) > expires:
        _active_sessions.pop(token, None)
        return False
    return True


def _new_session() -> str:
    token = secrets.token_urlsafe(48)
    _active_sessions[token] = datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    return token


# ── Login page ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Desk — Sign In</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f1117;color:#fafafa;display:flex;align-items:center;
       justify-content:center;min-height:100vh}
  .card{background:#1a1d27;border:1px solid #2c2f3e;border-radius:12px;
        padding:2.5rem 2rem;width:100%;max-width:360px;box-shadow:0 8px 32px #0008}
  h1{font-size:1.4rem;font-weight:700;margin-bottom:.25rem;color:#fff}
  p{font-size:.85rem;color:#888;margin-bottom:2rem}
  label{display:block;font-size:.8rem;color:#aaa;margin-bottom:.35rem}
  input{width:100%;background:#262a36;border:1px solid #3a3d4e;border-radius:7px;
        padding:.65rem .9rem;font-size:.95rem;color:#fff;outline:none;margin-bottom:1.2rem}
  input:focus{border-color:#7c6cfc}
  button{width:100%;background:#7c6cfc;border:none;border-radius:7px;
         padding:.75rem;font-size:1rem;font-weight:600;color:#fff;cursor:pointer;
         transition:opacity .15s}
  button:hover{opacity:.88}
  .err{color:#f87171;font-size:.82rem;margin-top:1rem;text-align:center}
</style>
</head>
<body>
<div class="card">
  <h1>Trade Desk</h1>
  <p>Sign in to access your trading dashboard.</p>
  <form method="post" action="/login">
    <label>Email</label>
    <input type="email" name="email" autocomplete="email" required>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
    {error_block}
  </form>
</div>
</body>
</html>"""


@app.get('/_health')
async def health() -> dict[str, str]:
    return {
        'status': 'ok',
        'service': 'auth_gateway',
        'streamlit_origin': STREAMLIT_ORIGIN,
    }


@app.get('/login', response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML.replace('{error_block}', ''))


@app.post('/login')
async def login_submit(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    response: Response,
) -> Response:
    email_ok = _constant_time_compare(email.strip().lower(), GATEWAY_EMAIL.strip().lower())
    pwd_ok = _constant_time_compare(password, GATEWAY_PASSWORD)
    if not (email_ok and pwd_ok):
        html = LOGIN_HTML.replace(
            '{error_block}',
            '<p class="err">Incorrect email or password.</p>',
        )
        return HTMLResponse(html, status_code=401)

    token = _new_session()
    redir = RedirectResponse(url='/', status_code=303)
    redir.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite='lax',
        secure=SESSION_COOKIE_SECURE,
        domain=SESSION_COOKIE_DOMAIN or None,
    )
    return redir


@app.get('/logout')
async def logout(response: Response, token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> Response:
    if token:
        _active_sessions.pop(token, None)
    redir = RedirectResponse(url='/login', status_code=303)
    redir.delete_cookie(SESSION_COOKIE)
    return redir


# ── Proxy all other traffic through to Streamlit ───────────────────────────

@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
async def proxy(
    request: Request,
    path: str,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Response:
    if not _session_valid(session_token):
        return RedirectResponse(url='/login', status_code=303)

    target_url = f'{STREAMLIT_ORIGIN.rstrip("/")}/{path}'
    if request.query_params:
        target_url += f'?{str(request.query_params)}'

    headers = dict(request.headers)
    headers.pop('host', None)
    body = await request.body()

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                follow_redirects=False,
            )
        except httpx.ConnectError:
            return HTMLResponse(
                '<h2 style="font-family:sans-serif;color:#f87171;padding:2rem">'
                'Dashboard is not reachable. Start the Streamlit server first.</h2>',
                status_code=502,
            )

    return StreamingResponse(
        content=iter([upstream.content]),
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )
