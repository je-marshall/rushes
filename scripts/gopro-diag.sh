#!/usr/bin/env bash
# GoPro connection diagnostics — run on the Proxmox HOST.
# Tells you, without a garage trip, exactly where a GoPro is in the chain:
# USB enumerated? net interface present? which namespace? recent kernel events?
#
#   bash scripts/gopro-diag.sh

set -uo pipefail

hr() { printf '=== %s ===\n' "$1"; }

hr "USB device (GoPro vendor 2672)"
if lsusb | grep -i 2672; then
    :
else
    echo "  NOT PRESENT — camera is powered off or physically unplugged."
    echo "  (Software re-plug can't help; the device must be woken physically.)"
fi
echo

hr "sysfs USB device path(s)"
for d in /sys/bus/usb/devices/*/idVendor; do
    [ -r "$d" ] || continue
    [ "$(cat "$d" 2>/dev/null)" = "2672" ] && echo "  ${d%/idVendor}"
done
echo

hr "Net interfaces in HOST namespace"
ip -br link
echo

hr "Net interfaces that belong to a GoPro (by USB vendor)"
found_iface=0
for iface in /sys/class/net/*; do
    name=$(basename "$iface")
    [ "$name" = "lo" ] && continue
    devlink=$(readlink -f "$iface/device" 2>/dev/null) || continue
    [ -n "$devlink" ] || continue
    vid=$(cat "$devlink/../idVendor" 2>/dev/null || true)
    if [ "$vid" = "2672" ]; then
        echo "  $name  (GoPro)"
        found_iface=1
    fi
done
[ "$found_iface" = 0 ] && echo "  none in host namespace (may already be inside the container)"
echo

hr "Recent kernel events (cdc_ncm / enx / usb)"
dmesg 2>/dev/null | grep -iE "cdc_ncm|enx[0-9a-f]|gopro" | tail -20 \
    || echo "  (run as root to read dmesg)"
echo

hr "gopro-connect log (today)"
journalctl -t gopro-connect --since today --no-pager 2>/dev/null | tail -40 \
    || echo "  (no journal access)"
