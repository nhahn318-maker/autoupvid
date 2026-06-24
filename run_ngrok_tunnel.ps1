$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$ngrok = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ngrok.exe"
$configPath = Join-Path $root ".ngrok\ngrok.yml"
$logDir = Join-Path $root "logs"
$stdoutLog = Join-Path $logDir "ngrok.out.log"
$stderrLog = Join-Path $logDir "ngrok.err.log"
$apiUrl = "http://127.0.0.1:4040/api/tunnels"

if (-not (Test-Path -LiteralPath $ngrok)) {
    throw "Khong tim thay ngrok.exe tai $ngrok"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Chua co authtoken. Hay chay .\set_ngrok_authtoken.ps1 truoc."
}

try {
    Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/status -TimeoutSec 5 | Out-Null
} catch {
    throw "Web local chua chay o http://127.0.0.1:8000 . Hay mo run_gui.ps1 truoc."
}

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item -LiteralPath $stdoutLog -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stderrLog -ErrorAction SilentlyContinue

$args = @(
    "http",
    "8000",
    "--config", $configPath,
    "--log", "stdout"
)

Start-Process -FilePath $ngrok -ArgumentList $args -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Hidden

Write-Host "Dang khoi dong ngrok..." -ForegroundColor Cyan

$deadline = (Get-Date).AddSeconds(75)
$publicUrl = $null
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 3
        $urls = @($response.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -ExpandProperty public_url)
        if ($urls.Count -gt 0) {
            $publicUrl = $urls[0]
            break
        }
    } catch {
    }
    Start-Sleep -Milliseconds 750
}

if (-not $publicUrl) {
    Write-Host "Khong lay duoc public URL. Kiem tra log:" -ForegroundColor Yellow
    Write-Host $stdoutLog -ForegroundColor Yellow
    Write-Host $stderrLog -ForegroundColor Yellow
    exit 1
}

Write-Host "ngrok da san sang:" -ForegroundColor Green
Write-Host $publicUrl -ForegroundColor Green
Write-Host "Mo link nay tren dien thoai 4G/Wi-Fi deu duoc." -ForegroundColor Green
