#!/usr/bin/env bash
# One-shot copybot VPS bootstrap (Ubuntu 24.04, run as root).
# Expects copybot.py, watchdog.py, copybot_config.json and this vps/ dir
# already copied to /opt/copybot (see vps/push.ps1).
set -euo pipefail

apt-get update
apt-get -y install python3-venv unattended-upgrades ufw

id -u copybot &>/dev/null || useradd -r -d /opt/copybot -s /usr/sbin/nologin copybot
python3 -m venv /opt/copybot/venv
/opt/copybot/venv/bin/pip install -q requests websocket-client regex py-clob-client-v2

chown -R copybot:copybot /opt/copybot
chmod 600 /opt/copybot/copybot_config.json   # holds the wallet key

# UI stays loopback-only (copybot.py binds 127.0.0.1); firewall allows SSH alone.
ufw allow OpenSSH
ufw --force enable

cp /opt/copybot/vps/copybot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now copybot
sleep 8
/opt/copybot/venv/bin/python /opt/copybot/watchdog.py
