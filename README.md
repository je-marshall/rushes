# Rushes

Plug in a GoPro, footage gets ingested, catalogued, and browsable on the TV.

- Auto-detects GoPro cameras over USB-C (Open GoPro HTTP API)
- Pulls footage in parallel — plug in multiple cameras at once
- Organises clips into named events via a web UI
- Integrates with Jellyfin for TV playback

## Requirements

**On the Proxmox host:**
- `lxc-utils` (for `lxc-info`)
- udev

**In the LXC container:**
- Python 3.11+
- `ffmpeg` and `ffprobe`
- `isc-dhcp-client` (`dhclient`)

```
apt install python3 python3-pip ffmpeg isc-dhcp-client
pip install -e .
```

## Configuration

All paths default to `/var/lib/rushes`. Override with the `RUSHES_DATA` environment variable.

Jellyfin integration is optional. Set these in the environment (or in the systemd service file):

```
JELLYFIN_URL=http://localhost:8096
JELLYFIN_TOKEN=your-api-key-from-jellyfin-dashboard
```

## Setup

### 1. Install the host-side udev rule

From the repo root, on the **Proxmox host**:

```bash
sudo bash scripts/install-host.sh 100   # replace 100 with your container ID
```

This installs `udev/99-gopro.rules` and `scripts/gopro-connect.sh`. When a GoPro is plugged in, the host moves its CDC-ECM network interface into the container's network namespace and triggers ingestion.

See `docs/architecture.md` for why this works without USB passthrough.

### 2. Start the web service

In the **container**:

```bash
cp systemd/rushes-web.service /etc/systemd/system/
systemctl enable --now rushes-web
```

Web UI is available at `http://<container-ip>:8765`.

### 3. Set up Jellyfin libraries

Point two Jellyfin libraries at the footage directories:

| Library name | Path |
|---|---|
| Unsorted Rushes | `/var/lib/rushes/footage/unsorted` |
| Events | `/var/lib/rushes/footage/events` |

Use library type **Home Videos** so Jellyfin doesn't try to match clips against external metadata.

## Usage

1. **Plug in a GoPro** — ingest starts automatically. Footage lands in `unsorted/<camera-slug>/`.
2. **Name your cameras** — go to `/cameras` in the web UI and give each camera a friendly name (e.g. "Jon's Hero 10"). This renames the folder and updates all paths.
3. **Create an event** — go to `/events`, create a named group (e.g. "Pembrokeshire beach, Aug 2025").
4. **Assign clips** — on the `/` (Unsorted) view, click clips to select them, then use the assign bar to move them into an event. Files physically move to `events/<event-slug>/<camera-slug>/`.
5. **Watch on TV** — Jellyfin's Events library reflects the folder structure. Favourite clips in Jellyfin natively.

## Project layout

```
rushes/              Python package
  config.py          Paths and environment config
  db.py              SQLite schema and connection
  cameras.py         Camera registry and rename logic
  events.py          Event creation and clip assignment (moves files)
  ingest.py          Per-camera ingest run
  gopro.py           Open GoPro HTTP API client
  netsetup.py        USB CDC-ECM interface setup (DHCP + policy routing)
  jellyfin.py        Jellyfin library rescan trigger
  slug.py            URL/folder slug utility
  thumbs.py          ffmpeg thumbnail generation
  web/app.py         FastAPI web UI
  web/templates/     Jinja2 HTML templates

scripts/
  gopro-connect.sh   Host-side: move interface to container, trigger ingest
  install-host.sh    Install udev rule and trigger script on Proxmox host

udev/
  99-gopro.rules     Host udev rule (GoPro CDC-ECM interface detection)

systemd/
  rushes-web.service Systemd service for the web UI (runs in container)
```
