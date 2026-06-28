from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import cameras, config, db, events as ev

app = FastAPI(title="Rushes")

app.mount("/thumbs",   StaticFiles(directory=str(config.THUMB_DIR)),   name="thumbs")
app.mount("/footage",  StaticFiles(directory=str(config.FOOTAGE_DIR)), name="footage")

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
    return _templates.TemplateResponse("index.html", {
        "request": request, "clips": clips,
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

    return _templates.TemplateResponse("events.html", {
        "request": request, "event_data": event_data,
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
    return _templates.TemplateResponse("cameras.html", {
        "request": request, "cameras": cam_rows,
    })


@app.post("/cameras/{camera_id}/rename")
async def rename_camera(camera_id: int, name: str = Form(...)):
    conn = db.connect()
    cameras.rename(conn, camera_id, name)
    return RedirectResponse("/cameras", status_code=303)


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
