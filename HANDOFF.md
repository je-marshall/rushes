# Rushes — handoff / TODO (updated 2026-07-21)

Untracked notes. The core system is DONE and working end to end. What's below is
polish + features, none of it urgent.

## Status: WORKING ✅
Full plug-and-forget loop confirmed on 2026-07-21:
plug in → host udev (`gopro-connect.sh`) moves the interface into container 302
→ `rushes-watch` daemon detects it, bounded-DHCP brings it up, waits for the
camera to be ready, sets Auto Power Down=Never + keep-alive, and pulls footage
to `/data/footage/unsorted/<camera>/` on ZFS. Retries through camera boot/sleep;
survives udev flakiness. Logs: `journalctl -fu rushes-watch`.

## Key facts (for whoever picks this up)
- Host = `esn`; container 302 = `dhrushes`, UNPRIVILEGED (uid_map 0→100000).
- Storage: ZFS at `/data` (mp0), posixacl + `setfacl u:100000:rwX`. `RUSHES_DATA=/data`
  → DB at `/data/rushes.db`, footage under `/data/footage/{unsorted,events}`.
  Footage dir is editable at web Settings; clips store absolute paths.
- Camera: HERO10, serial GP25995694, USB vendor 2672. USB API at the DHCP-derived
  `.51` (e.g. 172.26.194.51), NOT 10.5.5.9 (that's WiFi).
- Services (container): `rushes-web` (UI :8765), `rushes-watch` (ingest daemon).
  Both `systemctl`, PYTHONUNBUFFERED=1. Update after pull:
  `/opt/rushes-venv/bin/pip install -e /root/rushes && systemctl restart rushes-web rushes-watch`
- Open GoPro endpoints verified (H10/H13): `wired_usb?p=1`, `keep_alive`,
  `setting?setting=59&option=0` (APO=Never), `camera/state` (status 30=serial,
  8=busy, 10=encoding), `media/list`, download `GET /videos/DCIM/<dir>/<file>`.
  `camera/info` returns `{}` on H10 — use `state`.
- Host tools: `scripts/gopro-diag.sh` (state), `scripts/gopro-reset.sh` (software
  re-plug via USB unbind/bind — wakes a camera without a physical trip).

## TODO — pick up after holiday

### 0. BUG: renaming a camera mid-ingest corrupts paths
Reported 2026-07-21. Renaming a camera in the UI while its footage is still
importing broke things (folder moved under the active download). Worked around
by restarting `rushes-watch` + replugging.

Cause: `cameras.rename()` does `shutil.move(unsorted/<old_slug> → <new_slug>)`
and rewrites `ingest_path`, but the daemon's `pull_all` has already computed
`dest_dir = unsorted/<old_slug>` and is streaming into it — so files are written
to a path that just got moved away, and the DB path rewrite races the insert.

Recommended fix (clean): **key the unsorted folder by the immutable serial, not
the mutable slug.** Change `cameras.camera_slug()` usage so `unsorted/` always
uses `serial` (never moves), and drop the folder-move + path-rewrite from
`cameras.rename()` — rename then only updates the display name/slug. Event
folders (`events/<event-slug>/<camera>`) can keep the friendly name since event
assignment isn't concurrent with ingest. This removes the race entirely.

Alternative (guard): have the daemon mark a camera "ingesting" (a column on
`cameras` or a small table) and have the rename endpoint refuse/defer while
that camera is busy. More moving parts than the serial-keyed approach.


### 1. Jellyfin libraries (last piece of the original vision)
- Add two "Home Videos" libraries pointing at `/data/footage/unsorted` and
  `/data/footage/events` (Jellyfin must have `/data` mounted too, or share the
  dataset).
- Wire auto-rescan: `rushes/jellyfin.py` already has the rescan hook; set
  `JELLYFIN_URL` + `JELLYFIN_TOKEN` (Jellyfin → Administration → API Keys → +),
  e.g. re-run `install-container.sh --jellyfin-url http://<host>:8096 --jellyfin-token <key>`.
- Confirm event assignment (which moves files into `events/<slug>/<camera>/`)
  triggers a refresh so the TV updates.

### DONE since the holiday list
- 3b bulk import (web + CLI, recursive, .THM/.LRV/.JPG filtered, idempotent,
  cancellable, queued sequentially); recorded_at with mtime/plausibility fallback.
- bug #0: per-file folder resolution + canonical serials (last-8) so USB
  (GP25478328) and file (C3531325478328) map to one camera.
- #2 live-updating grid + hover-play + YouTube-style selection + video playback
  (GET /clip/{id}/video, Range-enabled).

### 2c. Playback proxy for HEVC (NEW, follow-up to playback)
Browsers can't play GoPro HEVC/H.265 clips natively. Add a transcoded/streamed
H.264 proxy for the player (on-the-fly ffmpeg, or pre-generated low-res proxy —
the .LRV files we currently skip are exactly this and could be repurposed).

### 2. Live-updating web UI (NEW) — DONE
As clips ingest, the Unsorted grid should update without a manual refresh.
- Ingest is a separate process writing to the DB, so the web app can't push
  directly — simplest is a lightweight JSON endpoint (e.g. `GET /api/clips?...`
  returning the current unsorted clips) that the page polls every ~5s and diffs
  into the grid. Or an SSE endpoint (`/events/stream`) where the web app polls
  the DB and pushes new-clip events; nicer UX, a bit more plumbing.
- Recommend starting with polling — trivial and robust. New clips appear as
  cards fade in; count in the header updates.

### 3. Per-camera toggle on the Unsorted page (NEW)
- Add a camera filter to the `/` route: `camera_id: int | None`. The `clips`
  table already has `camera_id`, and the index query already LEFT JOINs cameras.
- UI: a row of pill toggles at the top of Unsorted — "All" + one per camera
  (reuse the nav pill style). Selecting one filters the grid; combine with the
  existing favourite/flagged filters.
- Cameras list for the toggles: `SELECT id, COALESCE(name, serial) FROM cameras`.

### 3b. Bulk-import existing GoPro files (NEW)
Import a directory of old .MP4s as clips, attributed to the right camera.
- GoPro files embed the camera serial: GPMF `CASN`, surfaced by exiftool as
  `CameraSerialNumber` (verify tag on a real file — varies by firmware). Should
  match the API serial (status 30, e.g. GP25995694), so imports land under the
  same camera as live ingest.
- Also extract `CreateDate` → populate `clips.recorded_at` (live ingest could do
  the same from the media-list `cre` field — currently left NULL; nice-to-have).
- New CLI `rushes-import <dir>`: for each MP4 → read serial+date (exiftool) →
  `cameras.upsert(serial, model)` → checksum → copy into `unsorted/<serial>/`
  (see bug #0: key unsorted by serial) → thumbnail → INSERT. The `checksum`
  UNIQUE constraint makes re-runs idempotent (dupes skipped).
- Add `exiftool` to the apt deps in `install-container.sh` if used (else parse
  GPMF via a lib). Copy, don't move, the originals.

### 3c. Import GoPro photos (NEW, optional)
Bulk import currently handles video (.MP4) only; .JPG/.GPR photos are skipped.
Supporting photos means extending the model: no duration, the photo *is* its own
thumbnail (downscale rather than extract-a-frame), and playback/display differs
from clips. Doable but a deliberate data-model change — design before building.

### 3d. Timestamp-fix tool (NEW)
recorded_at now uses recording metadata only (no mtime guess); missing/implausible
dates are left NULL ("Undated"). Cameras with a wrong clock (e.g. this Hero 10
reports 2016) still get wrong-but-old dates. Build a tool to bulk-correct dates:
e.g. set/offset a camera's clip dates, or infer from filename/order/import batch.
Note: re-importing existing files won't update dates (checksum-dedup skips them),
so the fix tool must UPDATE in place.

### 4. Cold plug-and-forget test
The 2026-07-21 success used the interface already in the container. Do one real
unplug → wait → replug to confirm the whole chain fires from a fresh USB
enumeration (not just `gopro-reset`).

### 5. Tidy
- Delete this file once 1–4 are done.
- Confirm ingested clips appear in the web UI at http://10.1.0.160:8765 (proves
  DB/web/ingest all agree on `/data`).
