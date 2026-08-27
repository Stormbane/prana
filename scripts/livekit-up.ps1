# EMERGENCY native start for the LiveKit server — the rollback path for
# the supervised `livekit` component (see components.yaml and plan
# resilience-and-reach-2026-08-27 §A1). Normal operation: the host
# orchestrator runs livekit-server; this script exists for when the host
# itself is the problem.
#
# Refuses to run if a livekit-server process exists or port 7880 is
# bound — a double-start on the same ports is exactly the failure mode
# rollback must not create.
#
# (Historical: this script used to bring up the Docker Compose
# deployment. Docker Desktop proved unsupervisable — it crash-looped on
# a broken Ubuntu WSL distro for days, 2026-08-27 outage — and was
# replaced by the native binary. config/livekit/docker-compose.yml is
# kept as provenance only.)
$ErrorActionPreference = "Stop"

$bin = Join-Path $env:USERPROFILE ".narada\host\bin\livekit-server.exe"
$cfg = Join-Path $env:USERPROFILE ".narada\host\livekit.yaml"

if (-not (Test-Path $bin)) { throw "livekit-server binary not found at $bin" }
if (-not (Test-Path $cfg)) { throw "livekit config not found at $cfg" }

$existing = Get-Process livekit-server -ErrorAction SilentlyContinue
if ($existing) {
    throw "REFUSING: livekit-server already running (pid $($existing.Id -join ', ')). Stop it (or the supervised component) first."
}
$bound = Get-NetTCPConnection -LocalPort 7880 -State Listen -ErrorAction SilentlyContinue
if ($bound) {
    throw "REFUSING: port 7880 is already bound (pid $($bound.OwningProcess | Select-Object -Unique)). Another server is alive."
}

Start-Process -FilePath $bin -ArgumentList "--config", $cfg -WindowStyle Hidden
Start-Sleep -Seconds 3

try {
    $r = Invoke-WebRequest -Uri http://127.0.0.1:7880/ -TimeoutSec 5 -UseBasicParsing
    Write-Host "livekit-server up (emergency/manual): http://127.0.0.1:7880 -> $($r.StatusCode)"
    Write-Host "NOTE: this instance is UNSUPERVISED. Restore the host-supervised component when possible."
} catch {
    throw "livekit-server started but 7880 not answering: $($_.Exception.Message)"
}
