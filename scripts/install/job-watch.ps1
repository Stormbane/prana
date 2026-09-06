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
# TASK_STATE codes that appear as a "last result" while a run is still
# in flight -- neither success nor failure (a completed run is what
# confirms recovery or failure). SCHED_S_TASK_RUNNING (0x41301) and
# SCHED_S_TASK_QUEUED (0x41325).
$PENDING_CODES = @(267009, 267045)

# Reserved latch key for watcher-level problems (config missing/broken)
# so a silent loss of ALL coverage still pages -- an ordinary bad edit
# must not disable monitoring quietly (the never-silent rule applied to
# the watcher itself).
$WATCHER_KEY = "__watcher__"

$state = Read-WatchState
$nowUtc = (Get-Date).ToUniversalTime()

# --- config load: failure is a watcher-level problem, NOT a silent no-op
$config = $null
$watcherProblem = $null
if (-not (Test-Path $ConfigPath)) {
    $watcherProblem = "job-watch config missing ($ConfigPath) - overdue-job coverage is DISABLED"
} else {
    try {
        $config = Get-Content $ConfigPath -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        $watcherProblem = "job-watch config unreadable ($ConfigPath): $($_.Exception.Message) - coverage DISABLED"
    }
    if ($null -ne $config -and $null -eq $config.jobs) {
        $watcherProblem = "job-watch config has no 'jobs' list ($ConfigPath) - coverage DISABLED"
    }
}

if ($null -ne $watcherProblem) {
    # Preserve existing job latches (do NOT overwrite state with empty);
    # page the watcher problem once, gated on delivery.
    $prev = $state[$WATCHER_KEY]
    if ($prev -eq $watcherProblem) {
        Write-JobLog "$watcherProblem (already paged, standing down)"
    } else {
        Write-JobLog $watcherProblem
        $delivered = Send-Page "Narada job-watch: $watcherProblem"
        $state[$WATCHER_KEY] = if ($delivered) { $watcherProblem } else { "" }
    }
    Save-WatchState $state
    exit 0
}

# Config is good. Start the fresh latch map; carry a pending watcher-
# recovery forward until its all-clear is delivered.
$newState = @{}
if ($state.ContainsKey($WATCHER_KEY)) {
    $delivered = Send-Page "Narada job-watch: config readable again - coverage restored."
    if ($delivered) { Write-JobLog "watcher config recovered" }
    else { Write-JobLog "watcher config recovered (all-clear page failed, will retry)"; $newState[$WATCHER_KEY] = $state[$WATCHER_KEY] }
}

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
    $pending = $false     # in-flight (running/queued): neither success nor failure

    if ($null -eq $info) {
        $problem = "task '$name' not found in the scheduler"
        $signature = "missing"
    } else {
        $lastRun = $info.LastRunTime
        $result = $info.LastTaskResult
        $pending = ($PENDING_CODES -contains $result)
        # A never-run task reports a sentinel date far in the past; the
        # age check below catches it as overdue.
        $ageHours = if ($lastRun) { ($nowUtc - $lastRun.ToUniversalTime()).TotalHours } else { [double]::MaxValue }

        if ($watchResult -and $result -ne 0 -and -not $pending) {
            $problem = "task '$name' last run FAILED (result $result)"
            $signature = "result=$result@$($lastRun.Ticks)"
        } elseif ($ageHours -gt $maxAgeHours) {
            # A job stuck pending forever still ages out and is caught here.
            $ageText = if ($ageHours -eq [double]::MaxValue) { "never" } else { ("{0:n1}h ago" -f $ageHours) }
            $problem = "task '$name' is OVERDUE (last ran $ageText, expected within ${maxAgeHours}h)"
            $signature = "overdue@$($lastRun.Ticks)"
        }
    }

    $prev = $state[$name]

    if ($null -eq $problem) {
        if ($pending -and $null -ne $prev) {
            # In-flight and previously latched broken: a run is underway
            # but has NOT completed successfully yet. Do not announce
            # recovery or clear the latch prematurely -- carry it until a
            # completed successful run (result 0) confirms recovery.
            Write-JobLog "$name in-flight (result pending) - holding prior latch"
            $newState[$name] = $prev
            continue
        }
        if ($null -ne $prev) {
            # Genuine recovery. Clear the latch only on delivered all-clear;
            # otherwise carry it so the next healthy tick retries (a lost
            # recovery notice must not be permanent).
            $delivered = Send-Page "Narada job-watch: $name recovered."
            if ($delivered) {
                Write-JobLog "$name recovered"
            } else {
                Write-JobLog "$name recovered (all-clear page failed, will retry)"
                $newState[$name] = $prev
            }
        } else {
            Write-JobLog "$name ok"
        }
        continue
    }

    # Broken. Page once per distinct problem signature; carry the latch.
    if ($prev -eq $signature) {
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
