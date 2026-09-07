# install-cron-tasks.ps1 -- register the three Narada cron jobs as
# Windows scheduled tasks, replacing the Hermes cron gateway
# (retire-hermes-2026-09-07 plan, step 3). Windows Task Scheduler is the
# scheduler singleton; these tasks call the prana scripts directly.
#
# Each task:
#   - runs through run-hidden.vbs (no console flash) -> cron_run.py
#     (captures stdout/stderr to a durable per-job log) -> the tick
#   - has EXPLICIT triggers (one per fire time; Windows tasks support
#     multiple triggers) rather than a cron expression
#   - has NO RestartOnFailure (a crash is caught by job-watch + the next
#     scheduled trigger; auto-retry would risk double-delivery)
#
# Registered DISABLED by default. Cutover is operational (per-job:
# pause the Hermes twin, drain, enable this task) and NOT done here.
#
# Idempotent: re-run with -Force to re-register. -Uninstall removes all
# three. -Enable arms them (only after the per-job pause-first cutover).

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Enable,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$shim = Join-Path $PSScriptRoot 'run-hidden.vbs'
$cronRun = Join-Path (Split-Path -Parent $PSScriptRoot) 'cron_run.py'
$logDir = Join-Path $env:LOCALAPPDATA 'narada\logs\cron'

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error 'pythonw.exe not found on PATH.'; exit 1 }

# Job definitions. StartWhenAvailable: figures skip a missed slot (the
# next slot covers it); check-in/debrief replay the most-recent missed
# run (a debrief the operator should have seen is worth delivering late).
$jobs = @(
    @{ Name = 'Narada_Figures';         Script = 'scripts\populate_figures_tick.py';
       Times = @('21:17','23:17','01:17','03:17','05:17','07:17'); StartWhenAvailable = $false; Limit = 'PT10M' }
    @{ Name = 'Narada_Akhada_Checkin';  Script = 'scripts\akhada_checkin_tick.py';
       Times = @('11:00','18:00','22:00'); StartWhenAvailable = $true;  Limit = 'PT5M' }
    @{ Name = 'Narada_Daily_Debrief';   Script = 'scripts\daily_debrief.py';
       Times = @('18:30'); StartWhenAvailable = $true;  Limit = 'PT20M' }
)

$pranaRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # ...\prana

if ($Uninstall) {
    foreach ($j in $jobs) {
        $ex = Get-ScheduledTask -TaskName $j.Name -ErrorAction SilentlyContinue
        if ($null -eq $ex) { Write-Host "$($j.Name): not registered"; continue }
        if ($DryRun) { Write-Host "DRY RUN -- would unregister $($j.Name)"; continue }
        Unregister-ScheduledTask -TaskName $j.Name -Confirm:$false
        Write-Host "unregistered $($j.Name)"
    }
    exit 0
}

if (-not (Test-Path $shim)) { Write-Error "run-hidden.vbs not found: $shim"; exit 1 }
if (-not (Test-Path $cronRun)) { Write-Error "cron_run.py not found: $cronRun"; exit 1 }
if (-not $DryRun -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }

foreach ($j in $jobs) {
    $scriptPath = Join-Path $pranaRoot $j.Script
    if (-not (Test-Path $scriptPath)) { Write-Error "job script missing: $scriptPath"; exit 1 }
    $logPath = Join-Path $logDir ("{0}.log" -f $j.Name)

    # Action: wscript run-hidden.vbs  pythonw cron_run.py <log> <script>
    $arg = "`"$shim`" `"$pythonw`" `"$cronRun`" `"$logPath`" `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg

    $triggers = foreach ($t in $j.Times) { New-ScheduledTaskTrigger -Daily -At $t }

    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([System.Xml.XmlConvert]::ToTimeSpan($j.Limit)) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $settings.StartWhenAvailable = [bool]$j.StartWhenAvailable
    # Born disabled unless -Enable, ATOMICALLY (Codex review): registering
    # enabled-then-Disable leaves a window where a trigger could fire
    # pre-cutover, and an interrupted disable leaves it permanently on.
    $settings.Enabled = [bool]$Enable
    # No RestartOnFailure by design (see header).

    $existing = Get-ScheduledTask -TaskName $j.Name -ErrorAction SilentlyContinue
    if ($existing -and -not $Force) {
        Write-Host "$($j.Name): already registered (state $($existing.State)). -Force to re-register."
        continue
    }

    if ($DryRun) {
        Write-Host "DRY RUN -- $($j.Name): $($j.Times -join ',') SWA=$($j.StartWhenAvailable) limit=$($j.Limit) enabled=$([bool]$Enable)"
        Write-Host "           -> $($j.Script) (log: $logPath)"
        continue
    }

    if ($existing) { Unregister-ScheduledTask -TaskName $j.Name -Confirm:$false }
    # $settings.Enabled already encodes disabled-vs-armed — the task is
    # born in the right state, no post-register enable/disable window.
    Register-ScheduledTask -TaskName $j.Name `
        -Description "Narada cron ($($j.Times -join ',')) - was Hermes $($j.Name)" `
        -Action $action -Trigger $triggers -Settings $settings | Out-Null

    if ($Enable) {
        Write-Host "registered + ARMED $($j.Name) ($($j.Times -join ','))"
    } else {
        Write-Host "registered DISABLED $($j.Name) ($($j.Times -join ',')) - arm at cutover"
    }
}
