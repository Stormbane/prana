# install-deadman.ps1 -- register the Narada_Deadman scheduled task:
# the external watchdog that watches the Narada_Host supervisor
# (platform spec, one-process addendum, singleton 2).
#
# Registered DISABLED by default: the running host must first be on a
# build that writes the heartbeat beacon, or the deadman's first check
# would read "beacon missing" and bounce a healthy stack. Arming is the
# explicit step (-Enable, or later: Enable-ScheduledTask Narada_Deadman)
# taken together with the host restart that picks up the beacon code.
#
# Idempotent: re-running with -Force re-registers.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Enable,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$TaskName = 'Narada_Deadman'
$TaskDescription = 'External watchdog for Narada_Host - restarts a hung/dead host + pages on exhaustion; second action watches scheduled-job freshness'
$scriptPath = Join-Path $PSScriptRoot 'deadman.ps1'
$shimPath = Join-Path $PSScriptRoot 'run-hidden.vbs'
$jobWatchPath = Join-Path $PSScriptRoot 'job-watch.ps1'
$jobWatchTemplate = Join-Path $PSScriptRoot 'job-watch.template.json'
$jobWatchConfig = Join-Path $env:LOCALAPPDATA 'narada\job-watch.json'
$intervalMinutes = 5

if ($Uninstall) {
    if ($DryRun) { Write-Host "DRY RUN -- would unregister '$TaskName'"; exit 0 }
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) { Write-Host "task '$TaskName' not registered - nothing to do"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "unregistered '$TaskName'"
    exit 0
}

if (-not (Test-Path $scriptPath)) {
    Write-Error "deadman.ps1 not found next to this installer: $scriptPath"
    exit 1
}
if (-not (Test-Path $shimPath)) {
    Write-Error "run-hidden.vbs not found next to this installer: $shimPath"
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not $Force) {
    Write-Host "task '$TaskName' already registered (state: $($existing.State)). Use -Force to re-register."
    exit 0
}

# Two actions on ONE task (Task Scheduler runs them sequentially): the
# host-liveness watchdog, then the scheduled-job freshness watcher. One
# external entry, two composable dumb scripts.
#
# Both launch through the run-hidden.vbs shim: a powershell.exe action
# flashes a console window on every tick even with -WindowStyle Hidden
# (the console exists before PowerShell can hide it), and a 5-minute
# watchdog blinking at the operator all day reads as malware. wscript
# is GUI-subsystem — no console is ever created; the shim waits and
# propagates the exit code, so LastTaskResult and the sequential
# action order are unchanged.
$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$shimPath`" powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
$jobWatchAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$shimPath`" powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$jobWatchPath`""
# Start a minute from now, repeat forever on the interval.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $intervalMinutes)
# The deadman must never pile up or hang around: one instance, short cap.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

if ($DryRun) {
    Write-Host "DRY RUN -- would register '$TaskName': every ${intervalMinutes}m -> [1] $scriptPath [2] $jobWatchPath (enabled: $([bool]$Enable))"
    Write-Host "DRY RUN -- would seed job-watch config at $jobWatchConfig from template (if absent)"
    exit 0
}

# Seed the job-watch config from the template if the operator has none
# yet (never overwrite an edited config).
if (-not (Test-Path $jobWatchConfig)) {
    if (Test-Path $jobWatchTemplate) {
        $cfgDir = Split-Path -Parent $jobWatchConfig
        if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory -Force $cfgDir | Out-Null }
        Copy-Item -Path $jobWatchTemplate -Destination $jobWatchConfig
        Write-Host "seeded job-watch config: $jobWatchConfig"
    } else {
        Write-Warning "job-watch template missing ($jobWatchTemplate) - job-watch will no-op until a config exists"
    }
} else {
    Write-Host "job-watch config already present: $jobWatchConfig (left as-is)"
}

if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed existing '$TaskName' (-Force)"
}

Register-ScheduledTask -TaskName $TaskName -Description $TaskDescription `
    -Action @($action, $jobWatchAction) -Trigger $trigger -Settings $settings | Out-Null
Write-Host "registered '$TaskName' (every ${intervalMinutes}m; action 1 = host liveness, action 2 = job freshness)"

if ($Enable) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "ARMED - the deadman is live. The host must be writing heartbeats or it WILL be restarted."
} else {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "registered DISABLED - arm with: Enable-ScheduledTask $TaskName (after the host runs a heartbeat build)"
}
