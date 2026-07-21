"""
Camera registry: upsert on ingest, rename via web UI.
Renaming a camera renames its unsorted folder and updates all clip paths in the DB.
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import settings
from .slug import slugify


def camera_slug(camera: sqlite3.Row) -> str:
    """Folder-safe identifier — name slug if set, otherwise raw serial."""
    return camera["slug"] or camera["serial"]


def get(conn: sqlite3.Connection, camera_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()


def upsert(conn: sqlite3.Connection, serial: str, model: str) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO cameras (serial, model, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(serial) DO UPDATE SET
            model     = excluded.model,
            last_seen = excluded.last_seen
        """,
        (serial, model, datetime.now().isoformat()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM cameras WHERE serial = ?", (serial,)).fetchone()


def rename(conn: sqlite3.Connection, camera_id: int, new_name: str) -> None:
    """
    Set a human-readable name on a camera and rename its unsorted folder.
    Also updates ingest_path for every clip that lives in that folder.
    """
    camera = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    if not camera:
        raise ValueError(f"Camera {camera_id} not found")

    old_slug = camera_slug(camera)
    new_slug  = slugify(new_name)

    unsorted = settings.unsorted_dir(conn)
    old_dir  = unsorted / old_slug
    new_dir  = unsorted / new_slug

    if old_dir.exists() and old_dir != new_dir:
        if new_dir.exists():
            raise ValueError(f"Folder already exists: {new_dir}")
        shutil.move(str(old_dir), str(new_dir))

        # Rewrite ingest_path for every clip that was under old_dir
        clips = conn.execute(
            "SELECT id, ingest_path FROM clips WHERE camera_id = ? AND event_id IS NULL",
            (camera_id,),
        ).fetchall()
        for clip in clips:
            old_path = Path(clip["ingest_path"])
            new_path = new_dir / old_path.name
            conn.execute(
                "UPDATE clips SET ingest_path = ? WHERE id = ?",
                (str(new_path), clip["id"]),
            )

    conn.execute(
        "UPDATE cameras SET name = ?, slug = ? WHERE id = ?",
        (new_name, new_slug, camera_id),
    )
    conn.commit()
