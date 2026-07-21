"""
Extract camera identity + recording time from an existing GoPro file, for bulk
import. Uses exiftool (which reads GoPro's embedded metadata, incl. the GPMF
CASN = camera serial). Tag names vary a little by model/firmware, so we scan the
full exiftool dump liberally rather than trusting one exact key.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ExiftoolMissing(RuntimeError):
    pass


@dataclass
class FileMeta:
    serial: str | None
    model:  str | None
    exif_date: str | None   # raw exiftool date string, if any


def available() -> bool:
    return shutil.which("exiftool") is not None


def _first(d: dict, *keys: str) -> str | None:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k])
    return None


def _scan_for_serial(d: dict) -> str | None:
    # Prefer explicit keys, then any key that looks like a serial.
    explicit = _first(d, "CameraSerialNumber", "SerialNumber", "InternalSerialNumber")
    if explicit:
        return explicit
    for k, v in d.items():
        if "serial" in k.lower() and v not in (None, ""):
            return str(v)
    return None


def extract(path: Path) -> FileMeta:
    """Read metadata via exiftool. Raises ExiftoolMissing if exiftool isn't
    installed; returns all-None fields if a file simply has no such metadata."""
    if not available():
        raise ExiftoolMissing("exiftool not found (apt install libimage-exiftool-perl)")

    # -ee extracts embedded metadata (the GPMF stream, where the serial lives).
    try:
        out = subprocess.run(
            ["exiftool", "-json", "-ee", "-api", "largefilesupport=1", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return FileMeta(None, None, None)

    try:
        data = json.loads(out.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return FileMeta(None, None, None)

    serial = _scan_for_serial(data)
    model  = _first(data, "Model", "CameraModelName", "DeviceName")
    date   = _first(data, "CreateDate", "MediaCreateDate", "DateTimeOriginal",
                    "TrackCreateDate", "FileModifyDate")
    return FileMeta(serial=serial, model=model, exif_date=date)
