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

from . import config, db


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


def process_batch(probe_limit: int = 150, max_transcodes: int = 1) -> tuple[int, int]:
    """One pass: cheaply confirm H.264 proxies (up to probe_limit), and transcode
    up to max_transcodes that aren't playable. Returns (marked_ok, transcoded)."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, ingest_path, proxy_path, checksum FROM clips "
        "WHERE proxy_ok IS NULL LIMIT ?", (probe_limit,)
    ).fetchall()

    marked = transcoded = 0
    for r in rows:
        proxy = r["proxy_path"]
        if proxy and Path(proxy).exists() and _probe(Path(proxy))[0] == "h264":
            conn.execute("UPDATE clips SET proxy_ok = 1 WHERE id = ?", (r["id"],))
            marked += 1
            continue

        # Needs a real H.264 proxy. Prefer the small .LRV proxy as source (cheap);
        # fall back to the original if there's no proxy at all.
        if transcoded >= max_transcodes:
            continue  # leave for the next sweep
        src = Path(proxy) if (proxy and Path(proxy).exists()) else Path(r["ingest_path"])
        if not src.exists():
            conn.execute("UPDATE clips SET proxy_ok = 0 WHERE id = ?", (r["id"],))
            continue
        dest = config.PROXY_DIR / f"{r['checksum']}.mp4"
        if _transcode_h264(src, dest):
            conn.execute("UPDATE clips SET proxy_path = ?, proxy_ok = 1 WHERE id = ?",
                         (str(dest), r["id"]))
            transcoded += 1
        else:
            conn.execute("UPDATE clips SET proxy_ok = 0 WHERE id = ?", (r["id"],))
    conn.commit()
    return marked, transcoded
