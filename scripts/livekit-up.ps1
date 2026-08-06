# Bring up the self-hosted LiveKit server (Docker Desktop required).
# Generates ~/.narada/.livekit.env (api key + secret) on first run; the
# voice worker and any client tooling read the same file.
$ErrorActionPreference = "Stop"

$envFile = Join-Path $env:USERPROFILE ".narada\.livekit.env"
if (-not (Test-Path $envFile)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $secret = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    @(
        "LIVEKIT_API_KEY=narada-key"
        "LIVEKIT_API_SECRET=$secret"
        "LIVEKIT_URL=ws://127.0.0.1:7880"
    ) | Set-Content -Path $envFile -Encoding ascii
    Write-Host "generated $envFile"
}

$compose = Join-Path $PSScriptRoot "..\config\livekit\docker-compose.yml"
docker compose --env-file $envFile -f $compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
docker compose --env-file $envFile -f $compose ps
