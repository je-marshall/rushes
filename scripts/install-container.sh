#!/usr/bin/env bash
# Run inside the LXC container as root, from the repo root.
#
# Usage:
#   sudo bash scripts/install-container.sh
#   sudo bash scripts/install-container.sh --jellyfin-url http://192.168.1.x:8096 \
#                                           --jellyfin-token your-api-key
#
# Safe to re-run — idempotent.

set -euo pipefail

RUSHES_DATA="${RUSHES_DATA:-/var/lib/rushes}"
VENV="/opt/rushes-venv"
JELLYFIN_URL="${JELLYFIN_URL:-}"
JELLYFIN_TOKEN="${JELLYFIN_TOKEN:-}"

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jellyfin-url)    JELLYFIN_URL="$2";   shift 2 ;;
        --jellyfin-token)  JELLYFIN_TOKEN="$2"; shift 2 ;;
        --data-dir)        RUSHES_DATA="$2";    shift 2 ;;
        -h|--help)
            echo "Usage: install-container.sh [--jellyfin-url URL] [--jellyfin-token TOKEN] [--data-dir PATH]"
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root — sudo bash scripts/install-container.sh"
    exit 1
fi

if [[ ! -f pyproject.toml ]]; then
    echo "ERROR: run from the repo root directory"
    exit 1
fi

REPO_DIR="$(pwd)"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
echo "==> Installing system packages"
apt-get update -qq
apt-get install -y python3 python3-venv ffmpeg isc-dhcp-client

# ---------------------------------------------------------------------------
# Python venv + package
# ---------------------------------------------------------------------------
echo "==> Setting up Python venv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO_DIR"

# Symlink the ingest command — gopro-connect.sh on the host calls this via pct exec
ln -sf "$VENV/bin/rushes-ingest" /usr/local/bin/rushes-ingest
echo "    symlinked rushes-ingest → /usr/local/bin/rushes-ingest"

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
echo "==> Creating data directory $RUSHES_DATA"
mkdir -p "$RUSHES_DATA"

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------
echo "==> Installing rushes-web systemd service"

# Build the Environment= lines conditionally
ENV_LINES="Environment=RUSHES_DATA=$RUSHES_DATA"
[[ -n "$JELLYFIN_URL"   ]] && ENV_LINES+=$'\n'"Environment=JELLYFIN_URL=$JELLYFIN_URL"
[[ -n "$JELLYFIN_TOKEN" ]] && ENV_LINES+=$'\n'"Environment=JELLYFIN_TOKEN=$JELLYFIN_TOKEN"

cat > /etc/systemd/system/rushes-web.service <<EOF
[Unit]
Description=Rushes web UI
After=network.target

[Service]
Type=simple
ExecStart=$VENV/bin/rushes-web
Restart=on-failure
RestartSec=5
$ENV_LINES

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rushes-web
systemctl restart rushes-web

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
LOCAL_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "All done."
echo ""
echo "  Web UI:  http://${LOCAL_IP}:8765"
echo "  Data:    $RUSHES_DATA"
echo "  Logs:    journalctl -u rushes-web -f"
echo ""

if [[ -z "$JELLYFIN_URL" ]]; then
    echo "  Jellyfin not configured. To add it later, re-run with:"
    echo "    sudo bash scripts/install-container.sh \\"
    echo "      --jellyfin-url http://<jellyfin-host>:8096 \\"
    echo "      --jellyfin-token <api-key>"
    echo ""
fi

echo "  To update after a git pull:"
echo "    $VENV/bin/pip install -e $REPO_DIR && systemctl restart rushes-web"
echo ""
