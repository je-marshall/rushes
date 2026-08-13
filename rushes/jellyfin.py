"""
Jellyfin HTTP API — rescan (fire-and-forget) and favourites sync (best-effort).
Nothing here may block or crash ingest/web when Jellyfin is down or misconfigured.

Auth uses the current scheme `Authorization: MediaBrowser Token="<key>"`
(the old X-Emby-Token header still works but is deprecated).
"""

import logging

import httpx

from . import config, db, settings

log = logging.getLogger("rushes.jellyfin")


def _configured() -> bool:
    return bool(config.JELLYFIN_URL and config.JELLYFIN_TOKEN)


def _headers() -> dict:
    return {"Authorization": f'MediaBrowser Token="{config.JELLYFIN_TOKEN}"'}


def trigger_rescan() -> None:
    """Ask Jellyfin to scan all libraries. Best-effort."""
    if not _configured():
        return
    try:
        with httpx.Client(timeout=10) as client:
            client.post(f"{config.JELLYFIN_URL}/Library/Refresh", headers=_headers())
    except Exception:
        pass  # Jellyfin being down must not affect anything else


def _resolve_user_id(client: httpx.Client, name: str) -> str | None:
    r = client.get(f"{config.JELLYFIN_URL}/Users", headers=_headers())
    r.raise_for_status()
    for u in r.json():
        if (u.get("Name") or "").lower() == name.lower():
            return u.get("Id")
    return None


def sync_favourites() -> tuple[int, int]:
    """
    Two-way *union* sync of favourites with the configured Jellyfin user:
    favouriting a clip in either Rushes or Jellyfin marks it favourite in both.
    Un-favouriting is NOT propagated (do it in the place you set it).

    Jellyfin exposes no path lookup, so we enumerate its items and match on the
    path *below the footage root* (e.g. "unsorted/hero10/GX010001.MP4"). That
    makes it prefix-agnostic — Rushes can have it at /data/footage and Jellyfin
    at /media/gopro; only the tail has to line up. Returns (pulled, pushed).
    """
    if not _configured() or not config.JELLYFIN_USER:
        return (0, 0)

    pulled = pushed = 0
    try:
        with httpx.Client(timeout=20) as client:
            uid = _resolve_user_id(client, config.JELLYFIN_USER)
            if not uid:
                log.warning("Jellyfin user %r not found", config.JELLYFIN_USER)
                return (0, 0)

            r = client.get(
                f"{config.JELLYFIN_URL}/Items", headers=_headers(),
                params={"userId": uid, "Recursive": "true",
                        "Fields": "Path", "IncludeItemTypes": "Video,Movie"},
            )
            r.raise_for_status()

            # Index Jellyfin items by basename; each clip then matches the
            # candidate whose path ends with the clip's path below the footage
            # root (prefix-agnostic), falling back to a unique basename.
            by_base: dict[str, list] = {}
            for it in r.json().get("Items", []):
                path = (it.get("Path") or "").replace("\\", "/")
                if not path:
                    continue
                by_base.setdefault(path.rsplit("/", 1)[-1], []).append({
                    "id": it.get("Id"),
                    "fav": bool((it.get("UserData") or {}).get("IsFavorite")),
                    "path": path,
                })

            conn         = db.connect()
            footage_root = str(settings.footage_dir(conn)).replace("\\", "/").rstrip("/")

            def _match(ingest_path: str):
                p    = ingest_path.replace("\\", "/")
                base = p.rsplit("/", 1)[-1]
                cands = by_base.get(base, [])
                if not cands:
                    return None
                rel = p[len(footage_root):].lstrip("/") if p.startswith(footage_root) else base
                for c in cands:
                    if c["path"].endswith(rel):
                        return c
                return cands[0] if len(cands) == 1 else None

            clips = conn.execute("SELECT id, ingest_path, is_favourite FROM clips").fetchall()
            for clip in clips:
                it = _match(clip["ingest_path"])
                if not it:
                    continue
                if it["fav"] and not clip["is_favourite"]:
                    conn.execute("UPDATE clips SET is_favourite = 1 WHERE id = ?", (clip["id"],))
                    pulled += 1
                elif clip["is_favourite"] and not it["fav"] and it["id"]:
                    resp = client.post(
                        f"{config.JELLYFIN_URL}/UserFavoriteItems/{it['id']}",
                        headers=_headers(), params={"userId": uid},
                    )
                    if resp.status_code < 400:
                        pushed += 1
            if pulled:
                conn.commit()
    except Exception as exc:
        log.warning("favourites sync failed: %s", exc)
    return (pulled, pushed)
