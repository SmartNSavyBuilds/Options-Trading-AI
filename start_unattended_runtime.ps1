$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

if (-not (Test-Path '.env')) {
    Write-Error 'Missing .env in the project root. Copy .env.example and fill in the required values before starting unattended runtime.'
    exit 1
}

Write-Host 'Starting Docker-based unattended runtime...'
& "$PSScriptRoot\start_dashboard_docker.ps1"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host 'Registering Windows task scheduler entries for long-running automation...'
& "$PSScriptRoot\setup_monitor_task.ps1"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host 'Running readiness gate checks before autonomous mode...' 
& "$PSScriptRoot\run_pre_live_gate_check.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Pre-live readiness check reported a non-zero exit code. Review outputs/pre_live_gate_report.json before enabling live execution.'
}

Write-Host ''
Write-Host 'Runtime has been started.'
Write-Host 'Key commands:'
Write-Host '  docker compose ps'
Write-Host '  docker compose logs -f options-dashboard'
Write-Host '  docker compose logs -f options-worker'
Write-Host '  schtasks /Query /TN OptionsMonitor'
Write-Host '  schtasks /Query /TN OptionsDashboard'
