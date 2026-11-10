$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

$pythonExe = 'c:\claw-code\.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) {
    Write-Error "Configured Python environment was not found: $pythonExe"
    exit 1
}

Write-Host 'Starting local Streamlit dashboard with project virtual environment...'
& $pythonExe -m streamlit run dashboard.py --server.port 8502 --server.headless true --server.address 0.0.0.0 --server.enableCORS false --server.enableXsrfProtection false

exit $LASTEXITCODE
