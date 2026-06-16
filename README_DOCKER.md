# Options Dashboard Docker Runbook

## What was added

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `requirements-dashboard.txt`

These files run the Streamlit dashboard in a container on internal port `8502`
and publish it on host port `8503`. They also support a separate automation
worker for control audits and execution playbooks.

Additional hardening assets:

- `healthcheck_dashboard.py`
- `healthcheck_worker.py`
- `pre_live_gate_check.py`
- `run_pre_live_gate_check.ps1`
- `.env.example`

## Start the dashboard in Docker

From `c:\claw-code\projects\options_trading_ai`:

### One-command launcher (recommended on Windows)

```powershell
.\start_dashboard_docker.ps1
```

If PowerShell policy blocks execution or you are in `C:\WINDOWS\system32`, use the command wrapper:

```cmd
start_dashboard_docker.cmd
```

Or run PowerShell explicitly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "c:\claw-code\projects\options_trading_ai\start_dashboard_docker.ps1"
```

This script starts Docker service/Desktop when needed, waits for daemon readiness, and runs `docker compose up --build -d`.

### Manual command

```powershell
docker compose up --build -d
```

Open:

- `http://localhost:8503`

This starts both services defined in compose:

- `options-dashboard` for the Streamlit UI
- `options-worker` for background automation cycles

### Optional secure access profile (recommended for cross-device access)

Start dashboard + worker + auth gateway:

```powershell
docker compose --profile secure up --build -d
```

Open:

- `http://localhost:8080` (gateway login)

Required env values for gateway profile:

- `GATEWAY_EMAIL`
- `GATEWAY_PASSWORD`
- `GATEWAY_SECRET_KEY`

Optional hardening values for internet-facing/mobile access:

- `SESSION_COOKIE_SECURE=true` when the gateway is behind HTTPS
- `SESSION_COOKIE_DOMAIN=your-domain.example` if you want a fixed cookie domain

## Stop the dashboard

```powershell
docker compose down
```

## View logs

```powershell
docker compose logs -f options-dashboard
```

Worker logs:

```powershell
docker compose logs -f options-worker
```

## Run dashboard locally with venv (fallback)

Use this when Docker is not healthy yet.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "c:\claw-code\projects\options_trading_ai\start_dashboard_local.ps1"
```

This launcher always uses `c:\claw-code\.venv\Scripts\python.exe`, which avoids `ModuleNotFoundError` issues from global Python.

## Data and configuration behavior

- `.env` is injected at runtime via `env_file`.
- `./outputs` is mounted to `/app/outputs` so generated CSVs persist on your host.
- The dashboard container runs:

```bash
python -m streamlit run dashboard.py --server.port 8502 --server.headless true --server.address 0.0.0.0
```

- The worker container runs:

```bash
python automation_worker.py
```

### Worker automation controls

The worker reads these runtime settings from `.env` or the host environment:

- `OPERATION_MODE=manual|supervised|autonomous`
- `WORKER_PLAYBOOK=refresh_controls|cancel_stale|exit_recovery|full_self_heal`
- `WORKER_LOOP=true|false`
- `WORKER_INTERVAL_SECONDS=300`
- `WORKER_MIN_SLEEP_SECONDS=30`
- `WORKER_MAX_CYCLES=0`
- `WORKER_ENABLE_REFRESH=true|false`
- `WORKER_ENFORCE_FRESHNESS=true|false`
- `AGGRESSIVE_CIRCULATION_ENABLED=true|false`
- `AGGRESSIVE_CIRCULATION_LOOPS=2`
- `AGGRESSIVE_CIRCULATION_PAUSE_SECONDS=5`
- `OPS_WEBHOOK_URL=` optional JSON webhook for cycle success/failure notifications

Mode defaults:

- `manual` runs the refresh-controls playbook only
- `supervised` runs exit recovery steps
- `autonomous` runs the full self-heal playbook

When `OPERATION_MODE=autonomous` and `AGGRESSIVE_CIRCULATION_ENABLED=true`,
the worker also runs extra circulation passes each cycle:

- refresh opportunities and signal files
- refresh broker orders
- evaluate exits and route exits
- route new entries
- refresh control audit

Use `AGGRESSIVE_CIRCULATION_LOOPS` to increase/decrease how many extra passes run
inside one worker cycle, and `AGGRESSIVE_CIRCULATION_PAUSE_SECONDS` for pacing.

The worker writes a heartbeat snapshot to `outputs/worker_status.csv` after each cycle.

It also writes feed freshness evidence to `outputs/feed_freshness_status.csv`.

Container healthchecks:

- dashboard checks `http://127.0.0.1:8502`
- worker checks freshness of `outputs/worker_status.csv`
- gateway checks `http://127.0.0.1:8080/_health`

### Live trading safety preflight

Before any live-money routing, run the one-command readiness check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\run_live_readiness_check.ps1"
```

This command runs:

- `daily_control_check.py`
- `plan_control_cleanup.py`
- `live_trading_preflight.py`

Outputs:

- `outputs/live_trading_preflight.json`
- `outputs/control_cleanup_plan.md`
- `outputs/control_cleanup_plan.csv`

The automation worker now enforces this gate in `TRADING_MODE=live` and blocks routing steps when preflight fails.

### Consolidated pre-live gate report

Run one command to evaluate worker heartbeat, control audit status, live preflight status,
and decision engine telemetry:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\run_pre_live_gate_check.ps1"
```

Outputs:

- `outputs/pre_live_gate_report.json`
- `outputs/live_trading_preflight.json`

Use this before enabling real-money routing.

### Agentic controller modes

Use the agent controller to run one constrained autonomy cycle with explicit mode semantics:

```powershell
cd c:\claw-code\projects\options_trading_ai
set AGENT_RUNTIME_MODE=observe
python agentic_controller.py
```

Supported values:

- `observe`: decision engine in shadow, no route application
- `propose`: supervised cycle with route application disabled
- `execute`: supervised cycle with AI route application enabled

Optional loop mode:

```powershell
set AGENT_RUNTIME_MODE=execute
set AGENT_RUNTIME_LOOP=true
set AGENT_RUNTIME_INTERVAL_SECONDS=300
set AGENT_RUNTIME_MAX_CYCLES=12
python agentic_controller.py
```

Controller output artifacts:

- `outputs/agentic_runtime_status.csv`
- `outputs/worker_status.csv`
- `outputs/decision_engine_cycles.jsonl`

### Feed refresh and freshness gate

The worker executes this refresh chain each loop before playbook actions:

- `refresh_broker_snapshot.py`
- `refresh_broker_orders.py`
- `discover_opportunities.pyc`
- `enrich_opportunity_discovery.py`
- `refresh_signal_feeds.py`

The congressional refresh writes these outputs each cycle:

- `congressional_disclosures.csv`
- `congressional_summary.csv`

The broker snapshot step writes fresh account and holdings inputs used by the dashboard and controls:

- `broker_account_status.csv`
- `broker_positions.csv`

The broker orders step writes fresh order-state input used by dashboard alerts, cancel logic, and control checks:

- `broker_orders.csv`

The discovery enrichment step augments each opportunity row with liquidity,
tradability, catalyst timing, regime alignment, confidence, crowding, and churn
metadata and writes:

- `opportunity_discovery.csv` (enriched columns)
- `opportunity_discovery_history.csv` (lightweight score history for churn tracking)

Then it enforces feed freshness SLA checks for:

- `latest_signals.csv` (30 min, required)
- `options_candidates.csv` (30 min, required)
- `monitor_status.csv` (15 min, required)
- `broker_account_status.csv` (15 min, required)
- `broker_positions.csv` (15 min, required)
- `broker_orders.csv` (15 min, required)
- `catalyst_news.csv` (120 min)
- `congressional_disclosures.csv` (1440 min)
- `congressional_recent_large.csv` (1440 min)

When required feeds are stale or missing and `WORKER_ENFORCE_FRESHNESS=true`,
supervised/autonomous execution is blocked and the cycle reports `freshness_gate`
as `blocked` in `worker_status.csv`.

## Common troubleshooting

1. If Docker daemon is not running:
```powershell
docker info
```
Expected: no daemon connection error.

2. If port is busy:
- Change compose mapping from `8503:8502` to another host port (for example `8504:8502`).

3. If dependency import errors occur after code changes:
```powershell
docker compose build --no-cache
```

4. If output files look stale:
- Confirm files update under `outputs/` on host.
- Restart with:
```powershell
docker compose down ; docker compose up -d
```

5. If you want a one-off worker cycle without leaving it running:
```powershell
docker compose run --rm -e WORKER_LOOP=false -e OPERATION_MODE=manual options-worker
```

6. If healthcheck reports worker unhealthy:
- Confirm `outputs/worker_status.csv` is updating each cycle.
- Increase `WORKER_HEALTH_MAX_AGE_SECONDS` in `.env` if your cycle interval is intentionally longer.

7. If gateway login works but dashboard is unreachable:
- Set `STREAMLIT_ORIGIN=http://options-dashboard:8502` in `.env` when using Docker compose service names.

## Mobile-friendly remote deployment

For phone access and approval workflows, prefer HTTPS in front of the auth gateway instead of exposing Streamlit directly.

Recommended shape:

- `options-worker` stays private on the host network
- `options-dashboard` stays private behind Docker networking
- `options-gateway` handles sign-in and forwards traffic to Streamlit
- a TLS reverse proxy publishes ports `80/443` and forwards to `options-gateway`

Use `docker-compose.remote.yml` plus `Caddyfile` in this folder for that layout.
