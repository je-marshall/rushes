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


_BASE = ["exiftool", "-json", "-api", "largefilesupport=1"]
# The fields we care about — asking for just these keeps exiftool quick.
_FAST_TAGS = ["-Model", "-CameraSerialNumber", "-SerialNumber", "-DeviceName",
              "-CreateDate", "-MediaCreateDate", "-DateTimeOriginal",
              "-TrackCreateDate", "-FileModifyDate"]


def _run(args: list[str]) -> dict:
    try:
        out = subprocess.run(_BASE + args, capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout)[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return {}


def extract(path: Path) -> FileMeta:
    """Read metadata via exiftool. Raises ExiftoolMissing if exiftool isn't
    installed; returns all-None fields if a file has no such metadata.

    Fast path first (header metadata only). Only if the serial isn't there do we
    fall back to `-ee`, which parses the embedded GPMF stream (the full-file read
    that makes exiftool slow) to recover the serial."""
    if not available():
        raise ExiftoolMissing("exiftool not found (apt install libimage-exiftool-perl)")

    data   = _run(_FAST_TAGS + [str(path)])
    serial = _scan_for_serial(data)
    if not serial:
        deep   = _run(["-ee", "-CameraSerialNumber", "-SerialNumber", str(path)])
        serial = _scan_for_serial(deep)

    model = _first(data, "Model", "CameraModelName", "DeviceName")
    date  = _first(data, "CreateDate", "MediaCreateDate", "DateTimeOriginal",
                   "TrackCreateDate", "FileModifyDate")
    return FileMeta(serial=serial, model=model, exif_date=date)
