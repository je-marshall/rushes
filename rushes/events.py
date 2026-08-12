"""
Event management: create events, assign clips (which physically moves the file).
"""

import shutil
import sqlite3
from pathlib import Path

from . import jellyfin, settings
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


def get_or_create(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Return the event with this name's slug, creating it if needed."""
    row = conn.execute("SELECT * FROM events WHERE slug = ?", (slugify(name),)).fetchone()
    return row if row else create(conn, name)


def rename(conn: sqlite3.Connection, event_id: int, new_name: str) -> None:
    """Rename an event: update name/slug, move its folder, fix clip paths."""
    evt = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not evt:
        raise ValueError(f"Event {event_id} not found")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Event name cannot be empty")
    new_slug = slugify(new_name)

    clash = conn.execute(
        "SELECT id FROM events WHERE slug = ? AND id <> ?", (new_slug, event_id)
    ).fetchone()
    if clash:
        raise ValueError("Another event already uses that name")

    old_slug = evt["slug"]
    if new_slug != old_slug:
        events_root = settings.events_dir(conn)
        old_dir = events_root / old_slug
        new_dir = events_root / new_slug
        if old_dir.exists() and old_dir != new_dir:
            if new_dir.exists():
                raise ValueError(f"Folder already exists: {new_dir}")
            shutil.move(str(old_dir), str(new_dir))
            for clip in conn.execute(
                "SELECT id, ingest_path FROM clips WHERE event_id = ?", (event_id,)
            ).fetchall():
                old_path = Path(clip["ingest_path"])
                try:
                    rel = old_path.relative_to(old_dir)
                except ValueError:
                    continue
                conn.execute(
                    "UPDATE clips SET ingest_path = ? WHERE id = ?",
                    (str(new_dir / rel), clip["id"]),
                )

    conn.execute("UPDATE events SET name = ?, slug = ? WHERE id = ?",
                 (new_name, new_slug, event_id))
    conn.commit()
    jellyfin.trigger_rescan()


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

        dest_dir = settings.events_dir(conn) / event["slug"] / camera_slug(camera)
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

        dest_dir = settings.unsorted_dir(conn) / camera_slug(camera)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / clip["filename"]

        shutil.move(str(clip["ingest_path"]), str(dest))

        conn.execute(
            "UPDATE clips SET ingest_path = ?, event_id = NULL WHERE id = ?",
            (str(dest), clip_id),
        )

    conn.commit()
    jellyfin.trigger_rescan()
