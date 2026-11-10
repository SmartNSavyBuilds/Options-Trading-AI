$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

Write-Host 'Running daily control check...'
python daily_control_check.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host 'Running pre-live gate check...'
python pre_live_gate_check.py
exit $LASTEXITCODE
