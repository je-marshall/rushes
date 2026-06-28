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

# Auth — required when exposing the UI publicly.
# Generate a secret key with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY    = os.environ.get("RUSHES_SECRET_KEY", "")
AUTH_USERNAME = os.environ.get("RUSHES_USERNAME", "rushes")
AUTH_PASSWORD = os.environ.get("RUSHES_PASSWORD", "")

for _d in (UNSORTED_DIR, EVENTS_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
