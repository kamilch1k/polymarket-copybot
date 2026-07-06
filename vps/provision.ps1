# Create the copybot VPS via the Hetzner Cloud API - no web UI, just an API token.
# Token: pass -Token or set $env:HCLOUD_TOKEN. Dry by default; add -Create to provision.
#   Default:  EU Falkenstein CX22 (cheapest, x86, 4GB).  US: -ServerType cpx11 -Location ash
param(
    [string]$Token = $env:HCLOUD_TOKEN,
    [string]$ServerType = "cx22",
    [string]$Location = "fsn1",
    [string]$Name = "copybot",
    [switch]$Create
)
$ErrorActionPreference = "Stop"
if (-not $Token) { throw "no token: pass -Token or set env HCLOUD_TOKEN (Hetzner console > Security > API Tokens)" }
$pub = (Get-Content "$env:USERPROFILE\.ssh\copybot_vps.pub" -Raw).Trim()
$api = "https://api.hetzner.cloud/v1"
$hdr = @{ Authorization = "Bearer $Token" }

Write-Host "plan: create '$Name' - $ServerType / ubuntu-24.04 / $Location, SSH key copybot-vps"
if (-not $Create) { Write-Host "dry run - re-run with -Create to actually provision (billable)."; return }

# 1) SSH key (idempotent - ignore 'already exists')
try {
    Invoke-RestMethod -Method Post -Uri "$api/ssh_keys" -Headers $hdr -ContentType application/json `
        -Body (@{ name = "copybot-vps"; public_key = $pub } | ConvertTo-Json) | Out-Null
    Write-Host "ssh key uploaded"
} catch { Write-Host "ssh key already present (ok)" }

# 2) server
$body = @{ name = $Name; server_type = $ServerType; image = "ubuntu-24.04";
           location = $Location; ssh_keys = @("copybot-vps"); start_after_create = $true } | ConvertTo-Json
$srv = (Invoke-RestMethod -Method Post -Uri "$api/servers" -Headers $hdr -ContentType application/json -Body $body).server
Write-Host "server $($srv.id) creating..."

# 3) poll for a running IP
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 5
    $s = (Invoke-RestMethod -Uri "$api/servers/$($srv.id)" -Headers $hdr).server
    $ip = $s.public_net.ipv4.ip
    if ($s.status -eq "running" -and $ip) {
        Write-Host "`nREADY  ip=$ip  status=running"
        Write-Host "next: ssh -i `$env:USERPROFILE\.ssh\copybot_vps root@$ip"
        return
    }
}
throw "timed out waiting for the server to come up - check the Hetzner console"
