# install-doctor-digest.ps1 -- register Narada_Doctor_Digest: once a day
# at 08:30 Brisbane, run every doctor check, write doctor.json, and send a
# digest through notify_suti ONLY when something is red (at most one a
# day; docs/doctor.md "Daily digest").
#
# 08:30 is after smriti's 07:00 morning message (the heartbeat Suti
# notices) and before the 11:00 akhada tick. Same shape as
# install-cron-tasks.ps1: wscript run-hidden.vbs -> pythonw cron_run.py
# (durable per-job log) -> scripts/doctor.py. No RestartOnFailure: a
# failed delivery exits 3 and the task result shows it; tomorrow retries.
#
# Idempotent: -Force re-registers, -Uninstall removes, -DryRun prints.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$DryRun,
    [string]$At = '08:30'
)

$ErrorActionPreference = 'Stop'
$name = 'Narada_Doctor_Digest'

if ($Uninstall) {
    $ex = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $ex) { Write-Host "${name}: not registered"; exit 0 }
    if ($DryRun) { Write-Host "DRY RUN -- would unregister $name"; exit 0 }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "unregistered $name"
    exit 0
}

$shim = Join-Path $PSScriptRoot 'run-hidden.vbs'
$cronRun = Join-Path (Split-Path -Parent $PSScriptRoot) 'cron_run.py'
$pranaRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$doctor = Join-Path $pranaRoot 'scripts\doctor.py'
$logDir = Join-Path $env:LOCALAPPDATA 'narada\logs\cron'
$logPath = Join-Path $logDir "$name.log"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error 'pythonw.exe not found on PATH.'; exit 1 }
foreach ($f in @($shim, $cronRun, $doctor)) {
    if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}

$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "${name}: already registered (state $($existing.State)). -Force to re-register."
    exit 0
}

$arg = "`"$shim`" `"$pythonw`" `"$cronRun`" `"$logPath`" `"$doctor`" --write --send-digest"
if ($DryRun) {
    Write-Host "DRY RUN -- $name daily at $At"
    Write-Host "           wscript.exe $arg"
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($existing) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
Register-ScheduledTask -TaskName $name `
    -Description "Narada doctor: red-only daily digest via notify_suti ($At)" `
    -Action $action -Trigger $trigger -Settings $settings | Out-Null
Write-Host "registered $name (daily $At) -> $logPath"
