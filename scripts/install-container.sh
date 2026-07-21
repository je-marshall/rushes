#!/usr/bin/env bash
# Run inside the LXC container as root. Can be called from anywhere.
#
# Usage:
#   sudo bash /path/to/rushes/scripts/install-container.sh
#   sudo bash /path/to/rushes/scripts/install-container.sh \
#     --jellyfin-url http://192.168.1.x:8096 --jellyfin-token your-api-key
#
# Safe to re-run — idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

RUSHES_DATA="${RUSHES_DATA:-/var/lib/rushes}"
VENV="/opt/rushes-venv"
JELLYFIN_URL="${JELLYFIN_URL:-}"
JELLYFIN_TOKEN="${JELLYFIN_TOKEN:-}"
AUTH_USERNAME="${RUSHES_USERNAME:-rushes}"
AUTH_PASSWORD="${RUSHES_PASSWORD:-}"
SECRET_KEY="${RUSHES_SECRET_KEY:-}"

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jellyfin-url)    JELLYFIN_URL="$2";   shift 2 ;;
        --jellyfin-token)  JELLYFIN_TOKEN="$2"; shift 2 ;;
        --data-dir)        RUSHES_DATA="$2";    shift 2 ;;
        --username)        AUTH_USERNAME="$2";  shift 2 ;;
        --password)        AUTH_PASSWORD="$2";  shift 2 ;;
        -h|--help)
            echo "Usage: install-container.sh [--jellyfin-url URL] [--jellyfin-token TOKEN] [--data-dir PATH] [--username USER] [--password PASS]"
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

if [[ ! -f "$REPO_DIR/pyproject.toml" ]]; then
    echo "ERROR: could not find repo root (expected pyproject.toml at $REPO_DIR)"
    exit 1
fi

# Prompt for password if not provided
if [[ -z "$AUTH_PASSWORD" ]]; then
    read -rsp "Set Rushes password (for user '$AUTH_USERNAME'): " AUTH_PASSWORD; echo
    read -rsp "Confirm password: " AUTH_PASSWORD2; echo
    if [[ "$AUTH_PASSWORD" != "$AUTH_PASSWORD2" ]]; then
        echo "ERROR: passwords do not match"; exit 1
    fi
    if [[ -z "$AUTH_PASSWORD" ]]; then
        echo "ERROR: password cannot be empty"; exit 1
    fi
fi

# Generate a secret key if not provided
if [[ -z "$SECRET_KEY" ]]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "    generated secret key"
fi

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

# Install the ingest command as a WRAPPER (not a bare symlink). gopro-connect.sh
# on the host calls this via `pct exec`, which does NOT inherit the systemd
# service's Environment= vars — so we bake RUSHES_DATA in here. Otherwise ingest
# would write to the default path while the web UI reads the configured one.
cat > /usr/local/bin/rushes-ingest <<EOF
#!/usr/bin/env bash
export RUSHES_DATA='$RUSHES_DATA'
exec "$VENV/bin/rushes-ingest" "\$@"
EOF
chmod +x /usr/local/bin/rushes-ingest
echo "    installed rushes-ingest wrapper → /usr/local/bin/rushes-ingest (RUSHES_DATA=$RUSHES_DATA)"

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
echo "==> Creating data directory $RUSHES_DATA"
mkdir -p "$RUSHES_DATA"

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------
echo "==> Installing rushes-web systemd service"

# Build the Environment= lines. PYTHONUNBUFFERED so logs reach the journal live.
ENV_LINES="Environment=PYTHONUNBUFFERED=1"
ENV_LINES+=$'\n'"Environment=RUSHES_DATA=$RUSHES_DATA"
ENV_LINES+=$'\n'"Environment=RUSHES_SECRET_KEY=$SECRET_KEY"
ENV_LINES+=$'\n'"Environment=RUSHES_USERNAME=$AUTH_USERNAME"
ENV_LINES+=$'\n'"Environment=RUSHES_PASSWORD=$AUTH_PASSWORD"
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

# The self-healing ingest daemon. Needs RUSHES_DATA (DB location) and unbuffered
# output; it manages interfaces + DHCP so it runs as root (the container's root).
cat > /etc/systemd/system/rushes-watch.service <<EOF
[Unit]
Description=Rushes GoPro ingest watcher
After=network.target

[Service]
Type=simple
ExecStart=$VENV/bin/rushes-watch
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=RUSHES_DATA=$RUSHES_DATA

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rushes-web rushes-watch
systemctl restart rushes-web rushes-watch

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
LOCAL_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "All done."
echo ""
echo "  Web UI:  http://${LOCAL_IP}:8765"
echo "  Data:    $RUSHES_DATA"
echo "  Logs:    journalctl -u rushes-web -f       (web UI)"
echo "           journalctl -u rushes-watch -f     (GoPro ingest daemon)"
echo ""

if [[ -z "$JELLYFIN_URL" ]]; then
    echo "  Jellyfin not configured. To add it later, re-run with:"
    echo "    sudo bash scripts/install-container.sh \\"
    echo "      --jellyfin-url http://<jellyfin-host>:8096 \\"
    echo "      --jellyfin-token <api-key>"
    echo ""
fi

echo "  To update after a git pull:"
echo "    $VENV/bin/pip install -e $REPO_DIR && systemctl restart rushes-web rushes-watch"
echo ""
