$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
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

$hostIp = $null
try {
    $hostIp = (
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Select-Object -First 1 -ExpandProperty IPAddress
    )
} catch {
    $hostIp = $null
}

Write-Host "Web UI dang chay tren tat ca dia chi mang noi bo o cong 8000." -ForegroundColor Cyan
Write-Host "May nay: http://127.0.0.1:8000" -ForegroundColor Cyan
if ($hostIp) {
    Write-Host "Dien thoai cung Wi-Fi co the mo: http://$hostIp`:8000" -ForegroundColor Green
}

.\.venv\Scripts\python.exe -m uvicorn src.ai_music_automation.web:app --host 0.0.0.0 --port 8000
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
