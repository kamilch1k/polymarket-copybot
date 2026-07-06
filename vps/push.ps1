# Push current code to the VPS and restart the service. Config (wallet key)
# is NOT pushed by default — pass -WithConfig once, on the owner's say-so.
param(
    [Parameter(Mandatory)][string]$VpsIp,
    [switch]$WithConfig
)
$key = "$env:USERPROFILE\.ssh\copybot_vps"
$dir = Split-Path $PSScriptRoot -Parent

scp -i $key "$dir\copybot.py" "$dir\watchdog.py" "root@${VpsIp}:/opt/copybot/"
if ($WithConfig) {
    scp -i $key "$dir\copybot_config.json" "root@${VpsIp}:/opt/copybot/"
    ssh -i $key "root@$VpsIp" "chown copybot:copybot /opt/copybot/copybot_config.json && chmod 600 /opt/copybot/copybot_config.json"
}
ssh -i $key "root@$VpsIp" "systemctl restart copybot && sleep 8 && /opt/copybot/venv/bin/python /opt/copybot/watchdog.py"
