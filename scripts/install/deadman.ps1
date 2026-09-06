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
    # Telegram page. Returns $true only on confirmed delivery — callers
    # that latch "already notified" state MUST gate on this (a failed
    # page marked delivered would silence every retry). Never throws.
    if ($DryRun) { Write-DeadmanLog "DRYRUN would-page: $Text"; return $true }
    try {
        $token = $null; $chat = $null
        foreach ($line in (Get-Content $EnvPath -ErrorAction Stop)) {
            if ($line -match '^NARADA_TELEGRAM_BOT_TOKEN=(.+)$') { $token = $Matches[1].Trim() }
            if ($line -match '^TELEGRAM_ALLOWED_USERS=([^,]+)') { $chat = $Matches[1].Trim() }
        }
        if (-not $token -or -not $chat) { Write-DeadmanLog "page skipped: telegram config incomplete"; return $false }
        $body = @{ chat_id = $chat; text = $Text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        Write-DeadmanLog "paged: $Text"
        return $true
    } catch {
        Write-DeadmanLog "page FAILED: $($_.Exception.Message)"
        return $false
    }
}

function Read-DeadmanState {
    # Returns a state hashtable, or $null when a state file EXISTS but
    # cannot be read — corrupt state must refuse destructive action, not
    # silently grant a fresh restart budget (diff-review finding 1).
    if (-not (Test-Path $StatePath)) {
        return @{ restarts = @(); paged_exhausted = $false }
    }
    try {
        $raw = Get-Content $StatePath -Raw -ErrorAction Stop | ConvertFrom-Json
        $restarts = @()
        if ($null -ne $raw.restarts) { $restarts = @($raw.restarts | ForEach-Object { [double]$_ }) }
        $paged = $false
        if ($null -ne $raw.paged_exhausted) { $paged = [bool]$raw.paged_exhausted }
        return @{ restarts = $restarts; paged_exhausted = $paged }
    } catch {
        Write-DeadmanLog "state UNREADABLE: $($_.Exception.Message)"
        return $null
    }
}

function Save-DeadmanState($State) {
    # Returns $true on durable save. Written atomically (temp + move) so
    # an interrupted write can never leave a corrupt ledger behind.
    if ($DryRun) { return $true }
    try {
        $dir = Split-Path -Parent $StatePath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        $tmp = "$StatePath.tmp"
        @{ restarts = @($State.restarts); paged_exhausted = $State.paged_exhausted } |
            ConvertTo-Json -Compress | Set-Content -Path $tmp -Encoding utf8
        Move-Item -Path $tmp -Destination $StatePath -Force
        return $true
    } catch {
        Write-DeadmanLog "state write FAILED: $($_.Exception.Message)"
        return $false
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
# A corrupt state file ($null) is repaired here — healthy is healthy.
if ($null -ne $age -and $age -le $StaleSeconds -and $age -ge -60) {
    if ($null -eq $state -or $state.restarts.Count -gt 0 -or $state.paged_exhausted) {
        Write-DeadmanLog ("healthy again (age {0:n0}s) - clearing restart ledger" -f $age)
        if ($null -ne $state -and $state.paged_exhausted) {
            # Best-effort all-clear; a lost recovery notice self-corrects
            # (nothing further pages while healthy), so it never blocks
            # the ledger reset — health truth beats notification state.
            Send-Page "Narada deadman: host heartbeat recovered - standing down cleared." | Out-Null
        }
        Save-DeadmanState @{ restarts = @(); paged_exhausted = $false } | Out-Null
    } else {
        Write-DeadmanLog ("healthy (age {0:n0}s)" -f $age)
    }
    exit 0
}

# --- 3. Stale or missing: act within the restart budget --------------
if ($null -eq $age) { $ageText = "beacon missing" } else { $ageText = ("beacon stale {0:n0}s" -f $age) }

# Corrupt ledger: refuse destructive action rather than granting a fresh
# budget (diff-review finding 1). Page every run until repaired/healthy.
if ($null -eq $state) {
    Write-DeadmanLog "REFUSING restart: state file unreadable ($ageText) - fix or delete $StatePath"
    Send-Page "Narada deadman: $ageText but the restart ledger is corrupt - refusing to act. Fix or delete $StatePath." | Out-Null
    exit 0
}

# Exhaustion is a LATCH, not a rolling count (diff-review finding 2):
# once set, only a verified-healthy beacon (branch 2) clears it. Without
# this, aged-out timestamps would resume endless restart/page cycles.
if ($state.paged_exhausted) {
    Write-DeadmanLog "standing down (exhaustion latched; $ageText)"
    exit 0
}

$windowStart = $nowEpoch - ($RestartWindowMinutes * 60)
$recent = @($state.restarts | Where-Object { $_ -ge $windowStart })

if ($recent.Count -ge $MaxRestarts) {
    Write-DeadmanLog "EXHAUSTED: $($recent.Count) restarts in ${RestartWindowMinutes}m ($ageText) - standing down, paging"
    $delivered = Send-Page "Narada deadman: restart budget EXHAUSTED ($($recent.Count) in ${RestartWindowMinutes}m, $ageText). Host is not coming back on its own - manual attention needed."
    # Latch only on confirmed delivery (diff-review finding 3): a failed
    # page must retry next run, not go silent while the host is down.
    Save-DeadmanState @{ restarts = $state.restarts; paged_exhausted = [bool]$delivered } | Out-Null
    exit 0
}

if ($DryRun) {
    Write-DeadmanLog "DRYRUN would-restart '$TaskName' ($ageText; $($recent.Count)/$MaxRestarts used)"
    exit 0
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-DeadmanLog "FATAL: task '$TaskName' not found - nothing to restart ($ageText)"
    Send-Page "Narada deadman: $ageText but scheduled task '$TaskName' does not exist. Cannot act." | Out-Null
    exit 0
}

# Persist the attempt BEFORE acting (diff-review finding 1): if the
# ledger cannot be written, refuse the restart — otherwise a full disk
# (which also kills heartbeat writes) would restart the tree every run
# with no budget at all.
$saved = Save-DeadmanState @{ restarts = @($recent + $nowEpoch); paged_exhausted = $false }
if (-not $saved) {
    Write-DeadmanLog "REFUSING restart: cannot persist the restart ledger ($ageText)"
    Send-Page "Narada deadman: $ageText but cannot write the restart ledger - refusing to act (unbudgeted restarts are worse than none)." | Out-Null
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
    Send-Page "Narada deadman: restarted $TaskName ($ageText; restart $($recent.Count + 1)/$MaxRestarts this hour)." | Out-Null
} catch {
    Write-DeadmanLog "restart FAILED: $($_.Exception.Message)"
    Send-Page "Narada deadman: FAILED to restart ${TaskName}: $($_.Exception.Message)" | Out-Null
}
exit 0
