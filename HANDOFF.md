# Rushes — WIP handoff (2026-07-20 evening)

Untracked scratch notes. Not committed. Delete when Track 2 lands.

## TL;DR
The ingest pipeline is PROVEN to work end-to-end when the camera is awake
(connects to 172.26.194.51, lists media, pulls real files). The remaining
problem is entirely about **camera readiness/timing + no retry** — the camera
sleeps and the one-shot udev-triggered ingest fires at the wrong moment. That's
what Track 2 (a self-healing watcher daemon) is for.

## What works now (committed + deployed)
- USB → container interface move: host udev rule → `gopro-connect.sh`. The
  by-vendor-ID fallback works (tonight: "found GoPro interface: enx2474f78b43ad
  (was eth0)"). Interface reliably lands in container 302.
- Ingest launched as a transient unit via `systemd-run` (detaches from udev,
  logs to journal).
- Ingest pipeline (validated by hand earlier tonight — files were pulling):
  - Camera IP discovered as `.51` of the DHCP-assigned /24 (NOT 10.5.5.9 — that's
    the WiFi AP address; USB uses a per-serial 172.2x.1xx.0/24).
  - `wired_usb?p=1` enable (best-effort; Hero 10 returns 500 if already on).
  - Identify via `/gopro/camera/state` (status 30 = serial; info returns {} on H10).
  - keep-alive loop (3s) + auto-power-off=Never (setting 59 = 0) during ingest.
  - Resume-safe: downloads to `.part`, atomic rename on success.
- Storage: `/data` ZFS mount into the (UNPRIVILEGED) container. UID map 0→100000,
  posixacl enabled, `setfacl u:100000:rwX` (access + default). `RUSHES_DATA=/data`
  baked into both the `rushes-ingest` wrapper and the systemd service; DB at
  `/data/rushes.db`.
- Editable footage dir via web Settings page (stored in DB `settings` table;
  ingest reads it from the shared DB). Clips store absolute paths so the root
  can move without migration.

## Environment / facts
- Host = `esn`. Container 302 = `dhrushes`, UNPRIVILEGED (uid_map `0 100000 65536`).
- Camera: HERO10 Black, serial `GP25995694`, USB vendor `2672`, sysfs
  `/sys/bus/usb/devices/5-2` on host. USB API at `172.26.194.51:8080`, host `.52`.
- Repo: container `/root/rushes`; host path used for install scripts:
  `/evs/subvol-302-disk-0/root/rushes` (verify — may differ now it's unprivileged).
- Web UI: http://10.1.0.160:8765
- Verified Open GoPro endpoints (Hero 10 & 13):
  - `GET /gopro/camera/control/wired_usb?p=1`
  - `GET /gopro/camera/keep_alive` (every 3s)
  - `GET /gopro/camera/setting?setting=59&option=0` (Auto Power Down = Never)
  - `GET /gopro/camera/state` → status 8=busy, 10=encoding, 30=serial
  - `GET /gopro/media/list`; download `GET /videos/DCIM/<dir>/<file>`
  - `/gopro/camera/info` returns `{}` on Hero 10 — do not rely on it.

## Bugs found tonight (fix in Track 2 or before)
1. **Readiness race (root cause).** Auto-ingest fires ~1s after the interface
   moves, but after a plug-in / `gopro-reset` the camera takes seconds to boot
   its network. DHCP/API not ready → ingest fails, no folders created. Manual
   ingest works only because the camera had been awake a while.
2. **dhclient hangs when camera asleep.** No DHCP server answering → `dhclient -1`
   blocks. Need a hard timeout (e.g. `timeout 20 dhclient ...`) + fail fast + retry.
3. **Output buffering.** `netsetup`/ingest `print()`s are buffered when stdout is
   not a TTY (i.e. under systemd), so `journalctl` shows nothing until exit. Fix:
   `PYTHONUNBUFFERED=1` in the wrapper, or `flush=True` everywhere, or real logging.
4. **systemd-run unit-name collision.** Last error: "could not start
   rushes-ingest-enx2474f78b43ad (already ingesting this camera?)". A previous
   unit with that name still exists (the earlier hung/failed manual run is very
   likely STILL RUNNING and holding the interface — that also explains why the
   new DHCP hangs). `--collect` doesn't free a still-active/failed unit.

## FIRST THING TOMORROW — clean up the stuck state
```bash
# in container 302
systemctl stop rushes-ingest-enx2474f78b43ad 2>/dev/null
systemctl reset-failed rushes-ingest-enx2474f78b43ad 2>/dev/null
ps aux | grep -E 'rushes-ingest|dhclient' | grep -v grep   # kill leftovers
pkill -f rushes-ingest; pkill dhclient
ip -br addr show enx2474f78b43ad                            # expect no 172.26.x until re-ingest
```
The hung manual `rushes-ingest` from tonight is probably still alive holding the
interface — that single fact explains bugs #2 and #4 at once.

## NEXT: Track 2 — self-healing watcher daemon (`rushes-watch`)
Design agreed:
- Long-running systemd service in the container.
- Watches the container netns for GoPro interfaces appearing (poll
  `/sys/class/net` every ~2s, or `ip monitor link`). Ignore `lo` / `eth0` veth.
- Per interface, a retry-with-backoff worker:
  1. `bring_up` with BOUNDED dhclient (timeout, no hang), retry until it gets a
     172.26.x lease (camera waking).
  2. enable wired_usb (best-effort), poll `/gopro/camera/state` until ready
     (status 8==0 && 10==0).
  3. set auto-power-off=Never, start keep-alive → camera never sleeps again after
     the first good connection (solves the race at the source).
  4. ingest; optionally keep polling media list for NEW clips while connected.
  5. on interface disappear, stop the worker cleanly.
- Handle multiple cameras concurrently (one worker each).
- **Change udev path:** `gopro-connect.sh` should ONLY move the interface into
  the container; DROP the `systemd-run rushes-ingest` call. The daemon owns
  ingest → no more unit-name collisions, and retries mean udev timing no longer
  matters.
- Fold in fixes #2 (bounded DHCP), #3 (unbuffered logging to journal), #4 (daemon
  owns single-flight per interface).

## Also outstanding (later)
- Jellyfin libraries pointing at `/data/footage/unsorted` and `/data/footage/events`.
- Confirm host repo path for install scripts now the container is unprivileged.
- Decide whether to keep camera awake 24/7 (keep-alive) or let it sleep and rely
  on re-enumeration + daemon retry. Tradeoff: battery/wear vs. instant capture.
