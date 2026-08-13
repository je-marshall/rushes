import argparse
import asyncio
import hashlib
import os
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from . import cameras, config, db, gopro, jellyfin, netsetup, recorded, settings, thumbs


def finalize_clip(conn, camera_row, dest: Path, size: int, checksum: str,
                  recorded_at: str | None, thumb_path: Path | None,
                  proxy_path: Path | None) -> None:
    """Probe + DB insert for a file already at `dest`, with a resolved thumbnail
    and optional proxy. Shared by live download and bulk import. Idempotent
    (checksum and ingest_path are UNIQUE)."""
    duration = _probe_duration(dest)
    conn.execute(
        """
        INSERT OR IGNORE INTO clips
            (filename, ingest_path, recorded_at, ingested_at, camera_id,
             camera_serial, camera_model, duration_secs, size_bytes, checksum,
             thumbnail_path, proxy_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dest.name, str(dest), recorded_at, datetime.now().isoformat(),
            camera_row["id"], camera_row["serial"], camera_row["model"],
            duration, size, checksum,
            str(thumb_path) if thumb_path else None,
            str(proxy_path) if proxy_path else None,
        ),
    )
    conn.commit()


async def _download_to(client: httpx.AsyncClient, url: str, path: Path) -> None:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with open(path, "wb") as fh:
            async for chunk in resp.aiter_bytes(65536):
                fh.write(chunk)


async def _backfill_live(conn, client: httpx.AsyncClient, mf: gopro.MediaFile, clip) -> bool:
    """For a clip already in the DB, pull only its .LRV proxy / .THM thumbnail
    off the camera if we're missing them — never re-download the MP4."""
    checksum = clip["checksum"]
    changed  = False

    have_proxy = clip["proxy_path"] and Path(clip["proxy_path"]).exists()
    if not have_proxy and mf.lrv_path:
        p = config.PROXY_DIR / f"{checksum}.mp4"
        try:
            print(f"  proxy {mf.filename} ...", flush=True)
            await _download_to(client, mf.lrv_path, p)
            conn.execute("UPDATE clips SET proxy_path = ? WHERE id = ?", (str(p), clip["id"]))
            conn.commit(); changed = True
            print(f"  done  {mf.filename} (proxy, {p.stat().st_size // 1024 // 1024} MB)", flush=True)
        except Exception:
            p.unlink(missing_ok=True)  # no .LRV for this clip / fetch failed

    have_thumb = clip["thumbnail_path"] and Path(clip["thumbnail_path"]).exists()
    if not have_thumb and mf.thm_path:
        p = config.THUMB_DIR / f"{checksum}.jpg"
        try:
            await _download_to(client, mf.thm_path, p)
            conn.execute("UPDATE clips SET thumbnail_path = ? WHERE id = ?", (str(p), clip["id"]))
            conn.commit(); changed = True
            print(f"  thumb {mf.filename}", flush=True)
        except Exception:
            p.unlink(missing_ok=True)

    return changed


async def _pull_file(
    client:   httpx.AsyncClient,
    mf:       gopro.MediaFile,
    dest:     Path,
    conn,
    camera_row,
) -> None:
    mb = mf.size // 1024 // 1024
    print(f"  pull  {mf.filename} ({mb} MB)...", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Download to a .part file and atomically rename on success. A killed or
    # failed download therefore never leaves a partial file at the final path
    # that a later run would mistake for a completed clip.
    part = dest.with_name(dest.name + ".part")
    sha = hashlib.sha256()
    async with client.stream("GET", mf.download_path) as resp:
        resp.raise_for_status()
        with open(part, "wb") as fh:
            async for chunk in resp.aiter_bytes(65536):
                fh.write(chunk)
                sha.update(chunk)
    os.replace(part, dest)
    checksum = sha.hexdigest()

    # Thumbnail: prefer the camera's own .THM (no decode); else extract a frame.
    thumb_path = None
    if mf.thm_path:
        p = config.THUMB_DIR / f"{checksum}.jpg"
        try:
            await _download_to(client, mf.thm_path, p)
            thumb_path = p
        except Exception:
            thumb_path = None
    if not thumb_path:
        thumb_path = await thumbs.generate(dest, checksum)

    # Proxy: pull the camera's low-res .LRV (H.264) for browser playback.
    proxy_path = None
    if mf.lrv_path:
        p = config.PROXY_DIR / f"{checksum}.mp4"
        try:
            await _download_to(client, mf.lrv_path, p)
            proxy_path = p
        except Exception:
            proxy_path = None

    recorded_at = recorded.pick(recorded.from_unix(mf.created))
    finalize_clip(conn, camera_row, dest, mf.size, checksum, recorded_at, thumb_path, proxy_path)
    print(f"  done  {mf.filename}", flush=True)


async def keep_alive_loop(client: httpx.AsyncClient) -> None:
    """Ping the camera every few seconds so it can't sleep."""
    while True:
        await asyncio.sleep(gopro.KEEP_ALIVE_SECS)
        try:
            await gopro.keep_alive(client)
        except Exception:
            pass  # a missed keep-alive isn't fatal; real errors surface elsewhere


async def connect(client: httpx.AsyncClient) -> dict:
    """
    Bring the camera to a usable state and return its state dict.
    Raises httpx.HTTPError if the camera can't be reached.
    Enabling wired control is best-effort (Hero 10 500s if already on); the
    get_state() call is the real reachability check.
    """
    try:
        await gopro.enable_wired_usb(client)
    except httpx.HTTPError:
        pass
    return await gopro.get_state(client)


async def pull_all(conn, client: httpx.AsyncClient, serial: str, model: str,
                   on_progress=None) -> tuple[int, int, int]:
    """
    Sync this camera: pull new MP4s, and backfill the .LRV proxy / .THM thumbnail
    for clips we already have but are missing them (small downloads only, no
    re-fetch of the MP4). Returns (files_on_camera, pulled, updated). Safe to
    call repeatedly. on_progress(done, total, pulled, updated) fires per file.
    """
    camera_row  = cameras.upsert(conn, serial, model)
    camera_id   = camera_row["id"]
    media_files = await gopro.get_media_list(client)
    total       = len(media_files)

    def _dest_for(mf) -> Path:
        # Resolve the folder per file from the current DB state, so a camera
        # rename mid-pull sends subsequent files to the new folder.
        cam = cameras.get(conn, camera_id) or camera_row
        return settings.unsorted_dir(conn) / cameras.camera_slug(cam) / mf.filename

    sem     = asyncio.Semaphore(2)
    pulled  = 0
    updated = 0
    done    = 0

    async def handle(mf):
        nonlocal pulled, updated, done
        try:
            # Already ingested? (match by camera + filename — don't re-download
            # to get a checksum). Backfill missing sidecars instead.
            clip = conn.execute(
                "SELECT * FROM clips WHERE camera_id = ? AND filename = ?",
                (camera_id, mf.filename),
            ).fetchone()
            if clip:
                async with sem:
                    if await _backfill_live(conn, client, mf, clip):
                        updated += 1
                return
            dest = _dest_for(mf)
            if dest.exists():
                return  # file on disk without a DB row — leave it
            async with sem:
                try:
                    await _pull_file(client, mf, dest, conn, camera_row)
                    pulled += 1
                except Exception as exc:
                    print(f"  ERROR {mf.filename}: {exc}", flush=True)
                    dest.unlink(missing_ok=True)
                    dest.with_name(dest.name + ".part").unlink(missing_ok=True)
        finally:
            done += 1
            if on_progress:
                on_progress(done, total, pulled, updated)

    await asyncio.gather(*[handle(mf) for mf in media_files])
    return total, pulled, updated


async def run(interface: str | None = None, serial_hint: str | None = None) -> None:
    conn = db.connect()
    db.init_db(conn)

    ctx = netsetup.managed_interface(interface) if interface else _null_ctx()

    with ctx as netinfo:
        local_ip, camera_ip = netinfo if netinfo else (None, gopro.DEFAULT_CAMERA_IP)

        async with gopro.make_client(camera_ip, local_address=local_ip) as client:
            try:
                state = await connect(client)
            except httpx.HTTPError as exc:
                print(f"GoPro API unreachable at {camera_ip} on {interface or 'default route'}: {exc}", flush=True)
                return

            serial, model = gopro.identify(state)
            serial = serial or serial_hint or "unknown"
            print(f"Connected: {model} ({serial}) at {camera_ip}", flush=True)

            try:
                await gopro.set_auto_power_off_never(client)
            except httpx.HTTPError:
                pass
            keeper = asyncio.create_task(keep_alive_loop(client))

            def _progress(done, total, pulled, updated):
                if (pulled + updated) and done % 10 == 0:
                    print(f"  {done}/{total} ({pulled} new, {updated} updated)...", flush=True)

            try:
                found, pulled, updated = await pull_all(conn, client, serial, model, on_progress=_progress)
                print(f"Ingest complete: {model} ({serial}) — {pulled} new / {updated} updated / {found} on camera", flush=True)
                if pulled:
                    jellyfin.trigger_rescan()
            finally:
                keeper.cancel()


class _null_ctx:
    def __enter__(self):  return None
    def __exit__(self, *_): pass


def _probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default=None)
    parser.add_argument("--serial",    default=None)
    args = parser.parse_args()
    asyncio.run(run(interface=args.interface, serial_hint=args.serial))
