# Setup Windows Task Scheduler entry for market_monitor.py --loop
# Run this script once as Administrator to register the task

$taskName = "OptionsMonitor"
$pythonExe = "c:\claw-code\.venv\Scripts\python.exe"
$scriptPath = "c:\claw-code\projects\options_trading_ai\market_monitor.py"
$workingDir = "c:\claw-code\projects\options_trading_ai"

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "$scriptPath --loop --interval-seconds 900" `
    -WorkingDirectory $workingDir

# Trigger: run at logon
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Run with highest privileges, continue if on battery
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Write-Host "Task '$taskName' registered. It will start automatically at next logon."
Write-Host "To start it now without rebooting, run:"
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
