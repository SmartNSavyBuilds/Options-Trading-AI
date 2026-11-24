# Options Dashboard Docker Runbook

## What was added

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `requirements-dashboard.txt`

These files run the Streamlit dashboard in a container on port `8502`.

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

- `http://localhost:8502`

## Stop the dashboard

```powershell
docker compose down
```

## View logs

```powershell
docker compose logs -f options-dashboard
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
- The container runs:

```bash
python -m streamlit run dashboard.py --server.port 8502 --server.headless true --server.address 0.0.0.0
```

Before writing the paper-trade queue, the queue builder rechecks the underlying
quote and the candidate's exact debit-vertical contract pair and both option
quotes. Quotes must be no older than 60 seconds and each option spread must be
at most 6%; stricter configured limits still apply. Candidates without matching
spread metadata, including single-leg candidates, are excluded. Skips and symbol
cooldowns are recorded in the safety SQLite database. An empty cycle atomically
replaces the queue with a header-only CSV. Execution performs its own independent
checks again; discovery validation does not authorize or submit orders.

## Common troubleshooting

1. If Docker daemon is not running:
```powershell
docker info
```
Expected: no daemon connection error.

2. If port is busy:
- Change compose mapping from `8502:8502` to another host port (for example `8503:8502`).

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
