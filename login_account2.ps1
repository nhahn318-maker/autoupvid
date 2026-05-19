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

.\.venv\Scripts\python.exe -m src.ai_music_automation.cli login-account --token-file token_account2.json
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
