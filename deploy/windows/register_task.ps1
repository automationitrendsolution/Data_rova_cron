# Registers the daily DataRova run in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -At 09:30
#
# The task runs as the current user, and only while that user is logged on.
# If the PC is left logged off, open the task in Task Scheduler afterwards and
# tick "Run whether user is logged on or not". Times are this PC's local time.

param(
    # After DataRova publishes the previous day's ranks (about 13:40 IST).
    [string]$At = "14:30",
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
# A daily run takes 1-2 h (ASINs, keywords, 7 days of ranks): allow 8 h.
# If it fails, Windows tries again (up to 3 times, 15 min apart); a retry on
# the same day continues the stopped run instead of starting over.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -WakeToRun

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "DataRova projects, ASINs, keywords, ranks, BSR -> MySQL powerbidb" -Force | Out-Null

Write-Host "Registered '$TaskName': daily at $At, in $project"
Write-Host "Run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Logs:             $project\logs\datarova.log"
