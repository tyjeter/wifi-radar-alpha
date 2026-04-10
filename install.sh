#!/bin/bash
# install.sh — One-command Raspberry Pi setup for Wi-Fi Radar.
#
# Usage:
#   curl -fsSL https://your-server/install.sh | sudo bash
#   — or —
#   sudo bash install.sh
#
# What this does:
#   1. Installs system dependencies (python3, iw, wireless-tools, cloudflared)
#   2. Creates a Python venv and installs pip packages
#   3. Writes ~/.wifi_radar_config.json with sensible defaults
#   4. Installs and enables wifi-radar.service (auto-start on boot)

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_FILE="/etc/systemd/system/wifi-radar.service"
CONFIG_FILE="$HOME/.wifi_radar_config.json"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[-]${NC} $*" >&2; }

# -----------------------------------------------------------------------
# Must run as root
# -----------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  error "Run as root: sudo bash install.sh"
  exit 1
fi

info "=== Wi-Fi Radar Install ==="
info "Install directory: $INSTALL_DIR"

# -----------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------
info "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv \
  iw wireless-tools net-tools \
  curl ca-certificates \
  2>/dev/null

# -----------------------------------------------------------------------
# 2. cloudflared
# -----------------------------------------------------------------------
if ! command -v cloudflared &>/dev/null; then
  info "Installing cloudflared..."
  ARCH=$(uname -m)
  case "$ARCH" in
    aarch64|arm64) CF_ARCH="arm64"   ;;
    armv7l|armv6l) CF_ARCH="armhf"   ;;
    x86_64)        CF_ARCH="amd64"   ;;
    *)             warn "Unknown arch $ARCH — skipping cloudflared"; CF_ARCH="" ;;
  esac
  if [[ -n "$CF_ARCH" ]]; then
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
    curl -fsSL "$CF_URL" -o /tmp/cloudflared
    install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
    info "cloudflared installed."
  fi
else
  info "cloudflared already installed."
fi

# -----------------------------------------------------------------------
# 3. Python venv + packages
# -----------------------------------------------------------------------
info "Creating Python venv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
  'scapy>=2.5.0' \
  'numpy>=1.24.0' \
  'scipy>=1.10.0' \
  'flask>=3.0.0' \
  'matplotlib>=3.7.0'
info "Python packages installed."

# -----------------------------------------------------------------------
# 4. Detect WiFi interface
# -----------------------------------------------------------------------
IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}' || echo "wlan0")
info "Detected WiFi interface: $IFACE"

# -----------------------------------------------------------------------
# 5. Write default config (don't overwrite existing)
# -----------------------------------------------------------------------
if [[ ! -f "$CONFIG_FILE" ]]; then
  info "Writing default config to $CONFIG_FILE..."
  cat > "$CONFIG_FILE" <<EOF
{
  "interface": "$IFACE",
  "tx_interface": null,
  "channel": null,
  "sensitivity": 2.0,
  "smoothing_window": 5,
  "fall_threshold_multiplier": 3.0,
  "breathing_detection": true,
  "false_positive_filter": true,
  "port": 5000,
  "device_name": "$(hostname)",
  "ha_webhook_url": null,
  "ha_token": null,
  "vercel_url": null,
  "auto_tunnel": false,
  "occupancy_tracking": true,
  "db_retention_days": 7,
  "alert_zones": []
}
EOF
  warn "Edit $CONFIG_FILE to configure your setup."
else
  info "Config already exists at $CONFIG_FILE — leaving untouched."
fi

# -----------------------------------------------------------------------
# 6. Systemd service
# -----------------------------------------------------------------------
info "Installing systemd service..."

# Determine the user to run as (default: pi, else the sudo user)
RUN_USER="${SUDO_USER:-pi}"
if ! id "$RUN_USER" &>/dev/null; then
  RUN_USER="root"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Wi-Fi Radar Motion Detection
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/watchdog.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wifi-radar

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wifi-radar
info "Service installed and enabled (auto-start on boot)."

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
echo ""
info "=== Setup complete! ==="
echo ""
echo "  1. Edit config:      nano $CONFIG_FILE"
echo "  2. Start now:        sudo systemctl start wifi-radar"
echo "  3. View logs:        sudo journalctl -u wifi-radar -f"
echo "  4. Dashboard:        http://$(hostname -I | awk '{print $1}'):5000"
echo ""
