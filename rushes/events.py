"""
Event management: create events, assign clips (which physically moves the file).
"""

import shutil
import sqlite3
from pathlib import Path

from . import config, jellyfin
from .cameras import camera_slug
from .slug import slugify


def create(conn: sqlite3.Connection, name: str, description: str = "") -> sqlite3.Row:
    slug = slugify(name)
    conn.execute(
        "INSERT INTO events (name, slug, description) VALUES (?, ?, ?)",
        (name, slug, description),
    )
    conn.commit()
    return conn.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()


def assign_clips(conn: sqlite3.Connection, clip_ids: list[int], event_id: int) -> None:
    """
    Move clips into the event folder and update the DB.
    Clips from different cameras land in their own camera subfolder within the event.
    """
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        raise ValueError(f"Event {event_id} not found")

    for clip_id in clip_ids:
        clip   = conn.execute("SELECT * FROM clips WHERE id = ?",   (clip_id,)).fetchone()
        camera = conn.execute("SELECT * FROM cameras WHERE id = ?", (clip["camera_id"],)).fetchone()

        dest_dir = config.EVENTS_DIR / event["slug"] / camera_slug(camera)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / clip["filename"]

        shutil.move(str(clip["ingest_path"]), str(dest))

        conn.execute(
            "UPDATE clips SET ingest_path = ?, event_id = ? WHERE id = ?",
            (str(dest), event_id, clip_id),
        )

    conn.commit()
    jellyfin.trigger_rescan()


def unassign_clips(conn: sqlite3.Connection, clip_ids: list[int]) -> None:
    """Move clips back to their camera's unsorted folder."""
    for clip_id in clip_ids:
        clip   = conn.execute("SELECT * FROM clips WHERE id = ?",   (clip_id,)).fetchone()
        camera = conn.execute("SELECT * FROM cameras WHERE id = ?", (clip["camera_id"],)).fetchone()

        dest_dir = config.UNSORTED_DIR / camera_slug(camera)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / clip["filename"]

        shutil.move(str(clip["ingest_path"]), str(dest))

        conn.execute(
            "UPDATE clips SET ingest_path = ?, event_id = NULL WHERE id = ?",
            (str(dest), clip_id),
        )

    conn.commit()
    jellyfin.trigger_rescan()
