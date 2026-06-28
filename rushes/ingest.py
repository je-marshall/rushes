import argparse
import asyncio
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from . import cameras, config, db, gopro, netsetup, thumbs


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

    sha = hashlib.sha256()
    async with client.stream("GET", mf.download_url) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            async for chunk in resp.aiter_bytes(65536):
                fh.write(chunk)
                sha.update(chunk)

    checksum   = sha.hexdigest()
    thumb_path = await thumbs.generate(dest)
    duration   = _probe_duration(dest)

    conn.execute(
        """
        INSERT OR IGNORE INTO clips
            (filename, ingest_path, ingested_at, camera_id, camera_serial,
             camera_model, duration_secs, size_bytes, checksum, thumbnail_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mf.filename, str(dest), datetime.now().isoformat(),
            camera_row["id"], camera_row["serial"], camera_row["model"],
            duration, mf.size, checksum,
            str(thumb_path) if thumb_path else None,
        ),
    )
    conn.commit()
    print(f"  done  {mf.filename}", flush=True)


async def run(interface: str | None = None, serial_hint: str | None = None) -> None:
    conn = db.connect()
    db.init_db(conn)

    ctx = netsetup.managed_interface(interface) if interface else _null_ctx()

    with ctx as local_ip:
        async with gopro.make_client(local_address=local_ip) as client:
            try:
                info = await gopro.get_camera_info(client)
            except httpx.HTTPError as exc:
                print(f"GoPro API unreachable on {interface or 'default route'}: {exc}", flush=True)
                return

            serial = info.get("serial_number") or serial_hint or "unknown"
            model  = info.get("model_name", "GoPro")
            print(f"Connected: {model} ({serial})", flush=True)

            camera_row = cameras.upsert(conn, serial, model)
            cam_slug   = cameras.camera_slug(camera_row)
            dest_dir   = config.UNSORTED_DIR / cam_slug

            media_files = await gopro.get_media_list(client)
            print(f"Found {len(media_files)} MP4 files", flush=True)

            tasks = []
            for mf in media_files:
                dest = dest_dir / mf.filename
                if dest.exists():
                    print(f"  skip  {mf.filename}", flush=True)
                    continue
                tasks.append((mf, dest))

            sem = asyncio.Semaphore(2)

            async def pull_with_sem(mf, dest):
                async with sem:
                    try:
                        await _pull_file(client, mf, dest, conn, camera_row)
                    except Exception as exc:
                        print(f"  ERROR {mf.filename}: {exc}", flush=True)
                        dest.unlink(missing_ok=True)

            await asyncio.gather(*[pull_with_sem(mf, dest) for mf, dest in tasks])
            print(f"Ingest complete: {model} ({serial})", flush=True)


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
