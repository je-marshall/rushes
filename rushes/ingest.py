import argparse
import asyncio
import hashlib
import os
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from . import cameras, db, gopro, netsetup, recorded, settings, thumbs


async def finalize_clip(conn, camera_row, dest: Path, size: int,
                        checksum: str, recorded_at: str | None) -> None:
    """Thumbnail + probe + DB insert for a file already sitting at `dest`.
    Shared by live download and bulk import. Insert is idempotent (checksum
    and ingest_path are UNIQUE)."""
    thumb_path = await thumbs.generate(dest)
    duration   = _probe_duration(dest)
    conn.execute(
        """
        INSERT OR IGNORE INTO clips
            (filename, ingest_path, recorded_at, ingested_at, camera_id,
             camera_serial, camera_model, duration_secs, size_bytes, checksum,
             thumbnail_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dest.name, str(dest), recorded_at, datetime.now().isoformat(),
            camera_row["id"], camera_row["serial"], camera_row["model"],
            duration, size, checksum,
            str(thumb_path) if thumb_path else None,
        ),
    )
    conn.commit()


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

    recorded_at = recorded.pick(recorded.from_unix(mf.created))
    await finalize_clip(conn, camera_row, dest, mf.size, sha.hexdigest(), recorded_at)
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


async def pull_all(conn, client: httpx.AsyncClient, serial: str, model: str) -> tuple[int, int]:
    """
    Pull every not-yet-downloaded MP4 for this camera into its unsorted folder.
    Returns (files_on_camera, files_pulled_this_call). Reusable by the CLI and
    the watch daemon; safe to call repeatedly (existing files are skipped).
    """
    camera_row  = cameras.upsert(conn, serial, model)
    camera_id   = camera_row["id"]
    media_files = await gopro.get_media_list(client)

    def _dest_for(mf) -> Path:
        # Resolve the folder per file from the current DB state, so a camera
        # rename mid-pull sends subsequent files to the new folder.
        cam = cameras.get(conn, camera_id) or camera_row
        return settings.unsorted_dir(conn) / cameras.camera_slug(cam) / mf.filename

    tasks = [(mf, _dest_for(mf)) for mf in media_files if not _dest_for(mf).exists()]

    sem    = asyncio.Semaphore(2)
    pulled = 0

    async def pull_with_sem(mf, dest):
        nonlocal pulled
        async with sem:
            try:
                await _pull_file(client, mf, dest, conn, camera_row)
                pulled += 1
            except Exception as exc:
                print(f"  ERROR {mf.filename}: {exc}", flush=True)
                dest.unlink(missing_ok=True)
                dest.with_name(dest.name + ".part").unlink(missing_ok=True)

    await asyncio.gather(*[pull_with_sem(mf, dest) for mf, dest in tasks])
    return len(media_files), pulled


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
            try:
                found, pulled = await pull_all(conn, client, serial, model)
                print(f"Ingest complete: {model} ({serial}) — {pulled} new / {found} on camera", flush=True)
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
