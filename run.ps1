param(
    [ValidateSet("init", "render", "upload", "daily", "login-account", "auto-images")]
    [string]$Command = "daily",
    [int]$Limit = 0,
    [switch]$DryRun,
    [string]$TokenFile = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$ffmpegBin = Join-Path $PSScriptRoot "tools\ffmpeg\bin"
if (Test-Path -LiteralPath $ffmpegBin) {
    $env:PATH = "$ffmpegBin;$env:PATH"
}

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

$arguments = @("-m", "src.ai_music_automation.cli", $Command)
if ($Limit -gt 0) {
    $arguments += @("--limit", $Limit)
}
if ($DryRun) {
    $arguments += "--dry-run"
}
if ($TokenFile) {
    $arguments += @("--token-file", $TokenFile)
}
if ($ExtraArgs) {
    $arguments += $ExtraArgs
}

.\.venv\Scripts\python.exe @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
