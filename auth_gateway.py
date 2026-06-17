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

import asyncio
import hmac
import os
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, FastAPI, Form, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

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


HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


def _build_target_url(path: str, query_params, websocket: bool = False) -> str:
    upstream_base = STREAMLIT_ORIGIN.rstrip('/')
    if websocket:
        if upstream_base.startswith('https://'):
            upstream_base = 'wss://' + upstream_base[len('https://'):]
        elif upstream_base.startswith('http://'):
            upstream_base = 'ws://' + upstream_base[len('http://'):]

    target = f'{upstream_base}/{path}'
    if query_params:
        target += f'?{urlencode(list(query_params.multi_items()), doseq=True)}'
    return target


def _filter_upstream_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


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

@app.websocket('/{path:path}')
async def websocket_proxy(websocket: WebSocket, path: str) -> None:
    session_token = websocket.cookies.get(SESSION_COOKIE)
    if not _session_valid(session_token):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    target_url = _build_target_url(path, websocket.query_params, websocket=True)

    request_headers = {
        key: value
        for key, value in websocket.headers.items()
        if key.lower() not in {'host', 'connection', 'upgrade', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions'}
    }

    subprotocols_header = websocket.headers.get('sec-websocket-protocol', '')
    subprotocols = [proto.strip() for proto in subprotocols_header.split(',') if proto.strip()]

    try:
        async with ws_connect(
            target_url,
            additional_headers=request_headers,
            subprotocols=subprotocols,
            max_size=None,
        ) as upstream:

            async def client_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    message_type = message.get('type')

                    if message_type == 'websocket.disconnect':
                        break
                    if message.get('text') is not None:
                        await upstream.send(message['text'])
                    elif message.get('bytes') is not None:
                        await upstream.send(message['bytes'])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                error = task.exception()
                if error and not isinstance(error, (WebSocketDisconnect, ConnectionClosed)):
                    raise error

    except (WebSocketDisconnect, ConnectionClosed):
        return
    except Exception:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)
        return
    finally:
        if websocket.client_state == WebSocketState.CONNECTED:
            with suppress(RuntimeError):
                await websocket.close()

@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
async def proxy(
    request: Request,
    path: str,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Response:
    if not _session_valid(session_token):
        return RedirectResponse(url='/login', status_code=303)

    target_url = _build_target_url(path, request.query_params)

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != 'host'
    }
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
        headers=_filter_upstream_response_headers(upstream.headers),
    )
