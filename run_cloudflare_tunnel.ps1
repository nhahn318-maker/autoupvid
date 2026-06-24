$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$cloudflared = Join-Path $root "tools\cloudflared\cloudflared.exe"
$logDir = Join-Path $root "logs"
$stdoutLog = Join-Path $logDir "cloudflare-tunnel.out.log"
$stderrLog = Join-Path $logDir "cloudflare-tunnel.err.log"

if (-not (Test-Path -LiteralPath $cloudflared)) {
    throw "Missing cloudflared.exe at $cloudflared"
}

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

Remove-Item -LiteralPath $stdoutLog -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stderrLog -ErrorAction SilentlyContinue

$args = @(
    "tunnel",
    "--url",
    "http://127.0.0.1:8000",
    "--no-autoupdate"
)

Start-Process -FilePath $cloudflared -ArgumentList $args -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Hidden

Write-Host "Dang khoi dong Cloudflare Tunnel..." -ForegroundColor Cyan

$deadline = (Get-Date).AddSeconds(45)
$url = $null
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $stdoutLog) {
        $match = Select-String -Path $stdoutLog -Pattern "https://[-a-z0-9]+\.trycloudflare\.com" -AllMatches -ErrorAction SilentlyContinue
        if ($match) {
            $url = $match.Matches[-1].Value
            break
        }
    }
    Start-Sleep -Milliseconds 500
}

if (-not $url) {
    Write-Host "Chua lay duoc link tunnel. Kiem tra log: $stdoutLog" -ForegroundColor Yellow
    exit 1
}

Write-Host "Cloudflare Tunnel da san sang:" -ForegroundColor Green
Write-Host $url -ForegroundColor Green
Write-Host "Mo link tren dien thoai 4G/Wi-Fi deu duoc." -ForegroundColor Green
