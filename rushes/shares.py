"""
Expiring, optionally-password-protected share links for a selection of clips —
Immich-style. A share is a token + set of clips + optional password + expiry.
Public viewing routes live under /share/<token>; creation/management is
authenticated. Recipients stream the H.264 proxy and can download originals.
"""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

# Expiry presets (label → hours; None = never).
EXPIRY_PRESETS = {"24h": 24, "7d": 168, "30d": 720, "never": None}


def _hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(stored: str, pw: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _expires_at(expiry_key: str) -> str | None:
    hours = EXPIRY_PRESETS.get(expiry_key, 168)
    if hours is None:
        return None
    return (datetime.now() + timedelta(hours=hours)).isoformat()


def is_expired(share: sqlite3.Row) -> bool:
    ea = share["expires_at"]
    if not ea:
        return False
    try:
        return datetime.fromisoformat(ea) < datetime.now()
    except ValueError:
        return False


def create(conn, clip_ids: list[int], expiry_key: str, password: str = "") -> str:
    token = secrets.token_urlsafe(16)
    cur = conn.execute(
        "INSERT INTO shares (token, password_hash, expires_at) VALUES (?, ?, ?)",
        (token, _hash_password(password) if password else None, _expires_at(expiry_key)),
    )
    share_id = cur.lastrowid
    for cid in clip_ids:
        conn.execute("INSERT OR IGNORE INTO share_clips (share_id, clip_id) VALUES (?, ?)",
                     (share_id, cid))
    conn.commit()
    return token


def get(conn, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()


def revoke(conn, share_id: int) -> None:
    conn.execute("DELETE FROM share_clips WHERE share_id = ?", (share_id,))
    conn.execute("DELETE FROM shares WHERE id = ?", (share_id,))
    conn.commit()


def contains(conn, share_id: int, clip_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM share_clips WHERE share_id = ? AND clip_id = ?", (share_id, clip_id)
    ).fetchone() is not None


def clip_rows(conn, share_id: int):
    return conn.execute("""
        SELECT c.*, cam.name AS camera_name, cam.slug AS camera_slug
        FROM share_clips sc
        JOIN clips c ON c.id = sc.clip_id
        LEFT JOIN cameras cam ON cam.id = c.camera_id
        WHERE sc.share_id = ?
        ORDER BY c.recorded_at DESC NULLS LAST
    """, (share_id,)).fetchall()


def list_all(conn):
    rows = conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
    out = []
    for s in rows:
        n = conn.execute("SELECT COUNT(*) c FROM share_clips WHERE share_id = ?", (s["id"],)).fetchone()["c"]
        out.append({"share": dict(s), "count": n, "expired": is_expired(s)})
    return out
