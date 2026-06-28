#!/usr/bin/env bash
# Runs on the Proxmox HOST, called by udev when a GoPro CDC-ECM interface appears.
# Moves the interface into the LXC container's network namespace, then triggers ingest.
#
# Install:
#   cp scripts/gopro-connect.sh /usr/local/bin/gopro-connect.sh
#   chmod +x /usr/local/bin/gopro-connect.sh

set -euo pipefail

INTERFACE="${1:?usage: gopro-connect.sh <interface>}"
CTID="${RUSHES_CTID:-100}"

log() { echo "[gopro-connect] $*" | systemd-cat -t gopro-connect -p info; }

# lxc-info -p -H returns the host PID of the container's init process.
# We need this PID to reference the container's network namespace.
CT_PID=$(lxc-info -n "$CTID" -p -H 2>/dev/null || true)

if [[ -z "$CT_PID" || "$CT_PID" -le 0 ]]; then
    log "ERROR: container $CTID is not running or lxc-info failed"
    exit 1
fi

log "moving $INTERFACE → container $CTID (host pid $CT_PID)"
ip link set "$INTERFACE" netns "$CT_PID"

# pct exec returns immediately; ingest runs in the background inside the container.
# Multiple cameras trigger multiple concurrent pct exec calls — all parallel.
log "triggering ingest for $INTERFACE inside container $CTID"
pct exec "$CTID" -- rushes-ingest --interface "$INTERFACE" &

log "done — ingest running in background"
