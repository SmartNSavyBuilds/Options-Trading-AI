# Mobile Deployment Guide

This project can run as an always-on trading agent while you monitor and approve trades from a phone.

## Target Shape

- `options-worker` runs the autonomous loop continuously.
- `options-dashboard` stays private inside Docker.
- `options-gateway` handles sign-in and forwards traffic to the dashboard.
- `options-proxy` terminates HTTPS and publishes the app on ports `80/443`.

## Required Host

Use an always-on Linux or Windows VM with:

- Docker Engine and Docker Compose
- a fixed public IP or DNS name
- ports `80` and `443` open
- environment secrets stored outside source control

## Required Environment Values

Set these in `.env` before remote deployment:

```env
TRADING_MODE=paper
OPERATION_MODE=supervised
WORKER_PLAYBOOK=full_self_heal
WORKER_LOOP=true
WORKER_INTERVAL_SECONDS=300

GATEWAY_EMAIL=you@example.com
GATEWAY_PASSWORD=change-this
GATEWAY_SECRET_KEY=generate-a-long-random-secret
STREAMLIT_ORIGIN=http://options-dashboard:8502
SESSION_MAX_AGE=28800
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_DOMAIN=your-domain.example

DOMAIN=your-domain.example
TLS_EMAIL=you@example.com
```

Keep `TRADING_MODE=paper` until live preflight is consistently clean.

## Start Remote Stack

From the project directory:

```powershell
docker compose -f docker-compose.yml -f docker-compose.remote.yml --profile secure up --build -d
```

This starts:

- Streamlit dashboard
- automation worker
- auth gateway
- Caddy HTTPS reverse proxy

## Mobile Access

Open this URL on your phone:

```text
https://your-domain.example
```

Expected flow:

1. Sign in through the gateway.
2. Review control status, queue state, and broker health.
3. Approve or reject queued trades from the dashboard.
4. Let the worker continue running without the laptop.

## Approval Mode Recommendation

For mobile-first operation:

- keep the worker autonomous for discovery and refresh
- keep trade approval manual
- promote to live only after paper routing and pre-live gates stay stable

## Safe Promotion Path

1. Run paper mode remotely for several sessions.
2. Confirm `worker_status.csv`, `control_audit_latest.csv`, and `pre_live_gate_report.json` stay green.
3. Approve only entries with resolved live pricing.
4. Move to very small live size.
5. Turn on live mode only after `run_live_readiness_check.ps1` and `run_pre_live_gate_check.ps1` pass.

## Operational Notes

- If the host reboots, Docker restart policies bring services back automatically.
- If you rotate your domain, update `DOMAIN` and `SESSION_COOKIE_DOMAIN` together.
- Do not expose Streamlit directly to the internet.
- Do not reuse paper and live credentials.

## Next Build Steps

Useful follow-up improvements after this first deployment slice:

1. Add webhook alerts for stale worker heartbeat and failed gates.
2. Add a broker-specific live approval audit log.
3. Add VPN or IP allowlist in front of the gateway for tighter access control.
4. Move session storage from memory to Redis if you want durable multi-session logins.
