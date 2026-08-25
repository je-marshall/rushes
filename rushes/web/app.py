from contextlib import asynccontextmanager
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import cameras, config, db, events as ev, importer, settings, shares

_UNPROTECTED = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Public share viewing (/share/<token>/...) bypasses login; the
        # authenticated app (incl. /shares management) does not.
        if request.url.path in _UNPROTECTED or request.url.path.startswith("/share/"):
            return await call_next(request)
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SECRET_KEY:
        raise RuntimeError(
            "RUSHES_SECRET_KEY is not set. "
            "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if not config.AUTH_PASSWORD:
        raise RuntimeError("RUSHES_PASSWORD is not set.")
    db.init_db(db.connect())
    yield


app = FastAPI(title="Rushes", lifespan=lifespan)
# SessionMiddleware must be added last so it is outermost and runs first,
# populating request.session before AuthMiddleware checks it.
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 30)

# Only thumbnails are served by the web app. Footage playback is via Jellyfin,
# and clips carry absolute paths so the footage root can be moved freely.
app.mount("/thumbs", StaticFiles(directory=str(config.THUMB_DIR)), name="thumbs")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Clips — unsorted view
# ---------------------------------------------------------------------------

def _query_unsorted(conn, favourite: bool) -> list[dict]:
    where = ["c.event_id IS NULL"]
    if favourite: where.append("c.is_favourite = 1")
    rows = conn.execute(f"""
        SELECT c.*, cam.name AS camera_name, cam.slug AS camera_slug
        FROM clips c
        LEFT JOIN cameras cam ON cam.id = c.camera_id
        WHERE {' AND '.join(where)}
        ORDER BY c.recorded_at DESC NULLS LAST
    """).fetchall()
    return _enrich_clips(rows)


@app.get("/")
async def home():
    return RedirectResponse("/events", status_code=302)


@app.get("/unsorted", response_class=HTMLResponse)
async def index(request: Request, favourite: bool = False):
    # The grid is populated + kept live by JS via /api/unsorted.json; here we
    # just render the shell and the events list for the assign dropdown.
    conn       = db.connect()
    all_events = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    return _templates.TemplateResponse(request, "index.html", {
        "favourite": favourite,
        "all_events": all_events,
    })


@app.get("/api/unsorted.json")
async def unsorted_json(favourite: bool = False):
    conn = db.connect()
    return {"clips": [_clip_public(c) for c in _query_unsorted(conn, favourite)]}


@app.get("/clip/{clip_id}/video")
async def clip_video(clip_id: int):
    """Playback stream — prefer the H.264 proxy (plays in any browser), falling
    back to the original (which may be HEVC and not decode everywhere)."""
    conn = db.connect()
    row  = conn.execute("SELECT ingest_path, proxy_path, filename FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        return HTMLResponse("clip not found", status_code=404)
    proxy = row["proxy_path"]
    path  = Path(proxy) if proxy and Path(proxy).exists() else Path(row["ingest_path"])
    if not path.exists():
        return HTMLResponse("file missing", status_code=404)
    return FileResponse(str(path), media_type="video/mp4")   # honours Range → seeking


@app.get("/clip/{clip_id}/download")
async def clip_download(clip_id: int):
    """Always the original, full-quality file (video or photo)."""
    conn = db.connect()
    row  = conn.execute("SELECT ingest_path, filename FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        return HTMLResponse("clip not found", status_code=404)
    path = Path(row["ingest_path"])
    if not path.exists():
        return HTMLResponse("file missing", status_code=404)
    return FileResponse(str(path), filename=row["filename"])


@app.get("/clip/{clip_id}/photo")
async def clip_photo(clip_id: int):
    """The full-size photo (JPEG) for a photo clip."""
    conn = db.connect()
    row  = conn.execute("SELECT ingest_path FROM clips WHERE id = ? AND media_type = 'photo'", (clip_id,)).fetchone()
    if not row or not Path(row["ingest_path"]).exists():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(row["ingest_path"], media_type="image/jpeg")


@app.get("/clip/{clip_id}/raw")
async def clip_raw(clip_id: int):
    """The .GPR raw file for a photo, as a download."""
    conn = db.connect()
    row  = conn.execute("SELECT raw_path, filename FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row or not row["raw_path"] or not Path(row["raw_path"]).exists():
        return HTMLResponse("no raw", status_code=404)
    name = Path(row["filename"]).stem + ".GPR"
    return FileResponse(row["raw_path"], media_type="application/octet-stream", filename=name)


@app.post("/clips/assign")
async def assign_clips(clip_ids: str = Form(...), event_id: str = Form(""),
                       new_event: str = Form("")):
    ids  = [int(i) for i in clip_ids.split(",") if i.strip()]
    conn = db.connect()
    new_event = new_event.strip()
    if new_event:
        target = ev.get_or_create(conn, new_event)["id"]
    elif event_id:
        target = int(event_id)
    else:
        return RedirectResponse("/unsorted", status_code=303)  # nothing chosen
    if ids:
        ev.assign_clips(conn, ids, target)
    return RedirectResponse("/unsorted", status_code=303)


@app.post("/clips/{clip_id}/favourite")
async def toggle_favourite(clip_id: int):
    conn = db.connect()
    conn.execute("UPDATE clips SET is_favourite = NOT is_favourite WHERE id = ?", (clip_id,))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@app.get("/events", response_class=HTMLResponse)
async def events_list(request: Request):
    conn       = db.connect()
    event_rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    # Attach clip counts and camera breakdown to each event
    blocks = []
    for evt in event_rows:
        clips = conn.execute("""
            SELECT c.*, cam.name AS camera_name, cam.slug AS camera_slug
            FROM clips c
            LEFT JOIN cameras cam ON cam.id = c.camera_id
            WHERE c.event_id = ?
            ORDER BY c.recorded_at DESC NULLS LAST
        """, (evt["id"],)).fetchall()
        enriched = _enrich_clips(clips)
        top = enriched[0]["recorded_at"] if enriched else None
        blocks.append({
            "id": evt["id"], "name": evt["name"], "slug": evt["slug"],
            "count": len(enriched), "recorded": top,
            "clips": [_clip_public(c) for c in enriched],
        })

    # Newest footage first; undated/empty events fall to the bottom.
    blocks.sort(key=lambda b: (b["recorded"] is not None, b["recorded"] or ""), reverse=True)

    return _templates.TemplateResponse(request, "events.html", {"blocks": blocks})


@app.post("/events/create")
async def create_event(name: str = Form(...), description: str = Form("")):
    conn = db.connect()
    ev.create(conn, name, description)
    return RedirectResponse("/events", status_code=303)


@app.post("/events/{event_id}/rename")
async def rename_event(event_id: int, name: str = Form(...)):
    conn = db.connect()
    try:
        ev.rename(conn, event_id, name)
    except ValueError:
        pass  # e.g. empty or duplicate name — leave the event unchanged
    return RedirectResponse("/events", status_code=303)


@app.post("/events/{event_id}/unassign")
async def unassign_clips(event_id: int, clip_ids: str = Form(...)):
    ids  = [int(i) for i in clip_ids.split(",") if i.strip()]
    conn = db.connect()
    ev.unassign_clips(conn, ids)
    return RedirectResponse(f"/events", status_code=303)


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------

@app.get("/cameras", response_class=HTMLResponse)
async def cameras_list(request: Request):
    conn     = db.connect()
    cam_rows = conn.execute("""
        SELECT cam.*, COUNT(c.id) AS clip_count
        FROM cameras cam
        LEFT JOIN clips c ON c.camera_id = cam.id
        GROUP BY cam.id
        ORDER BY cam.last_seen DESC
    """).fetchall()
    return _templates.TemplateResponse(request, "cameras.html", {
        "cameras": cam_rows,
    })


@app.post("/cameras/{camera_id}/rename")
async def rename_camera(camera_id: int, name: str = Form(...)):
    conn = db.connect()
    cameras.rename(conn, camera_id, name)
    return RedirectResponse("/cameras", status_code=303)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    conn = db.connect()
    jobs = conn.execute(
        "SELECT * FROM import_jobs ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return _templates.TemplateResponse(request, "import.html", {
        "jobs": [dict(j) for j in jobs],
    })


@app.post("/import/start")
async def import_start(source_path: str = Form(...)):
    path = source_path.strip()
    conn = db.connect()
    if path:
        importer.enqueue(conn, path)
    return RedirectResponse("/import", status_code=303)


@app.post("/import/{job_id}/cancel")
async def import_cancel(job_id: int):
    conn = db.connect()
    importer.request_cancel(conn, job_id)
    return RedirectResponse("/import", status_code=303)


@app.post("/import/{job_id}/rerun")
async def import_rerun(job_id: int):
    conn = db.connect()
    importer.rerun(conn, job_id)
    return RedirectResponse("/import", status_code=303)


@app.post("/import/clear")
async def import_clear():
    conn = db.connect()
    importer.clear_finished(conn)
    return RedirectResponse("/import", status_code=303)


@app.get("/import/jobs.json")
async def import_jobs_json():
    conn = db.connect()
    jobs = conn.execute("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 20").fetchall()
    return {"jobs": [dict(j) for j in jobs]}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False, error: str = ""):
    conn = db.connect()
    return _templates.TemplateResponse(request, "settings.html", {
        "footage_dir": str(settings.footage_dir(conn)),
        "state_dir":   str(config.BASE_DIR),
        "db_path":     str(config.DB_PATH),
        "saved":       saved,
        "error":       error,
    })


@app.post("/settings/footage")
async def settings_footage(footage_dir: str = Form(...)):
    conn = db.connect()
    try:
        settings.set_footage_dir(conn, footage_dir.strip())
    except ValueError as exc:
        return RedirectResponse(f"/settings?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/settings?saved=true", status_code=303)


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------

def _share_scope(clips: list[dict], token: str) -> list[dict]:
    """Rewrite media URLs to the public, token-scoped share endpoints."""
    for c in clips:
        base = f"/share/{token}/clip/{c['id']}"
        c["thumb_url"]    = f"{base}/thumb" if c.get("thumbnail_path") else None
        c["video_url"]    = f"{base}/video"
        c["download_url"] = f"{base}/download"
        c["image_url"]    = f"{base}/photo" if c["media_type"] == "photo" else None
        c["raw_url"]      = f"{base}/raw" if c.get("raw_path") else None
    return clips


@app.get("/shares", response_class=HTMLResponse)
async def shares_list(request: Request):
    conn = db.connect()
    return _templates.TemplateResponse(request, "shares.html", {"shares": shares.list_all(conn)})


@app.post("/shares/create")
async def shares_create(clip_ids: str = Form(...), expiry: str = Form("7d"),
                        password: str = Form("")):
    ids  = [int(i) for i in clip_ids.split(",") if i.strip()]
    conn = db.connect()
    if ids:
        shares.create(conn, ids, expiry, password.strip())
    return RedirectResponse("/shares", status_code=303)


@app.post("/shares/{share_id}/revoke")
async def shares_revoke(share_id: int):
    conn = db.connect()
    shares.revoke(conn, share_id)
    return RedirectResponse("/shares", status_code=303)


@app.get("/share/{token}", response_class=HTMLResponse)
async def share_view(request: Request, token: str):
    conn = db.connect()
    s = shares.get(conn, token)
    if not s:
        return _templates.TemplateResponse(request, "share.html", {"state": "missing"}, status_code=404)
    if shares.is_expired(s):
        return _templates.TemplateResponse(request, "share.html", {"state": "expired"}, status_code=410)
    if s["password_hash"] and token not in request.session.get("shares_ok", []):
        return _templates.TemplateResponse(request, "share.html", {"state": "locked", "token": token})
    clips = _share_scope(_enrich_clips(shares.clip_rows(conn, s["id"])), token)
    return _templates.TemplateResponse(request, "share.html", {"state": "ok", "token": token, "clips": clips})


@app.post("/share/{token}/unlock")
async def share_unlock(request: Request, token: str, password: str = Form("")):
    conn = db.connect()
    s = shares.get(conn, token)
    if s and not shares.is_expired(s) and s["password_hash"] and shares.verify_password(s["password_hash"], password):
        ok = request.session.get("shares_ok", [])
        request.session["shares_ok"] = ok + [token] if token not in ok else ok
    return RedirectResponse(f"/share/{token}", status_code=303)


@app.get("/share/{token}/clip/{clip_id}/{what}")
async def share_media(request: Request, token: str, clip_id: int, what: str):
    conn = db.connect()
    s = shares.get(conn, token)
    if not s or shares.is_expired(s):
        return HTMLResponse("unavailable", status_code=404)
    if s["password_hash"] and token not in request.session.get("shares_ok", []):
        return HTMLResponse("locked", status_code=403)
    if not shares.contains(conn, s["id"], clip_id):
        return HTMLResponse("not in share", status_code=404)
    row = conn.execute(
        "SELECT ingest_path, proxy_path, raw_path, thumbnail_path, filename FROM clips WHERE id = ?",
        (clip_id,),
    ).fetchone()
    if not row:
        return HTMLResponse("not found", status_code=404)

    if what == "download":
        p = Path(row["ingest_path"])
        return FileResponse(str(p), filename=row["filename"]) if p.exists() else HTMLResponse("gone", status_code=404)
    if what == "raw":
        if not row["raw_path"] or not Path(row["raw_path"]).exists():
            return HTMLResponse("no raw", status_code=404)
        return FileResponse(row["raw_path"], media_type="application/octet-stream",
                            filename=Path(row["filename"]).stem + ".GPR")
    if what == "thumb":
        p = row["thumbnail_path"]
        return FileResponse(p, media_type="image/jpeg") if p and Path(p).exists() else HTMLResponse("no thumb", status_code=404)
    if what == "photo":
        p = Path(row["ingest_path"])
        return FileResponse(str(p), media_type="image/jpeg") if p.exists() else HTMLResponse("gone", status_code=404)
    if what == "video":
        proxy = row["proxy_path"]
        p = Path(proxy) if proxy and Path(proxy).exists() else Path(row["ingest_path"])
        return FileResponse(str(p), media_type="video/mp4") if p.exists() else HTMLResponse("gone", status_code=404)
    return HTMLResponse("bad request", status_code=400)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=302)
    return _templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    username_ok = secrets.compare_digest(username, config.AUTH_USERNAME)
    password_ok = secrets.compare_digest(password, config.AUTH_PASSWORD)
    if username_ok and password_ok:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"})


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enrich_clips(rows) -> list[dict]:
    result = []
    for row in rows:
        c = dict(row)
        c["thumb_url"] = (
            "/thumbs/" + Path(c["thumbnail_path"]).name
            if c.get("thumbnail_path") else None
        )
        c["display_camera"] = c.get("camera_name") or c.get("camera_slug") or c.get("camera_serial", "?")
        c["media_type"] = c.get("media_type") or "video"
        c["video_url"] = f"/clip/{c['id']}/video"
        if c["media_type"] == "photo":
            c["image_url"] = f"/clip/{c['id']}/photo"
        if c.get("raw_path"):
            c["raw_url"] = f"/clip/{c['id']}/raw"
        result.append(c)
    return result


# Slim view sent to the browser — display fields only (no internal paths).
_PUBLIC_FIELDS = ("id", "filename", "media_type", "display_camera",
                  "duration_secs", "recorded_at", "size_bytes", "is_favourite",
                  "thumb_url", "video_url", "image_url", "raw_url")

def _clip_public(c: dict) -> dict:
    d = {k: c.get(k) for k in _PUBLIC_FIELDS}
    d["download_url"] = f"/clip/{c['id']}/download"
    return d


def main() -> None:
    import uvicorn
    uvicorn.run("rushes.web.app:app", host="0.0.0.0", port=8765, reload=False)
