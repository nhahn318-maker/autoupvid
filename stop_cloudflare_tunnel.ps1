$ErrorActionPreference = "Stop"
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "Da dung Cloudflare Tunnel." -ForegroundColor Yellow
