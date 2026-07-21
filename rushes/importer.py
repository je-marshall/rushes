"""
Bulk import of existing GoPro files from a directory the container can see.

Each file is attributed to a camera by its embedded serial (via metadata.py), so
imports land in the same named folder as live-ingested footage from that camera,
and unknown serials create new cameras you can rename later. Idempotent: a file
whose checksum is already in the DB is skipped, so re-running is safe.

Runs as a background job (import_jobs table) driven by the rushes-watch daemon,
and is also exposed as the `rushes-import` CLI for quick testing.
"""

import argparse
import asyncio
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from . import cameras, db, ingest, metadata, recorded, settings

VIDEO_EXTS = {".mp4"}


def _iter_videos(source: Path):
    for p in sorted(source.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.endswith(".part"):
            yield p


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _checksum_exists(conn, checksum: str) -> bool:
    return conn.execute("SELECT 1 FROM clips WHERE checksum = ?", (checksum,)).fetchone() is not None


async def import_file(conn, path: Path) -> str:
    """Import one file. Returns 'imported' or 'skipped'."""
    meta   = metadata.extract(path)
    serial = (meta.serial or "unknown").strip() or "unknown"
    camera = cameras.upsert(conn, serial, meta.model or "GoPro")

    # Dedup by content before copying anything, so re-runs are cheap.
    checksum = await asyncio.to_thread(_sha256, path)
    if _checksum_exists(conn, checksum):
        return "skipped"

    dest_dir = settings.unsorted_dir(conn) / cameras.camera_slug(camera)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():  # same name, different content — disambiguate
        dest = dest_dir / f"{path.stem}_{checksum[:8]}{path.suffix}"

    part = dest.with_name(dest.name + ".part")
    await asyncio.to_thread(shutil.copy2, path, part)   # copy2 preserves mtime
    part.replace(dest)

    recorded_at = recorded.pick(recorded.from_exif(meta.exif_date), recorded.from_mtime(path))
    await ingest.finalize_clip(conn, camera, dest, dest.stat().st_size, checksum, recorded_at)
    return "imported"


async def run_import(conn, source: Path, on_progress=None,
                     is_cancelled=None) -> tuple[int, int, int, bool]:
    files    = list(_iter_videos(source))
    total    = len(files)
    imported = skipped = 0
    cancelled = False
    for i, path in enumerate(files, 1):
        # Stop cleanly at a file boundary — never mid-copy.
        if is_cancelled and is_cancelled():
            cancelled = True
            break
        try:
            if await import_file(conn, path) == "imported":
                imported += 1
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            print(f"  import ERROR {path}: {exc}", flush=True)
        if on_progress:
            on_progress(i, total, imported, skipped)
    return total, imported, skipped, cancelled


# --- background job plumbing (used by rushes-watch) -------------------------

def enqueue(conn, source_path: str):
    conn.execute("INSERT INTO import_jobs (source_path) VALUES (?)", (source_path,))
    conn.commit()


def claim_pending(conn):
    row = conn.execute(
        "SELECT * FROM import_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE import_jobs SET status = 'running', updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), row["id"]),
    )
    conn.commit()
    return row


def request_cancel(conn, job_id: int) -> None:
    """Cancel a pending job immediately, or ask a running job to stop at the
    next file boundary (the daemon's loop notices the 'cancelling' status)."""
    row = conn.execute("SELECT status FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return
    now = datetime.now().isoformat()
    if row["status"] == "pending":
        conn.execute(
            "UPDATE import_jobs SET status='cancelled', message='cancelled before start', updated_at=? WHERE id=?",
            (now, job_id),
        )
    elif row["status"] == "running":
        conn.execute(
            "UPDATE import_jobs SET status='cancelling', updated_at=? WHERE id=?",
            (now, job_id),
        )
    conn.commit()


async def run_job(conn, job) -> None:
    job_id = job["id"]
    source = Path(job["source_path"]).expanduser()
    now    = lambda: datetime.now().isoformat()

    if not source.is_dir():
        conn.execute(
            "UPDATE import_jobs SET status='error', message=?, updated_at=? WHERE id=?",
            (f"Not a directory: {source}", now(), job_id),
        )
        conn.commit()
        return

    def progress(i, total, imported, skipped):
        conn.execute(
            "UPDATE import_jobs SET total=?, processed=?, imported=?, skipped=?, updated_at=? WHERE id=?",
            (total, i, imported, skipped, now(), job_id),
        )
        conn.commit()

    def is_cancelled():
        r = conn.execute("SELECT status FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r) and r["status"] == "cancelling"

    try:
        total, imported, skipped, cancelled = await run_import(conn, source, progress, is_cancelled)
        status  = "cancelled" if cancelled else "done"
        summary = f"{imported} imported, {skipped} skipped of {total}"
        conn.execute(
            "UPDATE import_jobs SET status=?, message=?, updated_at=? WHERE id=?",
            (status, summary + (" (cancelled)" if cancelled else ""), now(), job_id),
        )
        conn.commit()
    except Exception as exc:
        conn.execute(
            "UPDATE import_jobs SET status='error', message=?, updated_at=? WHERE id=?",
            (str(exc), now(), job_id),
        )
        conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-import GoPro files from a directory")
    ap.add_argument("source", help="directory to scan for .MP4 files (recursive)")
    args = ap.parse_args()

    if not metadata.available():
        print("WARNING: exiftool not installed — files will import as 'unknown' camera.", flush=True)

    conn = db.connect()
    db.init_db(conn)

    def prog(i, total, imported, skipped):
        print(f"  [{i}/{total}] imported={imported} skipped={skipped}", flush=True)

    total, imported, skipped, _ = asyncio.run(run_import(conn, Path(args.source), prog))
    print(f"Done: {imported} imported, {skipped} skipped of {total}", flush=True)
