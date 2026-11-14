# Options Trading AI - Windows PowerShell Command Guide

This guide gives you one place for the core commands to run, monitor, recover, and automate the options trading workflow on Windows.

Project root:
- c:\claw-code\projects\options_trading_ai

Python environment used by this project:
- c:\claw-code\.venv\Scripts\python.exe

## 1) Open PowerShell in the project folder

```powershell
Set-Location c:\claw-code\projects\options_trading_ai
```

If script execution is blocked in your current PowerShell session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

## 2) Core one-time checks

Run broker/account sync snapshot:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\sync_broker_state.py
```

Run daily controls:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\daily_control_check.py
```

Run pre-live gate check:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\pre_live_gate_check.py
```

Run the bundled pre-live PowerShell wrapper:

```powershell
.\run_pre_live_gate_check.ps1
```

## 3) Build signals and queue

Generate fresh signals and option candidates:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\app.py
```

Build/refresh paper trade queue:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\paper_trade.py
```

## 4) Execute paper workflow manually

Run entry execution:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\execute_paper_trades.py
```

Run exit evaluation:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\evaluate_exit_rules.py
```

Run exit execution:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\execute_exit_trades.py
```

## 5) Deterministic full pipeline (recommended operator run)

Normal deterministic run:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\run_deterministic_pipeline.py
```

Allow disconnected broker (diagnostics only):

```powershell
c:\claw-code\.venv\Scripts\python.exe .\run_deterministic_pipeline.py --allow-disconnected
```

Fail if gate report fails:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\run_deterministic_pipeline.py --strict-gate
```

## 6) Continuous worker loop

Run market monitor one cycle:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\market_monitor.py
```

Run market monitor in loop every 15 minutes:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\market_monitor.py --loop --interval-seconds 900
```

## 7) Dashboard

Start Streamlit dashboard:

```powershell
c:\claw-code\.venv\Scripts\python.exe -m streamlit run .\dashboard.py --server.port 8501
```

## 8) Recovery after auth/config issues

Run broker auth recovery reconciliation:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\reconcile_after_broker_auth.py
```

## 9) Supporting data refresh tasks

Refresh broker snapshot:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\refresh_broker_snapshot.py
```

Refresh congressional reports:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\refresh_congressional_reports.py
```

Import congressional disclosures:

```powershell
c:\claw-code\.venv\Scripts\python.exe .\import_congressional_trades.py
```

## 10) Automation with Windows Task Scheduler

Register monitor and dashboard startup tasks (run once as Administrator):

```powershell
.\setup_monitor_task.ps1
```

Start scheduled tasks immediately:

```powershell
schtasks /Run /TN OptionsMonitor
schtasks /Run /TN OptionsDashboard
```

## 11) Logs and output files to check first

Primary output folder:
- c:\claw-code\projects\options_trading_ai\outputs

Useful status files:
- monitor_status.csv
- worker_status.csv
- deterministic_pipeline_last_run.json
- broker_auth_diagnostic.json
- execution_log.csv
- paper_trade_queue.csv

## 12) Quick troubleshooting commands

Show the most recent monitor status:

```powershell
Get-Content .\outputs\monitor_status.csv
```

Show recent deterministic pipeline summary:

```powershell
Get-Content .\outputs\deterministic_pipeline_last_run.json
```

Run targeted test for 401 options-contract auth handling:

```powershell
c:\claw-code\.venv\Scripts\python.exe -m pytest .\tests\test_trading_enhancements.py::test_fetch_option_contracts_handles_401_without_raising -q
```

---

Last updated: 2026-06-24
