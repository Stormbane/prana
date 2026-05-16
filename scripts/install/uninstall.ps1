# uninstall.ps1 -- remove the Narada Host scheduled task. Idempotent.
#
# Does NOT touch the components.yaml or the lockfile -- only the
# autostart trigger. If the orchestrator is currently running, this
# script doesn't stop it; use ``schtasks /End /TN Narada_Host`` or
# kill the pythonw process explicitly.

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "Narada_Host"

$existing = schtasks /Query /TN $TaskName 2>$null
if (-not $existing) {
    Write-Host "Task '$TaskName' not registered -- nothing to do."
    exit 0
}

Write-Host "Removing scheduled task '$TaskName'..."
schtasks /Delete /TN $TaskName /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks /Delete failed."
    exit 1
}

Write-Host "[OK] Removed scheduled task '$TaskName'"
Write-Host ""
Write-Host "The orchestrator process (if running) was not affected."
Write-Host "Stop it manually if needed: kill the pythonw running 'prana host run'."
