#!/usr/bin/env bash
# Runs on the Proxmox HOST when udev detects a GoPro CDC-ECM interface.
# Moves the interface into the LXC container's network namespace, then triggers ingest.
#
# Install:
#   sudo bash scripts/install-host.sh [CTID]

set -uo pipefail  # no -e so we can log errors explicitly

INTERFACE="${1:?usage: gopro-connect.sh <interface>}"
CTID="${RUSHES_CTID:-100}"

log() { echo "[gopro-connect] $*" | systemd-cat -t gopro-connect -p info; }

CT_PID=$(lxc-info -n "$CTID" -p -H 2>/dev/null || true)
if [[ -z "$CT_PID" || "$CT_PID" -le 0 ]]; then
    log "ERROR: container $CTID is not running or lxc-info failed"
    exit 1
fi

# The interface may have been renamed by udev (eth0 → enx...) between the
# event firing and this script running. If the given name is gone, search
# for the GoPro interface by walking each net device's USB parent to idVendor.
if ! ip link show "$INTERFACE" &>/dev/null; then
    log "$INTERFACE not found — searching for GoPro interface by USB vendor ID"
    for path in /sys/class/net/*; do
        iface=$(basename "$path")
        [[ "$iface" == "lo" ]] && continue
        # /sys/class/net/<iface>/device → the USB interface (X-Y:A.B);
        # its parent (X-Y) carries idVendor.
        devlink=$(readlink -f "$path/device" 2>/dev/null) || continue
        [[ -n "$devlink" ]] || continue
        vid=$(cat "$devlink/../idVendor" 2>/dev/null || true)
        if [[ "$vid" == "2672" ]]; then
            log "found GoPro interface: $iface (was $INTERFACE)"
            INTERFACE="$iface"
            break
        fi
    done
fi

if ! ip link show "$INTERFACE" &>/dev/null; then
    log "ERROR: cannot find GoPro network interface (vendor 2672) on host"
    exit 1
fi

log "moving $INTERFACE → container $CTID (host pid $CT_PID)"
if ! ip link set "$INTERFACE" netns "$CT_PID"; then
    log "ERROR: ip link set $INTERFACE netns $CT_PID failed"
    exit 1
fi

# That's all the host does. The rushes-watch daemon inside the container detects
# the interface, waits for the camera to be ready, and ingests — with retries —
# so we no longer race the camera's boot time or risk udev killing a download.
log "moved — rushes-watch in container $CTID will pick it up (tail: pct exec $CTID -- journalctl -fu rushes-watch)"
