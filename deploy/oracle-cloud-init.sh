#!/bin/bash
# Oracle Cloud (Ubuntu 24.04, Ampere A1) user-data for the always-on plant host.
# Paste the generated copy (with the gateway token filled in) into
# "Advanced options -> Management -> Initialization script" when creating the VM.
# It installs the repository, then runs deploy/runtime_agent.py as a systemd service.
# The agent starts the simulator/gateway only while an operator's start request is active.
set -euo pipefail

GATEWAY_TOKEN='__GATEWAY_TOKEN__'
REPO=https://github.com/jeonking80/HMI-MES.git
APP=/opt/hmi-mes
STATE=/var/lib/hmi-mes

# A public IP may be attached only after the VM is created; wait up to 2 hours for internet.
for _ in $(seq 1 720); do
  getent hosts github.com >/dev/null 2>&1 && curl -fsS -o /dev/null https://github.com && break
  sleep 10
done
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv
id hmi >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin hmi
install -d -o hmi -g hmi -m 700 "$STATE" /etc/hmi-mes
umask 077
printf '%s\n' "$GATEWAY_TOKEN" > /etc/hmi-mes/gateway.token
chown hmi:hmi /etc/hmi-mes/gateway.token
chmod 600 /etc/hmi-mes/gateway.token

if [ ! -d "$APP/.git" ]; then
  git clone --depth 1 "$REPO" "$APP"
fi
chown -R hmi:hmi "$APP"
sudo -u hmi python3 -m venv "$APP/.venv"
sudo -u hmi "$APP/.venv/bin/pip" install --quiet \
  alembic==1.20.0 asyncua==2.1.0 certifi==2026.10.19 cryptography==50.0.2 \
  fastapi==0.152.0 httpx==0.28.1 'psycopg[binary]==3.3.6' pydantic==2.14.1 \
  sqlalchemy==2.1.5 uvicorn==0.38.0 websockets==15.0.1

cat > /etc/systemd/system/hmi-runtime-agent.service <<UNIT
[Unit]
Description=HMI-MES plant runtime agent (follows the web on/off request)
After=network-online.target
Wants=network-online.target

[Service]
User=hmi
Group=hmi
WorkingDirectory=$APP
ExecStartPre=/usr/bin/git -C $APP pull --ff-only
ExecStart=$APP/.venv/bin/python deploy/runtime_agent.py --directory $STATE/runtime --token-file /etc/hmi-mes/gateway.token
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now hmi-runtime-agent.service
