import asyncio
from pathlib import Path
from . import config


async def generate(video_path: Path) -> Path | None:
    dest = config.THUMB_DIR / (video_path.stem + ".jpg")
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
