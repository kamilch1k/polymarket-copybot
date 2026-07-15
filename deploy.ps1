# One-command deploy: self-check -> commit -> push -> relaunch -> health check.
# Usage:  powershell -ExecutionPolicy Bypass -File deploy.ps1 -Message "fix: ..."
param([Parameter(Mandatory = $true)][string]$Message)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1) never ship a build that fails its own tests
& python copybot.py --check
if ($LASTEXITCODE -ne 0) { throw "self-check FAILED - not deploying" }

# 2) commit + push (skipped cleanly when there are no changes)
git add -A
$dirty = git status --porcelain
if ($dirty) {
    git commit -m $Message -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    git push
    Write-Host ">> committed + pushed: $Message"
} else {
    Write-Host ">> no file changes - relaunch only"
}

# 3) relaunch the app so the running process matches the code on disk
# (kill by command line: quotebot shares the pythonw binary, never touch it)
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'copybot\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
Start-Sleep 2
# headless: the managed bot never owns a window — windows are disposable second
# instances (desktop shortcut), so closing one can't kill the bot anymore
Start-Process -FilePath "C:\Users\rewwe\AppData\Local\Programs\Python\Python312\pythonw.exe" `
    -ArgumentList "`"$PSScriptRoot\copybot.py`" --headless" -WorkingDirectory $PSScriptRoot
Start-Sleep 8

# 4) prove it came back
$r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8777/dyn -TimeoutSec 15
if ($r.StatusCode -ne 200) { throw "app did not come back after relaunch!" }
Write-Host ">> deployed - app healthy ($($r.Content.Length) bytes served)"
