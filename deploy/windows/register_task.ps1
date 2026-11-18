# Registers the daily DataRova run in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -At 09:30
#
# The task runs as the current user, and only while that user is logged on.
# If the PC is left logged off, open the task in Task Scheduler afterwards and
# tick "Run whether user is logged on or not". Times are this PC's local time.

param(
    [string]$At = "10:00",
    [string]$TaskName = "DataRova daily snapshot"
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$uv = (Get-Command uv).Source

$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run --no-sync python run.py --quiet" `
    -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "DataRova projects + ASINs -> MySQL datarova_all_data" -Force | Out-Null

Write-Host "Registered '$TaskName': daily at $At, in $project"
Write-Host "Run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Logs:             $project\logs\datarova.log"
