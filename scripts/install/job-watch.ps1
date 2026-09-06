# job-watch.ps1 -- external scheduled-job freshness watcher (platform
# spec, one-process addendum, singleton 4 / migration step 4a).
#
# The addendum requires overdue-job detection checked OUTSIDE the
# scheduler: a scheduler that is alive but not firing (or firing and
# failing) is the zombie-heartbeat shape, and the thing that must
# notice it cannot be the scheduler itself. This is that observer.
#
# It runs as the SECOND action on the Narada_Deadman task (one external
# entry, two dumb scripts) after the host-liveness check. It is
# read-only + page-only: it NEVER restarts or mutates a job -- a failing
# or overdue job is a human's problem, so it pages once per failure and
# stands down until the job recovers. Watches Windows scheduled tasks
# today; the same shape reads Hermes run records after the cutover.
#
# HARD CONSTRAINT (same as deadman.ps1): depends on nothing in the
# Narada stack. Plain PowerShell 5.1 + Task Scheduler query + one
# Telegram call. Config lives in a plain JSON file, not code.

[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:LOCALAPPDATA\narada\job-watch.json",
    [string]$StatePath = "$env:LOCALAPPDATA\narada\job-watch-state.json",
    [string]$LogPath = "$env:LOCALAPPDATA\narada\logs\deadman.log",
    [string]$EnvPath = "$HOME\.hermes\.env",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-JobLog([string]$Message) {
    $line = "$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')) job-watch: $Message"
    Write-Host $line
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch {}
}

function Send-Page([string]$Text) {
    # Returns $true only on confirmed delivery (a failed page must not
    # latch, or the notification is lost while the job stays broken).
    if ($DryRun) { Write-JobLog "DRYRUN would-page: $Text"; return $true }
    try {
        $token = $null; $chat = $null
        foreach ($line in (Get-Content $EnvPath -ErrorAction Stop)) {
            if ($line -match '^NARADA_TELEGRAM_BOT_TOKEN=(.+)$') { $token = $Matches[1].Trim() }
            if ($line -match '^TELEGRAM_ALLOWED_USERS=([^,]+)') { $chat = $Matches[1].Trim() }
        }
        if (-not $token -or -not $chat) { Write-JobLog "page skipped: telegram config incomplete"; return $false }
        $body = @{ chat_id = $chat; text = $Text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        Write-JobLog "paged: $Text"
        return $true
    } catch {
        Write-JobLog "page FAILED: $($_.Exception.Message)"
        return $false
    }
}

function Read-WatchState {
    # Missing file = clean slate (unlike the deadman's restart ledger,
    # an unreadable watch-state is non-destructive: worst case a page
    # repeats, which is the safe direction for a notifier). Repairs in
    # place on corruption.
    if (-not (Test-Path $StatePath)) { return @{} }
    try {
        $raw = Get-Content $StatePath -Raw -ErrorAction Stop | ConvertFrom-Json
        $h = @{}
        if ($null -ne $raw) {
            foreach ($p in $raw.PSObject.Properties) { $h[$p.Name] = [string]$p.Value }
        }
        return $h
    } catch {
        Write-JobLog "state unreadable, resetting: $($_.Exception.Message)"
        return @{}
    }
}

function Save-WatchState($State) {
    if ($DryRun) { return }
    try {
        $dir = Split-Path -Parent $StatePath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        $tmp = "$StatePath.tmp"
        ($State | ConvertTo-Json -Compress) | Set-Content -Path $tmp -Encoding utf8
        Move-Item -Path $tmp -Destination $StatePath -Force
    } catch {
        Write-JobLog "state write FAILED: $($_.Exception.Message)"
    }
}

# --- config: the jobs to watch ---------------------------------------
if (-not (Test-Path $ConfigPath)) {
    Write-JobLog "no config at $ConfigPath - nothing to watch"
    exit 0
}
try {
    $config = Get-Content $ConfigPath -Raw -ErrorAction Stop | ConvertFrom-Json
} catch {
    Write-JobLog "config unreadable: $($_.Exception.Message)"
    exit 0
}

$state = Read-WatchState
$newState = @{}
$nowUtc = (Get-Date).ToUniversalTime()

foreach ($job in $config.jobs) {
    $name = [string]$job.task
    if (-not $name) { continue }
    $maxAgeHours = [double]$job.max_age_hours
    # check mode: "result+overdue" (default) or "overdue-only". Use
    # overdue-only for jobs whose exit code is an ambiguous health
    # signal -- e.g. smriti-morning exits 1 on "already sent today"
    # (a success invariant, not a failure), so only "did it fire within
    # the window" is trustworthy for it. See the migration log finding.
    $check = if ($job.check) { [string]$job.check } else { "result+overdue" }
    $watchResult = ($check -ne "overdue-only")

    # Query the Windows scheduled task. Absence is itself a finding: a
    # watched job that has vanished from the scheduler is not "healthy."
    $info = $null
    try { $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction Stop } catch {}

    $problem = $null      # human-readable problem, or $null when healthy
    $signature = $null    # dedup key: same signature = same failure, don't re-page

    if ($null -eq $info) {
        $problem = "task '$name' not found in the scheduler"
        $signature = "missing"
    } else {
        $lastRun = $info.LastRunTime
        $result = $info.LastTaskResult
        # A never-run task reports a sentinel date far in the past; the
        # age check below catches it as overdue.
        $ageHours = if ($lastRun) { ($nowUtc - $lastRun.ToUniversalTime()).TotalHours } else { [double]::MaxValue }

        if ($watchResult -and $result -ne 0 -and $result -ne 267009) {
            # 267009 = 0x41301 TASK_STATE_RUNNING as a "last result" while
            # a run is in flight; not a failure.
            $problem = "task '$name' last run FAILED (result $result)"
            $signature = "result=$result@$($lastRun.Ticks)"
        } elseif ($ageHours -gt $maxAgeHours) {
            $ageText = if ($ageHours -eq [double]::MaxValue) { "never" } else { ("{0:n1}h ago" -f $ageHours) }
            $problem = "task '$name' is OVERDUE (last ran $ageText, expected within ${maxAgeHours}h)"
            $signature = "overdue@$($lastRun.Ticks)"
        }
    }

    if ($null -eq $problem) {
        # Healthy: if it was previously latched as broken, announce recovery.
        if ($state.ContainsKey($name)) {
            Write-JobLog "$name recovered"
            Send-Page "Narada job-watch: $name recovered." | Out-Null
        } else {
            Write-JobLog "$name ok"
        }
        continue  # not carried into $newState = latch cleared
    }

    # Broken. Page once per distinct failure signature; carry the latch.
    $already = $state[$name]
    if ($already -eq $signature) {
        Write-JobLog "$problem (already paged, standing down)"
        $newState[$name] = $signature
    } else {
        Write-JobLog $problem
        $delivered = Send-Page "Narada job-watch: $problem"
        # Latch only on delivery; a failed page retries next tick.
        $newState[$name] = if ($delivered) { $signature } else { "" }
    }
}

Save-WatchState $newState
exit 0
