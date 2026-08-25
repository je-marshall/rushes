import asyncio
import shutil
import subprocess
from pathlib import Path

from . import config


def save_image(src: Path, name: str) -> Path | None:
    """Use the camera's own .THM JPEG as the thumbnail — no decoding needed."""
    dest = config.THUMB_DIR / f"{name}.jpg"
    try:
        shutil.copyfile(src, dest)
        return dest
    except OSError:
        return None


async def generate(video_path: Path, name: str) -> Path | None:
    """Fallback: extract a frame with ffmpeg when there's no .THM."""
    dest = config.THUMB_DIR / f"{name}.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", "00:00:02",
            "-vframes", "1",
            "-vf", "scale=640:-1",
            str(dest),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
        return dest if dest.exists() else None
    except Exception:
        return None


def image_thumb(src: Path, name: str) -> Path | None:
    """Downscaled JPEG thumbnail from a photo (no frame seek)."""
    dest = config.THUMB_DIR / f"{name}.jpg"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vf", "scale=640:-1", str(dest)],
            capture_output=True, timeout=60,
        )
        return dest if (proc.returncode == 0 and dest.exists()) else None
    except Exception:
        return None


def save_proxy(src: Path, name: str) -> Path | None:
    """Store the camera's low-res .LRV (H.264) as a browser-playable proxy."""
    dest = config.PROXY_DIR / f"{name}.mp4"
    try:
        shutil.copyfile(src, dest)
        return dest
    except OSError:
        return None
