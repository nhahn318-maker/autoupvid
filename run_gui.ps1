$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$requirements = "requirements.txt"
$stamp = ".venv\.deps_installed"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

if (
    -not (Test-Path $stamp) -or
    ((Get-Item $requirements).LastWriteTime -gt (Get-Item $stamp).LastWriteTime)
) {
    .\.venv\Scripts\python.exe -m pip install -r $requirements
    New-Item -ItemType File -Force -Path $stamp | Out-Null
}

.\.venv\Scripts\python.exe -m uvicorn src.ai_music_automation.web:app --host 127.0.0.1 --port 8000
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
