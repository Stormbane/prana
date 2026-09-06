# deadman.ps1 -- the watchdog's watchdog (platform spec, one-process
# addendum, singleton 2 / cross-review round-1 finding 1).
#
# Runs every few minutes from the Narada_Deadman scheduled task. Checks
# the host's heartbeat beacon; a stale or missing beacon means the host
# is hung, dead, or running a pre-heartbeat build -- all of which get
# the same treatment: restart the Narada_Host task. Pages Suti's
# Telegram on every action and stands down (paged once) when the
# restart budget is exhausted, until the heartbeat recovers.
#
# HARD CONSTRAINT: this script may depend on nothing in the Narada
# stack -- no Python, no prana imports, no smriti, no Hermes runtime.
# Plain PowerShell 5.1 + Task Scheduler + one HTTPS call to Telegram
# (token read from the bridge env file by line parse, not dotenv).
# It is one of the two permitted dumb OS-level entries (bootstrap +
# deadman); everything smarter lives under the supervisor it watches.

[CmdletBinding()]
param(
    [string]$HeartbeatPath = "$env:LOCALAPPDATA\narada\host.heartbeat",
    [string]$StatePath = "$env:LOCALAPPDATA\narada\deadman-state.json",
    [string]$LogPath = "$env:LOCALAPPDATA\narada\logs\deadman.log",
    [string]$TaskName = "Narada_Host",
    [string]$EnvPath = "$HOME\.hermes\.env",
    [int]$StaleSeconds = 300,
    [int]$MaxRestarts = 3,
    [int]$RestartWindowMinutes = 60,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-DeadmanLog([string]$Message) {
    $line = "$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message"
    Write-Host $line
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch {}
}

function Send-Page([string]$Text) {
    # Best-effort Telegram page. Never throws; a page failure is logged
    # but must not stop the deadman from acting (never silent, but also
    # never blocked on the messenger).
    if ($DryRun) { Write-DeadmanLog "DRYRUN would-page: $Text"; return }
    try {
        $token = $null; $chat = $null
        foreach ($line in (Get-Content $EnvPath -ErrorAction Stop)) {
            if ($line -match '^NARADA_TELEGRAM_BOT_TOKEN=(.+)$') { $token = $Matches[1].Trim() }
            if ($line -match '^TELEGRAM_ALLOWED_USERS=([^,]+)') { $chat = $Matches[1].Trim() }
        }
        if (-not $token -or -not $chat) { Write-DeadmanLog "page skipped: telegram config incomplete"; return }
        $body = @{ chat_id = $chat; text = $Text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        Write-DeadmanLog "paged: $Text"
    } catch {
        Write-DeadmanLog "page FAILED: $($_.Exception.Message)"
    }
}

function Read-DeadmanState {
    try {
        if (Test-Path $StatePath) {
            $raw = Get-Content $StatePath -Raw -ErrorAction Stop | ConvertFrom-Json
            $restarts = @()
            if ($null -ne $raw.restarts) { $restarts = @($raw.restarts | ForEach-Object { [double]$_ }) }
            $paged = $false
            if ($null -ne $raw.paged_exhausted) { $paged = [bool]$raw.paged_exhausted }
            return @{ restarts = $restarts; paged_exhausted = $paged }
        }
    } catch {
        Write-DeadmanLog "state unreadable, resetting: $($_.Exception.Message)"
    }
    return @{ restarts = @(); paged_exhausted = $false }
}

function Save-DeadmanState($State) {
    if ($DryRun) { return }
    try {
        $dir = Split-Path -Parent $StatePath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        @{ restarts = @($State.restarts); paged_exhausted = $State.paged_exhausted } |
            ConvertTo-Json -Compress | Set-Content -Path $StatePath -Encoding utf8
    } catch {
        Write-DeadmanLog "state write FAILED: $($_.Exception.Message)"
    }
}

# --- 1. Heartbeat age -------------------------------------------------
$nowEpoch = [double][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$age = $null   # $null = beacon missing/unreadable
try {
    if (Test-Path $HeartbeatPath) {
        $beat = Get-Content $HeartbeatPath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $beat.ts) { $age = $nowEpoch - [double]$beat.ts }
    }
} catch {
    Write-DeadmanLog "heartbeat unreadable: $($_.Exception.Message)"
}

$state = Read-DeadmanState

# --- 2. Healthy: fresh beacon resets the ledger ----------------------
if ($null -ne $age -and $age -le $StaleSeconds -and $age -ge -60) {
    if ($state.restarts.Count -gt 0 -or $state.paged_exhausted) {
        Write-DeadmanLog ("healthy again (age {0:n0}s) - clearing restart ledger" -f $age)
        if ($state.paged_exhausted) { Send-Page "Narada deadman: host heartbeat recovered - standing down cleared." }
        Save-DeadmanState @{ restarts = @(); paged_exhausted = $false }
    } else {
        Write-DeadmanLog ("healthy (age {0:n0}s)" -f $age)
    }
    exit 0
}

# --- 3. Stale or missing: act within the restart budget --------------
if ($null -eq $age) { $ageText = "beacon missing" } else { $ageText = ("beacon stale {0:n0}s" -f $age) }

$windowStart = $nowEpoch - ($RestartWindowMinutes * 60)
$recent = @($state.restarts | Where-Object { $_ -ge $windowStart })

if ($recent.Count -ge $MaxRestarts) {
    if (-not $state.paged_exhausted) {
        Write-DeadmanLog "EXHAUSTED: $($recent.Count) restarts in ${RestartWindowMinutes}m ($ageText) - standing down, paging"
        Send-Page "Narada deadman: restart budget EXHAUSTED ($($recent.Count) in ${RestartWindowMinutes}m, $ageText). Host is not coming back on its own - manual attention needed."
        Save-DeadmanState @{ restarts = $recent; paged_exhausted = $true }
    } else {
        Write-DeadmanLog "standing down (exhausted, already paged; $ageText)"
    }
    exit 0
}

if ($DryRun) {
    Write-DeadmanLog "DRYRUN would-restart '$TaskName' ($ageText; $($recent.Count)/$MaxRestarts used)"
    exit 0
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-DeadmanLog "FATAL: task '$TaskName' not found - nothing to restart ($ageText)"
    Send-Page "Narada deadman: $ageText but scheduled task '$TaskName' does not exist. Cannot act."
    exit 0
}

Write-DeadmanLog "RESTART: '$TaskName' (task state: $($task.State); $ageText; $($recent.Count + 1)/$MaxRestarts in window)"
try {
    if ($task.State -eq 'Running') {
        # Hung, not dead: stop first. The host holds a kill-on-close job
        # object, so stopping the task takes the component tree with it -
        # no orphans for the fresh host to collide with.
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Start-Sleep -Seconds 5
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Send-Page "Narada deadman: restarted $TaskName ($ageText; restart $($recent.Count + 1)/$MaxRestarts this hour)."
} catch {
    Write-DeadmanLog "restart FAILED: $($_.Exception.Message)"
    Send-Page "Narada deadman: FAILED to restart ${TaskName}: $($_.Exception.Message)"
}
Save-DeadmanState @{ restarts = @($recent + $nowEpoch); paged_exhausted = $false }
exit 0
