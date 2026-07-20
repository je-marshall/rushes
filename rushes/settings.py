"""
Runtime settings, stored in the DB `settings` table so both the web app and the
(separate-process) ingest see the same values without a restart.

The footage directory is the one that matters: it's where clips are written and
where events are organised. It's editable from the web UI. Existing clips keep
their absolute ingest_path, so changing it only affects where *new* files land.
"""

import sqlite3
from pathlib import Path

from . import config

FOOTAGE_DIR_KEY = "footage_dir"


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def footage_dir(conn: sqlite3.Connection) -> Path:
    return Path(get(conn, FOOTAGE_DIR_KEY, str(config.DEFAULT_FOOTAGE_DIR)))


def set_footage_dir(conn: sqlite3.Connection, path: str) -> Path:
    """Validate, create if needed, persist. Returns the resolved path.
    Raises ValueError if the path can't be created or isn't writable."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("Footage directory must be an absolute path.")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not create {p}: {exc}") from exc
    testfile = p / ".rushes-write-test"
    try:
        testfile.touch()
        testfile.unlink()
    except OSError as exc:
        raise ValueError(f"{p} is not writable: {exc}") from exc
    set(conn, FOOTAGE_DIR_KEY, str(p))
    return p


def unsorted_dir(conn: sqlite3.Connection) -> Path:
    d = footage_dir(conn) / "unsorted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_dir(conn: sqlite3.Connection) -> Path:
    d = footage_dir(conn) / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d
