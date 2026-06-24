$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$ngrok = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ngrok.exe"
$configDir = Join-Path $root ".ngrok"
$configPath = Join-Path $configDir "ngrok.yml"

if (-not (Test-Path -LiteralPath $ngrok)) {
    throw "Khong tim thay ngrok.exe tai $ngrok"
}

if (-not (Test-Path -LiteralPath $configDir)) {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
}

$secureToken = Read-Host "Nhap ngrok authtoken" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Authtoken trong."
}

& $ngrok config add-authtoken $token --config $configPath
if ($LASTEXITCODE -ne 0) {
    throw "Khong the luu authtoken vao $configPath"
}

Write-Host "Da luu ngrok authtoken vao $configPath" -ForegroundColor Green
