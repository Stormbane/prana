# install.ps1 -- register the Narada Host orchestrator as a Windows
# Scheduled Task that fires at user logon.
#
# Why Task Scheduler over the Startup folder we used earlier:
#   - no console window on logon (hidden run as pythonw)
#   - restart-on-failure built in (the task does this; orchestrator
#     does it again at the component layer for finer control)
#   - falls back gracefully when schtasks /Create is denied
#
# Idempotent: re-running with -Force re-registers.
#
# After running this, Suti can also (manually, when ready):
#   - remove Hermes_Gateway.cmd and Narada_Chat_Bridge.cmd from the
#     Startup folder
#   - flip enabled=true for chat-bridge and agent-gateway in
#     ~/.narada/host/components.yaml
# That is the Stage 5 migration step -- explicitly NOT done by this
# script because it touches currently-running services.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$TaskName = "Narada_Host"
$TaskDescription = "Narada runtime orchestrator (prana host)"

# Resolve pythonw -- must be the same interpreter prana is installed into
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Error "pythonw.exe not found on PATH. Install Python 3.12+ first."
    exit 1
}
Write-Host "pythonw: $pythonw"

# Verify prana is importable from this pythonw
& $pythonw -c "import prana.host" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "prana.host is not importable from $pythonw. Run ``pip install -e C:\Projects\prana`` first."
    exit 1
}
Write-Host "prana.host importable: OK"

# Build the action
$pranaArgs = "-m prana.host run"
Write-Host "command: $pythonw $pranaArgs"

if ($DryRun) {
    Write-Host "DRY RUN -- would register task '$TaskName' at logon"
    exit 0
}

# Check for existing task; if found and -Force, remove first
$existing = schtasks /Query /TN $TaskName 2>$null
if ($existing -and -not $Force) {
    Write-Host "Task '$TaskName' already exists. Use -Force to re-register."
    exit 2
}
if ($existing) {
    Write-Host "Removing existing task..."
    schtasks /Delete /TN $TaskName /F | Out-Null
}

# Build an XML task definition (richer than /Create flags allow -- gives
# us proper restart-on-failure semantics).
$user = "$env:USERDOMAIN\$env:USERNAME"
$startBoundary = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$TaskDescription</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$pythonw</Command>
      <Arguments>$pranaArgs</Arguments>
    </Exec>
  </Actions>
</Task>
"@

# Write XML to a temp file, register, clean up
$xmlPath = Join-Path $env:TEMP "narada-host-task.xml"
$xml | Out-File -FilePath $xmlPath -Encoding Unicode

try {
    Write-Host "Registering scheduled task '$TaskName'..."
    schtasks /Create /TN $TaskName /XML $xmlPath /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "schtasks /Create failed -- see error above."
        Write-Host ""
        Write-Host "Fallback option: Startup-folder shim (loses some Task Scheduler benefits)."
        Write-Host "Run: prana host install --fallback-startup-folder"
        exit 3
    }
} finally {
    Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "[OK] Installed scheduled task '$TaskName'"
Write-Host "  Triggers at user logon for $user"
Write-Host "  Runs: $pythonw $pranaArgs"
Write-Host ""
Write-Host "To start now without waiting for next logon:"
Write-Host "  schtasks /Run /TN $TaskName"
Write-Host ""
Write-Host "To uninstall:"
Write-Host "  prana host uninstall"
