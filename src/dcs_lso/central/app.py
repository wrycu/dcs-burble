"""FastAPI app: ingest API, greenie board, pass pages, ACMI downloads."""

# No `from __future__ import annotations` here: FastAPI must see the real annotation
# objects to resolve dependencies defined inside create_app().
import json
from urllib.parse import quote
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from ..cards import render_card
from ..cards.overlay import render_overlay
from ..cards.svg import X_MAX_M as OVERLAY_MAX_M, X_MIN_M as OVERLAY_MIN_M
from ..grading import grade_pass
from ..grading.trends import DEFAULT_PASSES
from . import pages
from .db import Pass, Pilot, Source, Upload
from .service import Central, IngestError

MAX_SLICE_BYTES = 20 * 1024 * 1024
MAX_RECORDING_BYTES = 1024 * 1024 * 1024  # a whole session's Tacview recording (backfill)
MAX_DEBRIEF_BYTES = 50 * 1024 * 1024
BOARD_COLUMNS = 20
MIN_ZOOM_M = 15.0  # the narrowest stretch the overlay zooms to


def create_app(central: Central) -> FastAPI:
    app = FastAPI(title="dcs-lso central", docs_url="/api/docs", redoc_url=None)
    # Uploaded recordings are processed one at a time, off the request threads.
    uploads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload")

    def source_from_token(authorization: Annotated[str | None, Header()] = None) -> Source:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        source = central.authenticate(authorization.removeprefix("Bearer ").strip())
        if source is None:
            raise HTTPException(401, "invalid token")
        return source

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/api/v1/passes", status_code=201)
    async def upload(source: Annotated[Source, Depends(source_from_token)],
                     slice: Annotated[UploadFile, File(description="standalone .zip.acmi slice")],
                     sidecar: Annotated[str, Form(description="the slice's JSON sidecar")]):
        data = await slice.read(MAX_SLICE_BYTES + 1)
        if len(data) > MAX_SLICE_BYTES:
            raise HTTPException(413, "slice too large")
        try:
            meta = json.loads(sidecar)
            result = central.ingest(source.id, data, meta)
        except (json.JSONDecodeError, IngestError) as exc:
            raise HTTPException(400, str(exc)) from exc
        body = {"pass_id": result.pass_id, "created": result.created, "grade": result.grade, "text": result.text,
                "url": f"/passes/{result.pass_id}"}
        # 201 for a new pass, 200 when this pass was already uploaded (nothing changes).
        return body if result.created else JSONResponse(body, status_code=200)

    async def _save(upload: UploadFile, path: Path, limit: int) -> int:
        size = 0
        with path.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    path.unlink(missing_ok=True)
                    raise HTTPException(413, f"{upload.filename} is larger than {limit // (1024 * 1024)} MB")
                out.write(chunk)
        return size

    @app.post("/api/v1/recordings", status_code=202)
    async def upload_recording(recording: Annotated[UploadFile, File(description="a whole .zip.acmi or .txt.acmi recording")],
                               debrief: Annotated[UploadFile | None, File(description="that session's debrief.log")] = None,
                               authorization: Annotated[str | None, Header()] = None):
        """Backfill: a whole recording's passes are sliced, graded and merged with the known landings (in
        the background; follow `status_url`, or the uploader's `page`). With a source's token: every pass.
        Without (unless the server requires tokens): one pilot's passes, picked from the pilots in the file
        (automatically if there's only one; otherwise POST it to `choose_url` with the `key`)."""
        if authorization:
            source_id, choose = source_from_token(authorization).id, False
        elif central.require_upload_token:
            raise HTTPException(401, "this server only accepts recordings uploaded with a source's token")
        else:
            source_id, choose = central.upload_source(), True
        name = Path(recording.filename or "recording.acmi").name
        if not name.lower().endswith(".acmi"):
            raise HTTPException(400, "expected a Tacview .acmi recording")
        incoming = central.uploads_dir / f"incoming-{uuid.uuid4().hex}.acmi"
        size = await _save(recording, incoming, MAX_RECORDING_BYTES)
        upload_id, key = central.add_upload(source_id, name, size, choose_pilot=choose)
        incoming.rename(central.upload_path(upload_id, name))
        if debrief is not None and debrief.filename:
            await _save(debrief, central.debrief_path(upload_id), MAX_DEBRIEF_BYTES)
        uploads.submit(_inspect_then_process if choose else central.process_upload, upload_id)
        return JSONResponse({"upload_id": upload_id, "key": key, "status_url": f"/api/v1/recordings/{upload_id}",
                             "choose_url": f"/api/v1/recordings/{upload_id}/pilot",
                             "page": f"/uploads/{upload_id}?key={key}"}, status_code=202)

    def _inspect_then_process(upload_id: int) -> None:
        if central.inspect_upload(upload_id):
            central.process_upload(upload_id)

    def _choose(upload_id: int, key: str, pilot: str) -> None:
        try:
            central.choose_pilot(upload_id, key, pilot)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        uploads.submit(central.process_upload, upload_id)

    @app.post("/api/v1/recordings/{upload_id}/pilot", status_code=202)
    def choose_pilot(upload_id: int, key: Annotated[str, Form()], pilot: Annotated[str, Form()]) -> dict:
        """Pick whose passes to import from a token-less upload with several pilots."""
        _choose(upload_id, key, pilot)
        return {"upload_id": upload_id, "status_url": f"/api/v1/recordings/{upload_id}"}

    @app.post("/uploads/{upload_id}/pilot")
    def choose_pilot_form(upload_id: int, key: Annotated[str, Form()], pilot: Annotated[str, Form()]):
        _choose(upload_id, key, pilot)
        return RedirectResponse(f"/uploads/{upload_id}?key={key}", status_code=303)

    def _upload(upload_id: int) -> Upload:
        with central.sessions() as s:
            upload = s.scalar(select(Upload).where(Upload.id == upload_id).options(selectinload(Upload.source)))
        if upload is None:
            raise HTTPException(404, "no such upload")
        return upload

    @app.get("/api/v1/recordings/{upload_id}")
    def upload_status(upload_id: int) -> dict:
        u = _upload(upload_id)
        return {"upload_id": u.id, "filename": u.filename, "size": u.size, "source": u.source.name,
                "pilots": u.pilots or [], "pilot": u.pilot,
                "status": u.status, "message": u.message, "results": u.results or []}

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form() -> str:
        return pages.upload_page(token_required=central.require_upload_token)

    @app.get("/uploads/{upload_id}", response_class=HTMLResponse)
    def upload_detail(upload_id: int, key: str | None = None) -> str:
        upload = _upload(upload_id)
        owner = key is not None and upload.key is not None and secrets.compare_digest(upload.key, key)
        return pages.upload_status_page(upload, key if owner else None)

    def _passes(days: int, pilot: str | None, source: str | None) -> list[Pass]:
        with central.sessions() as s:
            # Landings only: reports merged into another (same landing) and lone track reports are hidden.
            q = (select(Pass).where(Pass.merged_into_id.is_(None), or_(Pass.kind.is_(None), Pass.kind != "track"))
                 .options(selectinload(Pass.grades), selectinload(Pass.pilot), selectinload(Pass.source)))
            if days:
                q = q.where(Pass.occurred_at >= datetime.now(UTC) - timedelta(days=days))
            if pilot:
                q = q.join(Pass.pilot).where(Pilot.name == pilot)
            if source:
                # A landing belongs to every source that reported it.
                theirs = select(Pass.merged_into_id).join(Pass.source).where(Source.name == source)
                q = q.join(Pass.source).where(or_(Source.name == source, Pass.id.in_(theirs)))
            return list(s.scalars(q))

    @app.get("/api/v1/config")
    def get_config(source: Annotated[Source, Depends(source_from_token)]) -> dict:
        """Settings for this source's collector (callouts etc.)."""
        return source.config or {}

    @app.get("/api/v1/passes")
    def list_passes(days: int = 30, pilot: str | None = None, source: str | None = None) -> list[dict]:
        out = []
        for p in _passes(days, pilot, source):
            g = p.grade
            reports = central.reports(p)
            out.append({"id": p.id, "pilot": p.pilot.name, "source": p.source.name,
                        "reports": [{"id": r.id, "source": r.source.name, "kind": r.kind or "pass"} for r in reports],
                        "occurred_at": p.occurred_at.isoformat() if p.occurred_at else None,
                        "mission": p.mission, "carrier": p.carrier_unit, "aircraft": p.aircraft_type,
                        "outcome": p.outcome, "wire": p.wire, "dcs_grade": p.dcs_grade, "calls": p.calls,
                        # DCS's wire (above) takes priority; this is estimated from where the jet stopped.
                        "wire_estimated": ((g.detail or {}).get("wire_estimate") if g else None),
                        "grade": g.grade if g else None, "text": g.text if g else None,
                        "points": g.points if g else None, "grading_version": g.version if g else None})
        return out

    @app.get("/api/v1/pilots/{name}/trends")
    def pilot_trends_api(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES) -> dict:
        """Themes across a pilot's recent passes: what keeps going wrong, biases, speed, outcomes, wires."""
        found = central.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        return {"pilot": name, "landings": found.landings,
                "first_seen": found.first_seen.isoformat() if found.first_seen else None,
                "last_seen": found.last_seen.isoformat() if found.last_seen else None,
                **found.trends.to_dict(), "pass_ids": [p.id for p in found.rows]}

    @app.get("/pilots/{name}", response_class=HTMLResponse)
    def pilot_page(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES) -> str:
        found = central.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        items = central.overlay(found.rows)
        svg = render_overlay(items, uid="ov", title=f"{name}: last {len(items)} passes overlaid") if items else None
        return pages.pilot_page(name, found, passes, svg, overlay_src=f"/pilots/{quote(name, safe='')}/overlay.svg?passes={passes}")

    @app.get("/pilots/{name}/overlay.svg")
    def pilot_overlay(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES,
                      near: float | None = None, far: float | None = None) -> Response:
        """The pilot's passes overlaid; with `near`/`far` (meters short of the aim point), zoomed to that
        stretch of the approach."""
        found = central.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        view = None
        if near is not None and far is not None:
            near, far = max(min(near, far), OVERLAY_MIN_M), min(max(near, far), OVERLAY_MAX_M)
            if far - near < MIN_ZOOM_M:
                near, far = (near + far - MIN_ZOOM_M) / 2, (near + far + MIN_ZOOM_M) / 2
            view = (near, far)
        svg = render_overlay(central.overlay(found.rows), uid="ov", title=f"{name}: passes overlaid", view=view)
        return Response(svg, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def board(days: Annotated[int, Query(ge=0)] = 30, pilot: str | None = None, source: str | None = None) -> str:
        passes = _passes(days, pilot or None, source or None)
        with central.sessions() as s:
            pilots = sorted(s.scalars(select(Pilot.name)), key=str.lower)
            sources = sorted(s.scalars(select(Source.name)), key=str.lower)
        return pages.board_page(passes, pilots, sources, days, pilot, source, BOARD_COLUMNS)

    def _get(pass_id: int) -> Pass:
        with central.sessions() as s:
            q = select(Pass).where(Pass.id == pass_id).options(
                selectinload(Pass.grades), selectinload(Pass.pilot), selectinload(Pass.source),
                selectinload(Pass.slice))
            p = s.scalar(q)
        if p is None:
            raise HTTPException(404, "no such pass")
        return p

    @app.get("/passes/{pass_id}", response_class=HTMLResponse)
    def pass_detail(pass_id: int) -> str:
        p = _get(pass_id)
        if p.merged_into_id is not None:
            return RedirectResponse(f"/passes/{p.merged_into_id}", status_code=307)
        reports = central.reports(p)
        try:
            result = central.load_pass(p, reports)
            svg = render_card(result, grade_pass(result), p.mission or "", uid=f"p{p.id}", calls=p.calls)
            return pages.pass_page(p, svg, reports=reports, track_source=result.track_source)
        except (IngestError, OSError) as exc:
            return pages.pass_page(p, None, f"Trap card unavailable: {exc}", reports=reports)

    @app.get("/passes/{pass_id}/card.svg")
    def pass_card(pass_id: int) -> Response:
        """The pass's trap card on its own (e.g. previewed on hover)."""
        p = _get(pass_id)
        if p.merged_into_id is not None:
            return RedirectResponse(f"/passes/{p.merged_into_id}/card.svg", status_code=307)
        try:
            result = central.load_pass(p)
        except (IngestError, OSError) as exc:
            raise HTTPException(404, f"trap card unavailable: {exc}") from exc
        svg = render_card(result, grade_pass(result), p.mission or "", uid=f"hover{p.id}", calls=p.calls)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})

    @app.get("/passes/{pass_id}/acmi")
    def pass_acmi(pass_id: int) -> FileResponse:
        p = _get(pass_id)
        stamp = p.occurred_at.strftime("%Y%m%d-%H%M%S") if p.occurred_at else f"pass{p.id}"
        name = "".join(c if c.isalnum() else "_" for c in p.pilot.name)
        return FileResponse(central.store.path(p.slice.sha256), media_type="application/zip",
                            filename=f"{stamp}_{name}_{p.id}.zip.acmi")

    return app
