# Publish the built narada-phone-app PWA to the brain's static mount.
# Build side (narada-phone-app repo): npm run deploy:demo
#   -> emits C:\Projects\narada-phone-app\out\narada
# This copies it to %LOCALAPPDATA%\narada\pwa, which the brain serves
# at /app on its own origin (same-origin with the API, no CORS needed):
#   https://intergalactic.tail807360.ts.net:8443/app/
# Then bumps the brain component so the mount appears if it was absent.

$src = "C:\Projects\narada-phone-app\out\narada"
$dst = Join-Path $env:LOCALAPPDATA "narada\pwa"

if (-not (Test-Path "$src\index.html")) {
    Write-Error "no built PWA at $src - run 'npm run deploy:demo' in narada-phone-app first"
    exit 1
}

robocopy $src $dst /MIR /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Error "robocopy failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}
"published $src -> $dst"
"if the brain was running without the mount, restart the brain component (kill the 'python -m prana.brain' process; the supervisor respawns it in ~5s)"
exit 0
