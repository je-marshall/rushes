#!/usr/bin/env bash
# Run on the Proxmox HOST to install the udev rule and trigger script.
# Can be called from anywhere.
# Usage: sudo bash /path/to/rushes/scripts/install-host.sh [CTID]
#
# CTID defaults to 100; pass a different value if your container has another ID.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

CTID="${1:-100}"

echo "Installing with container ID: $CTID"

# Inject the container ID into the trigger script
sed "s/RUSHES_CTID:-100/RUSHES_CTID:-${CTID}/" "$SCRIPT_DIR/gopro-connect.sh" \
    > /usr/local/bin/gopro-connect.sh
chmod +x /usr/local/bin/gopro-connect.sh

install -m 644 "$REPO_DIR/udev/99-gopro.rules" /etc/udev/rules.d/99-gopro.rules

udevadm control --reload-rules
udevadm trigger

echo "Done. Plug in a GoPro to test — check: journalctl -t gopro-connect -f"
