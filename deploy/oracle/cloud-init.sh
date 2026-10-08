#!/bin/bash
# QSTS 24/7 server on an Oracle Cloud "Always Free" machine (Canonical Ubuntu 22.04 or 24.04).
# Paste this whole text in Oracle Cloud: Create instance -> Show advanced options -> Management ->
# "Paste cloud-init script". Before pasting, fill in the values between the quotes below.
TAILSCALE_KEY=""     # Tailscale auth key (admin console -> Settings -> Keys -> Generate auth key), starts with tskey-auth-
GITHUB_TOKEN=""      # read-only GitHub token, only needed if the repository is private
REPO="Guille027/quant-swing-trader"
BRANCH="claude/quirky-mayer-g6nhsj"

exec > /var/log/qsts-install.log 2>&1
set -eux
export DEBIAN_FRONTEND=noninteractive

# 2 GB of swap: the small free AMD machine has only 1 GB of memory
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y git curl
# uv installs its own Python 3.12, so this works on Ubuntu 22.04 and 24.04 alike
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

U=ubuntu
H=/home/$U
URL="https://github.com/$REPO.git"
if [ -n "$GITHUB_TOKEN" ]; then URL="https://x-access-token:$GITHUB_TOKEN@github.com/$REPO.git"; fi
[ -d $H/qsts ] || sudo -u $U git clone -b "$BRANCH" "$URL" $H/qsts
sudo -u $U uv venv --python 3.12 $H/qsts/.venv
sudo -u $U uv pip install --python $H/qsts/.venv/bin/python -q -e "$H/qsts[ui,yahoo,alpaca]"

cat > /etc/systemd/system/qsts.service <<EOF
[Unit]
Description=QSTS (servidor 24/7)
After=network-online.target
Wants=network-online.target

[Service]
User=$U
WorkingDirectory=$H/qsts
ExecStart=$H/qsts/.venv/bin/qsts server --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now qsts

# Private access from your phone and PC only: the app listens on 127.0.0.1 and Tailscale forwards it to your
# own devices (nothing is opened to the internet)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey="$TAILSCALE_KEY" --hostname=qsts
tailscale serve --bg --http=80 127.0.0.1:8765
echo "QSTS instalado"
