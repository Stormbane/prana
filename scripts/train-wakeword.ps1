# Train the "Narada" wake-word model end to end (hours; GPU recommended).
# Safe to re-run: each stage is incremental / idempotent per livekit-wakeword.
$ErrorActionPreference = "Stop"
$config = Join-Path $PSScriptRoot "..\config\wakeword\narada.yaml"

Write-Host "[1/5] setup (downloads: piper VITS, ACAV features, MUSAN, RIRs)"
python -m livekit.wakeword setup --config $config
if ($LASTEXITCODE -ne 0) { throw "setup failed" }

Write-Host "[2/5] generate (synthetic TTS utterances)"
python -m livekit.wakeword generate $config
if ($LASTEXITCODE -ne 0) { throw "generate failed" }

Write-Host "[3/5] augment (noise / reverb / feature extraction)"
python -m livekit.wakeword augment $config
if ($LASTEXITCODE -ne 0) { throw "augment failed" }

Write-Host "[4/5] train"
python -m livekit.wakeword train $config
if ($LASTEXITCODE -ne 0) { throw "train failed" }

Write-Host "[5/5] export to ONNX"
python -m livekit.wakeword export $config
if ($LASTEXITCODE -ne 0) { throw "export failed" }

Write-Host "done: $env:USERPROFILE\.narada\wakeword\output\narada\narada.onnx"
