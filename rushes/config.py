from pathlib import Path
import os

# RUSHES_DATA is the state root: the SQLite DB and thumbnails live here. It's
# small and stable — set it once at install (--data-dir) and leave it. The big
# footage directory is a *runtime* setting (see settings.py), editable from the
# web UI and defaulting to <RUSHES_DATA>/footage.
BASE_DIR   = Path(os.environ.get("RUSHES_DATA", "/var/lib/rushes"))
DB_PATH    = BASE_DIR / "rushes.db"
THUMB_DIR  = BASE_DIR / "thumbs"

DEFAULT_FOOTAGE_DIR = BASE_DIR / "footage"

# Jellyfin integration — set these in environment or override here.
# API key: Jellyfin dashboard → Administration → API Keys → +
JELLYFIN_URL   = os.environ.get("JELLYFIN_URL", "")    # e.g. http://localhost:8096
JELLYFIN_TOKEN = os.environ.get("JELLYFIN_TOKEN", "")

# Auth — required when exposing the UI publicly.
# Generate a secret key with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY    = os.environ.get("RUSHES_SECRET_KEY", "")
AUTH_USERNAME = os.environ.get("RUSHES_USERNAME", "rushes")
AUTH_PASSWORD = os.environ.get("RUSHES_PASSWORD", "")

# Only the state dirs are created eagerly; footage dirs are created on demand by
# settings.py once the (possibly user-edited) footage root is known.
for _d in (BASE_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
