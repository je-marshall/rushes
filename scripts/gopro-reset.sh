#!/usr/bin/env bash
# Software "re-plug" of a GoPro — run on the Proxmox HOST.
#
# Unbinds and re-binds the GoPro's USB device, forcing a full re-enumeration.
# This wakes a camera whose network interface has been torn down (slept) and
# recreates the CDC-NCM interface, which fires the udev rule again — WITHOUT
# anyone physically touching the camera.
#
# Only works if the USB device is still enumerated (see: lsusb | grep 2672).
# If the camera has fully powered off, USB is gone and only a physical wake
# (or fixing Auto Power Off) will help.
#
#   sudo bash scripts/gopro-reset.sh

set -uo pipefail

found=0
for d in /sys/bus/usb/devices/*/idVendor; do
    [ -r "$d" ] || continue
    [ "$(cat "$d" 2>/dev/null)" = "2672" ] || continue

    dev=$(basename "$(dirname "$d")")
    echo "Found GoPro USB device: $dev"

    echo "  unbinding..."
    if ! echo "$dev" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null; then
        echo "  (unbind failed — need root, or already unbound)"
    fi
    sleep 2
    echo "  re-binding..."
    if ! echo "$dev" > /sys/bus/usb/drivers/usb/bind 2>/dev/null; then
        echo "  (bind failed — need root)"
    fi
    found=1
done

if [ "$found" = 0 ]; then
    echo "No GoPro USB device (vendor 2672) enumerated on this host."
    echo "The camera is off or unplugged — a software re-plug can't reach it."
    exit 1
fi

echo
echo "Re-enumeration triggered. Watch it land:"
echo "  journalctl -t gopro-connect -f"
