# Publish the brain server on the tailnet (idempotent).
# https://intergalactic.tail807360.ts.net:8443 -> 127.0.0.1:8811
# Tailnet-only (serve, never funnel). See deploy/README.md.

$ts = "C:\Software\Tailscale\tailscale.exe"
if (-not (Test-Path $ts)) {
    Write-Error "tailscale.exe not found at $ts"
    exit 1
}

& $ts serve --bg --https=8443 http://127.0.0.1:8811
if ($LASTEXITCODE -ne 0) {
    Write-Error "tailscale serve failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

& $ts serve status
