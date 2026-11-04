# Put the previous PWA bundle back (the one publish-pwa.ps1 kept in
# pwa.prev before its last publish). The scene art is not touched: it is
# never moved by a publish, so there is nothing of it to roll back.

[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA "narada\pwa")
)

$ErrorActionPreference = 'Stop'
$prev = "$Target.prev"
if (-not (Test-Path "$prev\index.html")) {
    Write-Error "no previous bundle at $prev - nothing to roll back to"
    exit 1
}
& robocopy $prev $Target /MIR /XD scene-packs /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Error "robocopy failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
"rolled back: $prev -> $Target (scene art untouched)"
exit 0
