# install-news.ps1 -- register Narada_News: three times a day (07:00, 12:30,
# 18:00 Brisbane) the news desk writes ~/.narada/brain/news.json for Today's
# News card (redesign L2d; prana/news.py). One web-search model call on the
# subscription per run; a failed run keeps the last good edition.
#
# Same shape as install-doctor-digest.ps1: wscript run-hidden.vbs -> pythonw
# cron_run.py (durable per-job log) -> python -m prana.news.
# Idempotent: -Force re-registers, -Uninstall removes, -DryRun prints.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$DryRun,
    [string[]]$At = @('07:00', '12:30', '18:00')
)

$ErrorActionPreference = 'Stop'
$name = 'Narada_News'

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
$newsScript = Join-Path $pranaRoot 'src\prana\news.py'
$logDir = Join-Path $env:LOCALAPPDATA 'narada\logs\cron'
$logPath = Join-Path $logDir "$name.log"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error 'pythonw.exe not found on PATH.'; exit 1 }
foreach ($f in @($shim, $cronRun, $newsScript)) {
    if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}

$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "${name}: already registered (state $($existing.State)). -Force to re-register."
    exit 0
}

$arg = "`"$shim`" `"$pythonw`" `"$cronRun`" `"$logPath`" `"$newsScript`""
if ($DryRun) {
    Write-Host "DRY RUN -- $name daily at $($At -join ', ')"
    Write-Host "           wscript.exe $arg"
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
$triggers = $At | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($existing) { Unregister-ScheduledTask -TaskName $name -Confirm:$false }
Register-ScheduledTask -TaskName $name `
    -Description "Narada news desk: sectioned, ranked news for Today ($($At -join ', '))" `
    -Action $action -Trigger $triggers -Settings $settings | Out-Null
Write-Host "registered $name (daily $($At -join ', ')) -> $logPath"
