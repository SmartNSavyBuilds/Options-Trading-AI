# Setup Windows Task Scheduler entries for the Options Trading AI system.
# Uses schtasks.exe directly to avoid PowerShell cmdlet hangs.
# Run this script ONCE as Administrator.

$workingDir = "c:\claw-code\projects\options_trading_ai"
$pythonExe  = "c:\claw-code\.venv\Scripts\python.exe"
$streamlit  = "c:\claw-code\.venv\Scripts\streamlit.exe"

# --- 1. Monitor loop ---
$monitorCmd = "`"$pythonExe`" market_monitor.py --loop --interval-seconds 900"
schtasks /Create /F /TN "OptionsMonitor" /TR $monitorCmd /SC ONLOGON /RL HIGHEST /IT
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK - OptionsMonitor task registered."
} else {
    Write-Host "ERROR - OptionsMonitor failed to register (code $LASTEXITCODE)."
}

# --- 2. Dashboard ---
$dashCmd = "`"$streamlit`" run dashboard.py --server.port 8501 --server.headless true"
schtasks /Create /F /TN "OptionsDashboard" /TR $dashCmd /SC ONLOGON /RL HIGHEST /IT
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK - OptionsDashboard task registered."
} else {
    Write-Host "ERROR - OptionsDashboard failed to register (code $LASTEXITCODE)."
}

# Set working directory for both tasks via XML patch
foreach ($task in @("OptionsMonitor", "OptionsDashboard")) {
    $xml = schtasks /Query /TN $task /XML ONE 2>$null
    if ($xml) {
        $xml = $xml -replace "<WorkingDirectory>.*?</WorkingDirectory>", ""
        $xml = $xml -replace "(<Command>)", "<WorkingDirectory>$workingDir</WorkingDirectory>`$1"
        $tmpFile = "$env:TEMP\$task.xml"
        $xml | Set-Content -Encoding Unicode $tmpFile
        schtasks /Create /F /TN $task /XML $tmpFile | Out-Null
        Remove-Item $tmpFile -ErrorAction SilentlyContinue
        Write-Host "OK - Working directory set for $task."
    }
}

Write-Host ""
Write-Host "Done. Both tasks will start automatically at next logon."
Write-Host "To start them NOW run:"
Write-Host "  schtasks /Run /TN OptionsMonitor"
Write-Host "  schtasks /Run /TN OptionsDashboard"
