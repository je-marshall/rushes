from pathlib import Path
import os

BASE_DIR     = Path(os.environ.get("RUSHES_DATA", "/var/lib/rushes"))
FOOTAGE_DIR  = BASE_DIR / "footage"
UNSORTED_DIR = FOOTAGE_DIR / "unsorted"
EVENTS_DIR   = FOOTAGE_DIR / "events"
THUMB_DIR    = BASE_DIR / "thumbs"
DB_PATH      = BASE_DIR / "rushes.db"

# Jellyfin integration — set these in environment or override here.
# API key: Jellyfin dashboard → Administration → API Keys → +
JELLYFIN_URL   = os.environ.get("JELLYFIN_URL", "")    # e.g. http://localhost:8096
JELLYFIN_TOKEN = os.environ.get("JELLYFIN_TOKEN", "")

for _d in (UNSORTED_DIR, EVENTS_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
