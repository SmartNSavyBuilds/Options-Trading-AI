# Setup Windows Task Scheduler entries for the Options Trading AI system.
# Registers two tasks:
#   OptionsMonitor   - market_monitor.py --loop (scoring + execution)
#   OptionsDashboard - streamlit dashboard on port 8501
#
# Run this script ONCE as Administrator to register/update both tasks.
# After that, both start automatically at every logon.

$pythonExe  = "c:\claw-code\.venv\Scripts\python.exe"
$streamlit  = "c:\claw-code\.venv\Scripts\streamlit.exe"
$workingDir = "c:\claw-code\projects\options_trading_ai"

$taskSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# --- 1. Monitor loop ---
$monitorAction = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "market_monitor.py --loop --interval-seconds 900" `
    -WorkingDirectory $workingDir

$monitorTrigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask `
    -TaskName  "OptionsMonitor" `
    -Action    $monitorAction `
    -Trigger   $monitorTrigger `
    -Settings  $taskSettings `
    -RunLevel  Highest `
    -Force | Out-Null

Write-Host "OK - Task 'OptionsMonitor' registered (starts at logon)."

# --- 2. Dashboard ---
$dashboardAction = New-ScheduledTaskAction `
    -Execute $streamlit `
    -Argument "run dashboard.py --server.port 8501 --server.headless true" `
    -WorkingDirectory $workingDir

$dashboardTrigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask `
    -TaskName  "OptionsDashboard" `
    -Action    $dashboardAction `
    -Trigger   $dashboardTrigger `
    -Settings  $taskSettings `
    -RunLevel  Highest `
    -Force | Out-Null

Write-Host "OK - Task 'OptionsDashboard' registered (starts at logon, http://localhost:8501)."
Write-Host ""
Write-Host "To start both NOW without rebooting:"
Write-Host "  Start-ScheduledTask -TaskName OptionsMonitor"
Write-Host "  Start-ScheduledTask -TaskName OptionsDashboard"
Write-Host ""
Write-Host "To check status:"
Write-Host "  Get-ScheduledTask -TaskName OptionsMonitor,OptionsDashboard | Select TaskName, State"
