"""
Ensure every clip has a browser-playable (H.264) proxy.

GoPro's .LRV proxy is usually H.264, but on some models/modes it's HEVC — which
browsers can't decode, so those "proxies" don't help the web player. This sweep
finds proxies that aren't H.264 (or are missing/corrupt) and transcodes a real
H.264 proxy, preferring the small .LRV as the source so it's cheap even without
a GPU. Runs incrementally in the background from the watch daemon.
"""

import subprocess
from pathlib import Path

from . import config, db, thumbs


def _probe(path: Path) -> tuple[str | None, int | None]:
    """Return (video codec, height) or (None, None)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,height",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        parts = r.stdout.strip().split(",")
        codec = parts[0] or None
        height = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return codec, height
    except Exception:
        return None, None


def _gen_thumb(src: Path, name: str) -> Path | None:
    """Grab a 640px thumbnail from src, named by `name` (the checksum)."""
    dest = config.THUMB_DIR / f"{name}.jpg"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=640:-1", str(dest)],
            capture_output=True, timeout=60,
        )
        return dest if (r.returncode == 0 and dest.exists()) else None
    except Exception:
        return None


def _transcode_h264(src: Path, dest: Path) -> bool:
    """Transcode src to an H.264 mp4 at dest (capped at 720p). Atomic via .part."""
    _, height = _probe(src)
    vf = ["-vf", "scale=-2:720"] if (height and height > 720) else []
    tmp = dest.with_name(dest.name + ".part.mp4")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             *vf,
             "-map", "0:v:0", "-map", "0:a:0?",   # first video + audio-if-present (skip GPMF)
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             str(tmp)],
            capture_output=True, timeout=3600,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return True
        tmp.unlink(missing_ok=True)
        return False
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def pending() -> tuple[int, int]:
    """(proxies unchecked, thumbnails unchecked) — for startup visibility."""
    conn = db.connect()
    p = conn.execute("SELECT COUNT(*) FROM clips WHERE proxy_ok IS NULL").fetchone()[0]
    t = conn.execute("SELECT COUNT(*) FROM clips WHERE thumb_ok IS NULL").fetchone()[0]
    return p, t


def process_batch(probe_limit: int = 150, max_transcodes: int = 1,
                  max_thumbs: int = 25) -> tuple[int, int, int, int]:
    """One pass over clips whose proxy or thumbnail isn't confirmed good:
    - regenerate legacy/colliding thumbnails keyed by checksum (up to max_thumbs)
    - transcode non-H.264 proxies (up to max_transcodes)
    Cheap confirmations (already-good) are unlimited.
    Returns (checked, thumbs_fixed, transcoded, remaining)."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, ingest_path, proxy_path, thumbnail_path, checksum, proxy_ok, thumb_ok, media_type "
        "FROM clips WHERE proxy_ok IS NULL OR thumb_ok IS NULL LIMIT ?", (probe_limit,)
    ).fetchall()

    transcoded = thumbs_fixed = 0
    for r in rows:
        cs = r["checksum"]
        is_photo = r["media_type"] == "photo"

        # --- thumbnail: must be keyed by checksum (unique per clip) ---
        if r["thumb_ok"] is None:
            tp = r["thumbnail_path"]
            if tp and Path(tp).name == f"{cs}.jpg" and Path(tp).exists():
                conn.execute("UPDATE clips SET thumb_ok = 1 WHERE id = ?", (r["id"],))
            elif thumbs_fixed < max_thumbs:
                if is_photo:
                    ip = Path(r["ingest_path"])
                    thumb = thumbs.image_thumb(ip, cs) if ip.exists() else None
                else:
                    src = (Path(r["proxy_path"]) if r["proxy_path"] and Path(r["proxy_path"]).exists()
                           else Path(r["ingest_path"]))
                    thumb = _gen_thumb(src, cs) if src.exists() else None
                if thumb:
                    conn.execute("UPDATE clips SET thumbnail_path = ?, thumb_ok = 1 WHERE id = ?",
                                 (str(thumb), r["id"]))
                    thumbs_fixed += 1
                else:
                    conn.execute("UPDATE clips SET thumb_ok = 0 WHERE id = ?", (r["id"],))
            # else: leave NULL for the next sweep

        # --- proxy: photos need none; videos must be browser-playable H.264 ---
        if r["proxy_ok"] is None:
            if is_photo:
                conn.execute("UPDATE clips SET proxy_ok = 1 WHERE id = ?", (r["id"],))
                continue
            proxy = r["proxy_path"]
            if proxy and Path(proxy).exists() and _probe(Path(proxy))[0] == "h264":
                conn.execute("UPDATE clips SET proxy_ok = 1 WHERE id = ?", (r["id"],))
            elif transcoded < max_transcodes:
                src = (Path(proxy) if proxy and Path(proxy).exists()
                       else Path(r["ingest_path"]))
                if not src.exists():
                    conn.execute("UPDATE clips SET proxy_ok = 0 WHERE id = ?", (r["id"],))
                else:
                    dest = config.PROXY_DIR / f"{cs}.mp4"
                    if _transcode_h264(src, dest):
                        conn.execute("UPDATE clips SET proxy_path = ?, proxy_ok = 1 WHERE id = ?",
                                     (str(dest), r["id"]))
                        transcoded += 1
                    else:
                        conn.execute("UPDATE clips SET proxy_ok = 0 WHERE id = ?", (r["id"],))
            # else: leave NULL for the next sweep
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM clips WHERE proxy_ok IS NULL OR thumb_ok IS NULL"
    ).fetchone()[0]
    return len(rows), thumbs_fixed, transcoded, remaining
