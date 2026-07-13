$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

$existing = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -First 1

if ($existing) {
    Write-Host "Mobile gateway is already running on port 8765. PID: $existing"
    exit 0
}

.\.venv\Scripts\python.exe -m src.ai_music_automation.mobile_gateway
