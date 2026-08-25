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
            thumbnail_path TEXT,
            proxy_path     TEXT,
            media_type     TEXT NOT NULL DEFAULT 'video',
            raw_path       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_clips_recorded ON clips(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_clips_camera   ON clips(camera_id);
        CREATE INDEX IF NOT EXISTS idx_clips_event    ON clips(event_id);

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS shares (
            id            INTEGER PRIMARY KEY,
            token         TEXT    NOT NULL UNIQUE,
            password_hash TEXT,                       -- NULL = no password
            expires_at    TEXT,                       -- ISO; NULL = never
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS share_clips (
            share_id INTEGER NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
            clip_id  INTEGER NOT NULL REFERENCES clips(id),
            PRIMARY KEY (share_id, clip_id)
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
    # Migrations for pre-existing clips tables.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(clips)")]
    if "proxy_path" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN proxy_path TEXT")
    if "jf_synced_fav" not in cols:
        # Last favourite state we reconciled with Jellyfin (NULL = never), used
        # to detect which side changed for two-way sync.
        conn.execute("ALTER TABLE clips ADD COLUMN jf_synced_fav INTEGER")
    if "proxy_ok" not in cols:
        # Whether the proxy is confirmed browser-playable H.264 (NULL = unchecked,
        # 1 = ready, 0 = couldn't be made). GoPro .LRV is sometimes HEVC, which a
        # browser can't play, so those get re-transcoded to H.264.
        conn.execute("ALTER TABLE clips ADD COLUMN proxy_ok INTEGER")
    if "thumb_ok" not in cols:
        # Whether the thumbnail is confirmed to be keyed by checksum (unique per
        # clip). Legacy thumbnails were named by filename stem, so same-named
        # clips from different cameras collided; those get regenerated.
        conn.execute("ALTER TABLE clips ADD COLUMN thumb_ok INTEGER")
    if "media_type" not in cols:
        # 'video' (default) or 'photo'. Photos share this table so they flow
        # through cameras/events/shares, but have no proxy/duration.
        conn.execute("ALTER TABLE clips ADD COLUMN media_type TEXT NOT NULL DEFAULT 'video'")
    if "raw_path" not in cols:
        # For a photo, the sibling .GPR raw (download-only), checksum-keyed.
        conn.execute("ALTER TABLE clips ADD COLUMN raw_path TEXT")
    conn.commit()
