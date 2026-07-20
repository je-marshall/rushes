from contextlib import asynccontextmanager
import secrets
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import cameras, config, db, events as ev, settings

_UNPROTECTED = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _UNPROTECTED:
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

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Clips — unsorted view
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, favourite: bool = False, flagged: bool = False):
    conn  = db.connect()
    where = ["c.event_id IS NULL"]
    if favourite: where.append("c.is_favourite = 1")
    if flagged:   where.append("c.flagged = 1")

    rows = conn.execute(f"""
        SELECT c.*, cam.name AS camera_name, cam.slug AS camera_slug
        FROM clips c
        LEFT JOIN cameras cam ON cam.id = c.camera_id
        WHERE {' AND '.join(where)}
        ORDER BY c.recorded_at DESC NULLS LAST
    """).fetchall()

    clips       = _enrich_clips(rows)
    all_events  = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    return _templates.TemplateResponse(request, "index.html", {
        "clips": clips,
        "favourite": favourite, "flagged": flagged,
        "all_events": all_events,
    })


@app.post("/clips/assign")
async def assign_clips(event_id: int = Form(...), clip_ids: str = Form(...)):
    ids  = [int(i) for i in clip_ids.split(",") if i.strip()]
    conn = db.connect()
    ev.assign_clips(conn, ids, event_id)
    return RedirectResponse("/", status_code=303)


@app.post("/clips/{clip_id}/favourite")
async def toggle_favourite(clip_id: int):
    conn = db.connect()
    conn.execute("UPDATE clips SET is_favourite = NOT is_favourite WHERE id = ?", (clip_id,))
    conn.commit()
    return {"ok": True}


@app.post("/clips/{clip_id}/flag")
async def toggle_flag(clip_id: int):
    conn = db.connect()
    conn.execute("UPDATE clips SET flagged = NOT flagged WHERE id = ?", (clip_id,))
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
    event_data = []
    for evt in event_rows:
        clips = conn.execute("""
            SELECT c.*, cam.name AS camera_name, cam.slug AS camera_slug
            FROM clips c
            LEFT JOIN cameras cam ON cam.id = c.camera_id
            WHERE c.event_id = ?
            ORDER BY c.recorded_at DESC NULLS LAST
        """, (evt["id"],)).fetchall()
        event_data.append({"event": evt, "clips": _enrich_clips(clips)})

    return _templates.TemplateResponse(request, "events.html", {
        "event_data": event_data,
    })


@app.post("/events/create")
async def create_event(name: str = Form(...), description: str = Form("")):
    conn = db.connect()
    ev.create(conn, name, description)
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
        result.append(c)
    return result


def main() -> None:
    uvicorn.run("rushes.web.app:app", host="0.0.0.0", port=8765, reload=False)
