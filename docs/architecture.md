# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────┐
│  Proxmox host                                           │
│                                                         │
│  GoPro ──USB-C──► CDC-ECM driver                        │
│                       │ creates usb0                    │
│  udev (99-gopro.rules)│                                 │
│       └─ gopro-connect.sh                               │
│               │ ip link set usb0 netns <ct-pid>         │
│               │ pct exec 100 -- rushes-ingest &         │
└───────────────┼─────────────────────────────────────────┘
                │ network interface moved into container
┌───────────────▼─────────────────────────────────────────┐
│  LXC container (privileged, CAP_NET_ADMIN)              │
│                                                         │
│  rushes-ingest --interface usb0                         │
│    netsetup.py                                          │
│      dhclient usb0  ──────────────────► GoPro DHCP      │
│      ip route add 10.5.5.9 dev usb0 table 200           │
│      ip rule add from 172.26.x.y lookup 200             │
│                                                         │
│    gopro.py (httpx, local_address=172.26.x.y)           │
│      GET /gopro/camera/info                             │
│      GET /gopro/media/list                              │
│      GET /videos/DCIM/.../*.MP4  (streaming download)   │
│                                                         │
│    thumbs.py  ffmpeg thumbnail                          │
│    db.py      SQLite WAL insert                         │
│                                                         │
│  rushes-web (FastAPI, port 8765)                        │
│    /             unsorted clips, multi-select + assign  │
│    /events       create events, view/unassign clips     │
│    /cameras      name cameras                           │
│                                                         │
│  SQLite (/var/lib/rushes/rushes.db)                     │
│  Footage (/var/lib/rushes/footage/)                     │
└─────────────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────┐
│  Jellyfin                     │
│  library: unsorted/ ──────── TV playback
│  library: events/   ──────── TV playback
└───────────────────────────────┘
```

## Why no USB passthrough

Traditional Proxmox USB passthrough exposes the USB device node (`/dev/bus/usb/...`) inside the container. That's not needed here.

When a GoPro connects via USB-C, the host kernel's CDC-ECM driver creates a virtual ethernet interface (e.g. `usb0`) in the **host's network namespace**. We move that interface directly into the container's network namespace:

```bash
ip link set usb0 netns <container-init-pid>
```

Network namespaces are a Linux kernel feature. The interface vanishes from the host and appears inside the container — no cgroup device permissions, no `/dev/bus/usb` mount required. The container only needs `CAP_NET_ADMIN` to accept the incoming interface, which a privileged LXC container has by default.

This also works with an unprivileged container if you grant `CAP_NET_ADMIN` explicitly.

## Multi-camera parallel ingestion

Each GoPro always presents itself at `10.5.5.9:8080`, which would be a routing conflict if two cameras are connected simultaneously. We resolve this with Linux policy routing:

1. `dhclient` gets a unique local IP from each camera's built-in DHCP server (e.g. `172.26.x.y` and `172.27.x.y`)
2. Each interface gets its own routing table entry: `ip route add 10.5.5.9 dev usb0 table 200`
3. A policy rule ties local IP to table: `ip rule add from 172.26.x.y lookup 200`
4. `httpx.AsyncHTTPTransport(local_address="172.26.x.y")` binds the socket to that IP
5. The kernel selects routing table 200 → traffic goes through `usb0` → correct camera

Each camera runs as a completely independent process. SQLite WAL mode handles concurrent writes from multiple ingest processes safely.

## Folder structure

```
/var/lib/rushes/
├── footage/
│   ├── unsorted/
│   │   ├── jons-hero10/       ← camera slug (name or serial)
│   │   │   └── GX010001.MP4
│   │   └── sarahs-hero13/
│   │       └── GX010001.MP4   ← same filename, no collision
│   └── events/
│       └── 2025-08-pembrokeshire/
│           ├── jons-hero10/
│           └── sarahs-hero13/
├── thumbs/                    ← JPEG thumbnails (640px wide)
└── rushes.db                  ← SQLite database
```

Files are **physically moved** (not symlinked) when assigned to an event. `ingest_path` in the database is updated accordingly. Moving a clip back to Unsorted reverses the move.

Camera slugs are derived from the human-readable name set in the web UI (`/cameras`). If no name is set, the raw serial number is used. Renaming a camera renames the `unsorted/<slug>/` folder atomically and updates all `ingest_path` values in the database.

## Database schema

```sql
cameras   id, serial, name, slug, model, last_seen
events    id, name, slug, description, created_at
clips     id, filename, ingest_path, recorded_at, ingested_at,
          camera_id, camera_serial, camera_model,
          event_id, duration_secs, size_bytes, checksum,
          is_favourite, flagged, thumbnail_path
```

`clips.event_id` is NULL for unsorted clips. A clip belongs to at most one event (matches the filesystem — a file can only be in one folder).

## Jellyfin integration

Jellyfin is pointed at `footage/unsorted/` and `footage/events/` as separate **Home Videos** libraries. When clips are assigned to or removed from events, Rushes calls `POST /Library/Refresh` on the Jellyfin API so libraries update immediately.

Jellyfin integration is entirely optional and fail-safe — if the API is unreachable, ingest and the web UI are unaffected.
