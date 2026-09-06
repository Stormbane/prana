# Test harness: runs deadman.ps1 WITHOUT -DryRun with the mutating
# cmdlets shadowed by mocks (function lookup beats cmdlet lookup in
# child scopes), so the restart / exhaustion / refusal paths execute
# for real against fabricated files and print MOCK-* markers.

param(
    [string]$ScriptPath,
    [string]$HeartbeatPath,
    [string]$StatePath,
    [string]$LogPath,
    [string]$EnvPath,
    [string]$TaskState = 'Running',
    [switch]$PageFail
)

# $global: deliberately — the mocks execute from the child script's
# scope, where $script: would resolve against deadman.ps1, not here.
$global:mockPageFail = [bool]$PageFail
$global:mockTaskState = $TaskState

function global:Get-ScheduledTask {
    param([string]$TaskName, $ErrorAction)
    [pscustomobject]@{ TaskName = $TaskName; State = $global:mockTaskState }
}
function global:Stop-ScheduledTask { param([string]$TaskName, $ErrorAction) Write-Host "MOCK-STOP $TaskName" }
function global:Start-ScheduledTask { param([string]$TaskName, $ErrorAction) Write-Host "MOCK-START $TaskName" }
function global:Start-Sleep { param([int]$Seconds) }
function global:Invoke-RestMethod {
    param($Method, $Uri, $ContentType, $Body, $TimeoutSec)
    if ($global:mockPageFail) { throw 'mock: network down' }
    Write-Host "MOCK-PAGE $Body"
}

& $ScriptPath -HeartbeatPath $HeartbeatPath -StatePath $StatePath `
    -LogPath $LogPath -EnvPath $EnvPath -TaskName 'MockHost'
exit $LASTEXITCODE
