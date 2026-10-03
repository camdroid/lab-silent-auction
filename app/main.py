"""LAB Lot Tracker web app.

Everything is served under /t/<TRACKER_TOKEN>/. Anyone with that link can use
the tracker; any other path returns 404.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config
from .photos import MIME, PhotoError, PhotoLibrary
from .stores import STAGES, StoreError, lot_dict, open_store

config.check()
store = open_store()
photos = PhotoLibrary(config.DATA_DIR, config.MAX_PHOTO_MB * 1024 * 1024)
INDEX = (Path(__file__).parent / "static" / "index.html").read_text()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
base = f"/t/{config.TRACKER_TOKEN}"
r = APIRouter(prefix=base)

SHEET_ERROR = "Couldn't save to the spreadsheet. Check your connection and try again."


def _lot_or_404(lot_id: int):
    try:
        lot = store.lot(lot_id)
    except StoreError as e:
        raise HTTPException(503, f"Couldn't load lots: {e}")
    if not lot:
        raise HTTPException(404, f"There's no lot {lot_id}.")
    return lot


def _photo_json(p: dict) -> dict:
    return {"id": p["id"], "url": f"{base}/photos/{p['id']}", "thumb": f"{base}/thumbs/{p['id']}.jpg",
            "replaced": bool(p["replaced"]), "created_at": p["created_at"]}


# ---------------------------------------------------------------- pages

@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@r.get("/", response_class=HTMLResponse)
@r.get("/links", response_class=HTMLResponse)
@r.get("/lot/{lot_id}", response_class=HTMLResponse)
def page(lot_id: int | None = None):
    return HTMLResponse(INDEX, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ---------------------------------------------------------------- API

@r.get("/api/state")
def state():
    try:
        snap = store.snapshot()
    except StoreError as e:
        raise HTTPException(503, f"Couldn't load lots from the spreadsheet: {e}")
    by_lot: dict[int, list] = {}
    for p in photos.all():
        by_lot.setdefault(p["lot_id"], []).append(_photo_json(p))
    lots = []
    for lot in snap["lots"]:
        prog = snap["progress"].get(lot.id, {})
        lots.append({**lot_dict(lot), "stages": {k: bool(prog.get(k)) for k in STAGES},
                     "retake": bool(prog.get("retake")), "photos": by_lot.get(lot.id, [])})
    return {"lots": lots, "flags": snap["flags"], "source": store.source,
            "refreshed_at": snap["refreshed_at"], "error": snap["error"]}


class StageIn(BaseModel):
    stage: str
    done: bool


@r.post("/api/lots/{lot_id}/stages")
def set_stage(lot_id: int, body: StageIn):
    _lot_or_404(lot_id)
    if body.stage not in STAGES:
        raise HTTPException(400, "Unknown step.")
    fields = {body.stage: body.done}
    if body.stage == "photographed" and body.done:
        fields["retake"] = False
    try:
        return store.set_fields(lot_id, fields)
    except StoreError:
        raise HTTPException(502, SHEET_ERROR)


@r.post("/api/lots/{lot_id}/photos")
async def upload_photo(lot_id: int, photo: UploadFile = File(...)):
    _lot_or_404(lot_id)
    try:
        rec = await run_in_threadpool(photos.add, lot_id, photo.file, photo.filename or "")
    except PhotoError as e:
        raise HTTPException(400, str(e))
    warning = None
    try:
        await run_in_threadpool(store.set_fields, lot_id, {
            "photographed": True, "retake": False, "photos": photos.current_count(lot_id)})
    except StoreError:
        warning = "Photo saved, but the spreadsheet didn't update. It will catch up on the next photo or tick."
    return {"photo": _photo_json(rec), "warning": warning}


class RetakeIn(BaseModel):
    reason: str = Field("", max_length=2000)


@r.post("/api/lots/{lot_id}/retake")
def retake(lot_id: int, body: RetakeIn):
    _lot_or_404(lot_id)
    photos.mark_replaced(lot_id)
    reason = body.reason.strip()
    try:
        store.set_fields(lot_id, {"photographed": False, "uploaded": False, "retake": True, "photos": 0})
        flag = store.add_flag(lot_id, "Needs retake" + (f": {reason}" if reason else "."), kind="retake")
    except StoreError:
        raise HTTPException(502, SHEET_ERROR)
    return {"flag": flag}


class FlagIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@r.post("/api/lots/{lot_id}/flags")
def add_flag(lot_id: int, body: FlagIn):
    _lot_or_404(lot_id)
    if not body.text.strip():
        raise HTTPException(400, "Say what's still needed.")
    try:
        return store.add_flag(lot_id, body.text)
    except StoreError:
        raise HTTPException(502, SHEET_ERROR)


@r.post("/api/flags/{flag_id}/resolve")
def resolve_flag(flag_id: str):
    try:
        return store.resolve_flag(flag_id)
    except KeyError:
        raise HTTPException(404, "That flag doesn't exist any more.")
    except StoreError:
        raise HTTPException(502, SHEET_ERROR)


@r.get("/api/lots/{lot_id}/photos.zip")
def photos_zip(lot_id: int):
    _lot_or_404(lot_id)
    if not photos.current_count(lot_id):
        raise HTTPException(404, "This lot has no photos yet.")
    return StreamingResponse(photos.zip_lot(lot_id), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="lot-{lot_id}-photos.zip"'})


# ---------------------------------------------------------------- files

@r.get("/photos/{photo_id}")
def original(photo_id: str, download: bool = False):
    rec = photos.get(photo_id)
    if not rec:
        raise HTTPException(404)
    path = photos.original_path(rec)
    return FileResponse(path, media_type=MIME.get(path.suffix, "application/octet-stream"),
                        filename=photos.download_name(rec),
                        content_disposition_type="attachment" if download else "inline")


@r.get("/thumbs/{photo_id}.jpg")
def thumb(photo_id: str):
    path = photos.thumb_path(photo_id) if photo_id.isalnum() else None
    if not path or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


app.include_router(r)
