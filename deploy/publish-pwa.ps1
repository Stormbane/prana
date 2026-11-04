# Publish the built narada-phone-app PWA to the brain's static mount.
# Build side (narada-phone-app repo): npm run deploy:demo
#   -> emits C:\Projects\narada-phone-app\out\narada
# This copies it to %LOCALAPPDATA%\narada\pwa, which the brain serves
# at /app on its own origin (same-origin with the API, no CORS needed):
#   https://<machine>.<tailnet>.ts.net:8443/app/
#
# Deploy protection (redesign architecture §7.5, 2026-11-04). The scene
# art (scene-packs/, ~180 MB) is untracked in the app repo, so a build
# from a clean checkout has none; the old /MIR then deleted the live art.
#   1. The live bundle is kept in pwa.prev first: rollback-pwa.ps1 puts it
#      back in one step.
#   2. The app is mirrored with scene-packs EXCLUDED (/XD keeps a folder
#      out of both the copy and the purge), so the live art is never
#      deleted, whatever the build holds.
#   3. Art in the build is added, never subtracted, and never replaces a
#      newer live file (/XO).
#   4. Then every pack the live daily.json names must be present with its
#      sha256; if not, the publish fails loudly with the rollback command.

[CmdletBinding()]
param(
    [string]$Source = "C:\Projects\narada-phone-app\out\narada",
    [string]$Target = (Join-Path $env:LOCALAPPDATA "narada\pwa")
)

$ErrorActionPreference = 'Stop'
$prev = "$Target.prev"

function Invoke-Robocopy([string[]]$argv) {
    & robocopy @argv /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $($argv[0]) -> $($argv[1]) failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path "$Source\index.html")) {
    Write-Error "no built PWA at $Source - run 'npm run deploy:demo' in narada-phone-app first"
    exit 1
}

# 1. The live bundle, for rollback (art excluded: it is never moved).
if (Test-Path "$Target\index.html") {
    Invoke-Robocopy @($Target, $prev, '/MIR', '/XD', 'scene-packs')
    "kept the previous bundle in $prev"
}

# 2. The app, mirrored, with the live art out of reach.
Invoke-Robocopy @($Source, $Target, '/MIR', '/XD', 'scene-packs')

# 3. Art from the build: additive, never older over newer.
if (Test-Path "$Source\scene-packs") {
    Invoke-Robocopy @("$Source\scene-packs", "$Target\scene-packs", '/E', '/XO')
    "added scene art from the build (nothing deleted)"
}

# 4. Every pack the live schedule names is there, intact.
$daily = Join-Path $Target "scene-packs\daily.json"
if (Test-Path $daily) {
    $sched = Get-Content $daily -Raw | ConvertFrom-Json
    $refs = @($sched.entries) + @($sched.rotation.packs) | Where-Object { $_ -and $_.manifest }
    $bad = @()
    foreach ($r in $refs) {
        $path = Join-Path (Join-Path $Target "scene-packs") ($r.manifest -replace '/', '\')
        if (-not (Test-Path $path)) { $bad += "$($r.manifest) missing"; continue }
        $hash = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant()
        if ($hash -ne $r.sha256) { $bad += "$($r.manifest) sha256 mismatch" }
    }
    if ($bad.Count -gt 0) {
        # -ErrorAction Continue: under Stop, Write-Error would throw and exit 1.
        Write-Error -ErrorAction Continue ("published, but the live scene schedule is broken: " +
            ($bad -join '; ') + ". Roll back with: deploy\rollback-pwa.ps1")
        exit 2
    }
    "scene schedule ok: $($refs.Count) pack reference(s) present and verified"
} else {
    "no scene schedule at $daily (the app falls back to the baked grove)"
}

"published $Source -> $Target"
"if the brain was running without the mount, restart the brain component (kill the 'python -m prana.brain' process; the supervisor respawns it in ~5s)"
exit 0
