"""
Choosing a trustworthy recording timestamp.

GoPro timestamps come from the camera's clock, which is often wrong (a flat
battery resets it — we've seen this camera report 2016 dates). So we don't trust
any single source: we take candidates in preference order and pick the first
*plausible* one, falling back to file mtime and finally to None (import time).
"""

from datetime import datetime, timezone, timedelta

# Anything outside this window is treated as a bad clock, not a real date.
_MIN = datetime(2008, 1, 1, tzinfo=timezone.utc)   # first GoPro HD Hero: 2009


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plausible(dt: datetime | None) -> bool:
    return bool(dt) and _MIN <= dt <= _now() + timedelta(days=2)


def from_unix(value) -> datetime | None:
    """GoPro media-list 'cre'/'mod' fields: seconds since the Unix epoch."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def from_exif(value: str | None) -> datetime | None:
    """ExifTool dates look like '2024:02:15 10:30:00' (naive → treat as UTC)."""
    if not value:
        return None
    text = value.strip().split(".")[0].split("+")[0].strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def from_mtime(path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def pick(*candidates: datetime | None) -> str | None:
    """Return the first plausible candidate as an ISO string, else None."""
    for dt in candidates:
        if _plausible(dt):
            return dt.astimezone(timezone.utc).isoformat()
    return None
