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
$TaskDescription = 'External watchdog for Narada_Host - restarts a hung or dead host, pages on exhaustion'
$scriptPath = Join-Path $PSScriptRoot 'deadman.ps1'
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

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not $Force) {
    Write-Host "task '$TaskName' already registered (state: $($existing.State)). Use -Force to re-register."
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
# Start a minute from now, repeat forever on the interval.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $intervalMinutes)
# The deadman must never pile up or hang around: one instance, short cap.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

if ($DryRun) {
    Write-Host "DRY RUN -- would register '$TaskName': every ${intervalMinutes}m -> powershell -File $scriptPath (enabled: $([bool]$Enable))"
    exit 0
}

if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed existing '$TaskName' (-Force)"
}

Register-ScheduledTask -TaskName $TaskName -Description $TaskDescription `
    -Action $action -Trigger $trigger -Settings $settings | Out-Null
Write-Host "registered '$TaskName' (every ${intervalMinutes}m, stale threshold lives in deadman.ps1)"

if ($Enable) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "ARMED - the deadman is live. The host must be writing heartbeats or it WILL be restarted."
} else {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "registered DISABLED - arm with: Enable-ScheduledTask $TaskName (after the host runs a heartbeat build)"
}
