import sqlite3
from . import config


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cameras (
            id        INTEGER PRIMARY KEY,
            serial    TEXT    NOT NULL UNIQUE,
            name      TEXT,
            slug      TEXT,
            model     TEXT,
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            slug        TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS clips (
            id             INTEGER PRIMARY KEY,
            filename       TEXT    NOT NULL,
            ingest_path    TEXT    NOT NULL UNIQUE,
            recorded_at    TEXT,
            ingested_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            camera_id      INTEGER REFERENCES cameras(id),
            camera_serial  TEXT,
            camera_model   TEXT,
            event_id       INTEGER REFERENCES events(id),
            duration_secs  REAL,
            size_bytes     INTEGER,
            checksum       TEXT    UNIQUE,
            is_favourite   INTEGER NOT NULL DEFAULT 0,
            flagged        INTEGER NOT NULL DEFAULT 0,
            thumbnail_path TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_clips_recorded ON clips(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_clips_camera   ON clips(camera_id);
        CREATE INDEX IF NOT EXISTS idx_clips_event    ON clips(event_id);

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS import_jobs (
            id          INTEGER PRIMARY KEY,
            source_path TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|error
            total       INTEGER NOT NULL DEFAULT 0,
            processed   INTEGER NOT NULL DEFAULT 0,
            imported    INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            message     TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT
        );
    """)
    conn.commit()
