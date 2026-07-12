# Build a double-clickable Copybot app: dist\Copybot.exe (Windows).
# Usage:  powershell -ExecutionPolicy Bypass -File package.ps1  [-Console]
# -Console keeps a terminal window attached (useful to see errors).
param([switch]$Console)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --quiet --upgrade pyinstaller
$flags = @("--noconfirm", "--onefile", "--name", "Copybot",
           "--collect-all", "py_clob_client_v2", "--collect-all", "eth_account",
           "--collect-all", "eth_utils", "--collect-all", "websocket",
           "--collect-all", "webview", "--collect-all", "regex")
if (-not $Console) { $flags += "--windowed" }
python -m PyInstaller @flags copybot.py

Write-Host ">> built dist\Copybot.exe"
Write-Host ">> config + state persist NEXT TO the exe - keep it in its own folder"
& "$PSScriptRoot\dist\Copybot.exe" --check
if ($LASTEXITCODE -ne 0) { throw "packaged binary failed its self-check" }
Write-Host ">> packaged binary passed the self-check"
