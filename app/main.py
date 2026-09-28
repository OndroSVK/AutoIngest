from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import db
from .logging_config import configure_logging
from .recorder import RecorderManager
from .schemas import ActionResult, Channel, ChannelCreate, ChannelUpdate
from .security import redact_url
from .storage import storage

configure_logging()
log = logging.getLogger("ingest.app")
recorder = RecorderManager(db)
archive_stop = asyncio.Event()
archive_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global archive_task
    db.init()
    storage.init()
    await recorder.start()
    archive_task = asyncio.create_task(storage.archive_loop(db, recorder, archive_stop))
    log.info("application_started")
    try:
        yield
    finally:
        archive_stop.set()
        if archive_task:
            await archive_task
        await recorder.stop()
        log.info("application_stopped")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def dashboard_context(request: Request) -> dict:
    channels = db.list_channels()
    snapshots = recorder.all_snapshots()
    queue = storage.queue_summary()
    rows = []
    for channel in channels:
        state = snapshots.get(int(channel["id"]), recorder.snapshot(int(channel["id"])))
        rows.append(
            {
                **channel,
                "redacted_input_url": redact_url(channel["input_url"]),
                "runtime": state,
                "spool_count": queue["by_channel"].get(int(channel["id"]), 0),
            }
        )
    return {
        "request": request,
        "channels": rows,
        "spool": storage.spool_status(),
        "recordings": storage.recordings_status(),
        "queue": queue,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", dashboard_context(request))


@app.get("/channels/new", response_class=HTMLResponse)
async def new_channel(request: Request):
    return templates.TemplateResponse("channel_form.html", {"request": request, "channel": None, "error": ""})


@app.get("/channels/{channel_id}/edit", response_class=HTMLResponse)
async def edit_channel(request: Request, channel_id: int):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return templates.TemplateResponse("channel_form.html", {"request": request, "channel": channel, "error": ""})


@app.post("/channels")
async def create_channel_form(
    request: Request,
    display_name: str = Form(...),
    input_url: str = Form(...),
    output_folder: str = Form(""),
    segment_duration: int = Form(300),
    enabled: bool = Form(False),
    automatic_recording: bool = Form(False),
):
    try:
        channel = db.create_channel(locals())
        await recorder.refresh()
        return RedirectResponse("/", status_code=303)
    except Exception as exc:
        return templates.TemplateResponse(
            "channel_form.html",
            {"request": request, "channel": None, "error": str(exc)},
            status_code=400,
        )


@app.post("/channels/{channel_id}")
async def update_channel_form(
    request: Request,
    channel_id: int,
    display_name: str = Form(...),
    input_url: str = Form(...),
    output_folder: str = Form(""),
    segment_duration: int = Form(300),
    enabled: bool = Form(False),
    automatic_recording: bool = Form(False),
):
    try:
        channel = db.update_channel(channel_id, locals())
        if not channel:
            raise HTTPException(404, "Channel not found")
        await recorder.restart_channel(channel_id)
        return RedirectResponse("/", status_code=303)
    except HTTPException:
        raise
    except Exception as exc:
        existing = db.get_channel(channel_id)
        return templates.TemplateResponse(
            "channel_form.html",
            {"request": request, "channel": existing, "error": str(exc)},
            status_code=400,
        )


@app.post("/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: int):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    db.update_channel(channel_id, {"enabled": not bool(channel["enabled"])})
    await recorder.restart_channel(channel_id)
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{channel_id}/restart")
async def restart_channel_form(channel_id: int):
    await recorder.restart_channel(channel_id)
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{channel_id}/delete")
async def delete_channel_form(channel_id: int):
    await recorder.stop_channel(channel_id)
    db.delete_channel(channel_id)
    return RedirectResponse("/", status_code=303)


@app.get("/api/channels", response_model=list[Channel])
async def api_list_channels():
    return db.list_channels()


@app.post("/api/channels", response_model=Channel)
async def api_create_channel(payload: ChannelCreate):
    try:
        channel = db.create_channel(payload.model_dump())
        await recorder.refresh()
        return channel
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/channels/{channel_id}", response_model=Channel)
async def api_get_channel(channel_id: int):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return channel


@app.patch("/api/channels/{channel_id}", response_model=Channel)
async def api_update_channel(channel_id: int, payload: ChannelUpdate):
    try:
        channel = db.update_channel(channel_id, payload.model_dump(exclude_unset=True))
        if not channel:
            raise HTTPException(404, "Channel not found")
        await recorder.restart_channel(channel_id)
        return channel
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/channels/{channel_id}", response_model=ActionResult)
async def api_delete_channel(channel_id: int):
    await recorder.stop_channel(channel_id)
    deleted = db.delete_channel(channel_id)
    if not deleted:
        raise HTTPException(404, "Channel not found")
    return ActionResult(ok=True, message="Channel deleted")


@app.post("/api/channels/{channel_id}/restart", response_model=ActionResult)
async def api_restart_channel(channel_id: int):
    if not db.get_channel(channel_id):
        raise HTTPException(404, "Channel not found")
    await recorder.restart_channel(channel_id)
    return ActionResult(ok=True, message="Recorder restarted")


@app.get("/api/status")
async def api_status():
    return {
        "channels": recorder.all_snapshots(),
        "spool": storage.spool_status(),
        "recordings": storage.recordings_status(),
        "queue": storage.queue_summary(),
    }


@app.get("/healthz")
async def healthz():
    spool = storage.spool_status()
    payload = {
        "ok": spool.available,
        "spool_available": spool.available,
        "recordings_available": storage.recordings_status().available,
    }
    if not spool.available:
        raise HTTPException(status_code=503, detail=payload)
    return payload
