# Test harness for job-watch.ps1: shadows Get-ScheduledTaskInfo and
# Invoke-RestMethod (global-scope functions beat cmdlets in the called
# script's scope) so the page/latch/recovery paths run for real against
# a fabricated task table. Task states are passed as a JSON map of
# name -> {LastTaskResult, LastRunIso} ("MISSING" value = task absent).
# LastRunIso is an absolute UTC timestamp (stable across runs, like a
# real task's last-run time) so the tick-based failure signature is
# deterministic between successive deadman ticks.

param(
    [string]$ScriptPath,
    [string]$ConfigPath,
    [string]$StatePath,
    [string]$LogPath,
    [string]$EnvPath,
    [string]$TasksJson,
    [switch]$PageFail
)

$global:mockPageFail = [bool]$PageFail
$global:mockTasks = $TasksJson | ConvertFrom-Json

function global:Get-ScheduledTask {
    param([string]$TaskName, $ErrorAction)
    $t = $global:mockTasks.$TaskName
    if ($null -eq $t -or $t -eq 'MISSING') { throw "mock: no task $TaskName" }
    # A task entry may declare a State (e.g. 'Disabled'); default 'Ready'.
    $st = if ($null -ne $t.State) { [string]$t.State } else { 'Ready' }
    [pscustomobject]@{ TaskName = $TaskName; State = $st }
}
function global:Get-ScheduledTaskInfo {
    param([string]$TaskName, $ErrorAction)
    $t = $global:mockTasks.$TaskName
    if ($null -eq $t -or $t -eq 'MISSING') { throw "mock: no task $TaskName" }
    $lastRun = [datetime]::Parse($t.LastRunIso, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
    $next = $null
    if ($null -ne $t.NextRunIso) {
        $next = [datetime]::Parse($t.NextRunIso, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
    }
    [pscustomobject]@{ LastTaskResult = [int]$t.LastTaskResult; LastRunTime = $lastRun; NextRunTime = $next }
}
function global:Invoke-RestMethod {
    param($Method, $Uri, $ContentType, $Body, $TimeoutSec)
    if ($global:mockPageFail) { throw 'mock: network down' }
    Write-Host "MOCK-PAGE $Body"
}

& $ScriptPath -ConfigPath $ConfigPath -StatePath $StatePath -LogPath $LogPath -EnvPath $EnvPath
exit $LASTEXITCODE
