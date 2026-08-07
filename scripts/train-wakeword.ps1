# Train the "Narada" wake-word model end to end (hours; GPU recommended).
# Safe to re-run: each stage is incremental / idempotent per livekit-wakeword.
$ErrorActionPreference = "Stop"
$config = Join-Path $PSScriptRoot "..\config\wakeword\narada.yaml"

# Pin the interpreter that actually has livekit-wakeword + torch installed.
# Bare `python` is NOT safe here: under a scheduled task / minimal shell it
# resolves to the Hermes venv (no livekit). Prefer $env:PRANA_PYTHON, else
# the Anaconda base python, else PATH.
$py = $env:PRANA_PYTHON
if (-not $py -or -not (Test-Path $py)) {
    $candidates = @(
        "C:\ProgramData\anaconda3\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    $py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $py) { $py = (Get-Command python -ErrorAction Stop).Source }
}
Write-Host "using interpreter: $py"

# espeak-ng (Piper's phonemizer) is required by the generate stage. It
# was installed via MSI administrative-extract to LOCALAPPDATA (no admin
# needed); make it discoverable to shutil.which and point it at its data.
$espeakDir = Join-Path $env:LOCALAPPDATA "espeak-ng\eSpeak NG"
if (Test-Path (Join-Path $espeakDir "espeak-ng.exe")) {
    $env:PATH = "$espeakDir;$env:PATH"
    $env:ESPEAK_DATA_PATH = Join-Path $espeakDir "espeak-ng-data"
} elseif (-not (Get-Command espeak-ng -ErrorAction SilentlyContinue)) {
    throw "espeak-ng not found (expected $espeakDir or on PATH)"
}

Write-Host "[1/5] setup (downloads: piper VITS, ACAV features, MUSAN, RIRs)"
& $py -m livekit.wakeword setup --config $config
if ($LASTEXITCODE -ne 0) { throw "setup failed" }

Write-Host "[2/5] generate (synthetic TTS utterances)"
& $py -m livekit.wakeword generate $config
if ($LASTEXITCODE -ne 0) { throw "generate failed" }

Write-Host "[3/5] augment (noise / reverb / feature extraction)"
& $py -m livekit.wakeword augment $config
if ($LASTEXITCODE -ne 0) { throw "augment failed" }

Write-Host "[4/5] train"
& $py -m livekit.wakeword train $config
if ($LASTEXITCODE -ne 0) { throw "train failed" }

Write-Host "[5/5] export to ONNX"
& $py -m livekit.wakeword export $config
if ($LASTEXITCODE -ne 0) { throw "export failed" }

Write-Host "done: $env:USERPROFILE\.narada\wakeword\output\narada\narada.onnx"
