$ErrorActionPreference = "Stop"
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "Da dung ngrok." -ForegroundColor Yellow
