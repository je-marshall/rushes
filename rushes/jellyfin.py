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


def _reconcile(R: bool, J: bool, S) -> tuple[bool, bool, bool]:
    """Decide favourite targets from Rushes-now (R), Jellyfin-now (J) and the
    last-synced state (S; None = never). Returns (rushes_target, jellyfin_target,
    new_synced_state). First sight → union; otherwise the side that changed wins
    (Rushes on the — with booleans, only apparent — tie)."""
    if S is None:
        v = R or J
        return v, v, v
    S = bool(S)
    r_changed, j_changed = (R != S), (J != S)
    if r_changed and not j_changed:
        return R, R, R
    if j_changed and not r_changed:
        return J, J, J
    if r_changed and j_changed:      # both moved (to the same value, for booleans)
        return R, R, R
    return R, J, S                    # neither changed


def _resolve_user_id(client: httpx.Client, name: str) -> str | None:
    r = client.get(f"{config.JELLYFIN_URL}/Users", headers=_headers())
    r.raise_for_status()
    for u in r.json():
        if (u.get("Name") or "").lower() == name.lower():
            return u.get("Id")
    return None


def sync_favourites() -> tuple[int, int]:
    """
    True two-way favourites sync (incl. un-favourite) with the configured
    Jellyfin user. We remember the last-synced state per clip (clips.jf_synced_fav)
    so we can tell which side changed:
      - only Rushes changed  → push that state to Jellyfin
      - only Jellyfin changed → apply that state to Rushes
      - both changed & differ → conflict, Rushes wins
      - first time we see a clip (no snapshot) → union (favourite if either side
        has it), so enabling the sync never surprise-unfavourites anything
    Jellyfin exposes no path lookup, so we match on the path *below the footage
    root* ("unsorted/hero10/GX010001.MP4") — prefix-agnostic (Rushes /data vs
    Jellyfin /media/gopro both fine). Returns (rushes_updated, jellyfin_updated).
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

            def _set_fav(item_id: str, fav: bool) -> bool:
                try:
                    url = f"{config.JELLYFIN_URL}/UserFavoriteItems/{item_id}"
                    resp = (client.post if fav else client.delete)(
                        url, headers=_headers(), params={"userId": uid})
                    return resp.status_code < 400
                except Exception:
                    return False

            dirty = False
            clips = conn.execute(
                "SELECT id, ingest_path, is_favourite, jf_synced_fav FROM clips"
            ).fetchall()
            for clip in clips:
                it = _match(clip["ingest_path"])
                if not it:
                    continue
                R = bool(clip["is_favourite"])   # Rushes now
                J = bool(it["fav"])              # Jellyfin now
                r_t, j_t, new_s = _reconcile(R, J, clip["jf_synced_fav"])

                if r_t != R:
                    conn.execute("UPDATE clips SET is_favourite = ? WHERE id = ?",
                                 (int(r_t), clip["id"]))
                    pulled += 1; dirty = True
                if j_t != J and it["id"] and _set_fav(it["id"], j_t):
                    pushed += 1
                new_int = int(new_s)
                if clip["jf_synced_fav"] is None or clip["jf_synced_fav"] != new_int:
                    conn.execute("UPDATE clips SET jf_synced_fav = ? WHERE id = ?",
                                 (new_int, clip["id"]))
                    dirty = True
            if dirty:
                conn.commit()
    except Exception as exc:
        log.warning("favourites sync failed: %s", exc)
    return (pulled, pushed)
