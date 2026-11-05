# install-credits.ps1 -- register Narada_Credits: once a night (03:30, after
# smriti's 03:00 nightly: part of the sleep cycle) prana/credits.py asks each
# API provider for its credit and writes ~/.narada/brain/credits.json for the
# Data tab (Suti 2026-11-05). Read-only requests; no model calls.
#
# Same shape as install-doctor-digest.ps1: wscript run-hidden.vbs -> pythonw
# cron_run.py (durable per-job log) -> src/prana/credits.py.
# Idempotent: -Force re-registers, -Uninstall removes, -DryRun prints.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$DryRun,
    [string[]]$At = @('03:30')
)

$ErrorActionPreference = 'Stop'
$name = 'Narada_Credits'

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
$script = Join-Path $pranaRoot 'src\prana\credits.py'
$logDir = Join-Path $env:LOCALAPPDATA 'narada\logs\cron'
$logPath = Join-Path $logDir "$name.log"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error 'pythonw.exe not found on PATH.'; exit 1 }
foreach ($f in @($shim, $cronRun, $script)) {
    if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}

$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "${name}: already registered (state $($existing.State)). -Force to re-register."
    exit 0
}

$arg = "`"$shim`" `"$pythonw`" `"$cronRun`" `"$logPath`" `"$script`""
if ($DryRun) {
    Write-Host "DRY RUN -- $name daily at $($At -join ', ')"
    Write-Host "           wscript.exe $arg"
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
$triggers = $At | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($existing) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
Register-ScheduledTask -TaskName $name `
    -Description "Narada credits: each API provider's credit for the Data tab ($($At -join ', '))" `
    -Action $action -Trigger $triggers -Settings $settings | Out-Null
Write-Host "registered $name (daily $($At -join ', ')) -> $logPath"
