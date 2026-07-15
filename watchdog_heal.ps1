# Self-heal: relaunch copybot if the dashboard stopped serving.
# Registered as a Windows scheduled task (fires every 10 min while logged on):
#   schtasks /Create /TN CopybotWatchdog /SC MINUTE /MO 10 /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\cc\copybot\watchdog_heal.ps1"
# Runs independent of any Claude/terminal session — catches window-close deaths
# and crashes that previously stayed down until a human noticed (a full flat day
# once). Deep health checks (missed trades, stale polls) stay in watchdog.py.
$ErrorActionPreference = "SilentlyContinue"
try { $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8777/dyn -TimeoutSec 10 } catch { $r = $null }
if ($r -and $r.StatusCode -eq 200) { exit 0 }   # serving = alive enough

# a deploy is mid-restart: let it finish instead of racing it
if (Get-CimInstance Win32_Process -Filter "Name LIKE 'powershell%'" |
        Where-Object { $_.CommandLine -match 'deploy\.ps1' }) { exit 0 }

Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'copybot\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }   # OUR zombie only — quotebot runs as pythonw too
Start-Sleep 2
# headless: a background resurrection must never pop a window (the user closing
# a surprise window is exactly what used to kill the bot into a heal loop)
Start-Process -FilePath "C:\Users\rewwe\AppData\Local\Programs\Python\Python312\pythonw.exe" `
    -ArgumentList "`"$PSScriptRoot\copybot.py`" --headless" -WorkingDirectory $PSScriptRoot
Add-Content "$PSScriptRoot\watchdog_heal.log" "$(Get-Date -Format s) dashboard was down - relaunched"
