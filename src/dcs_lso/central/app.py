"""FastAPI app: ingest API, greenie board, pass pages, ACMI downloads."""

# No `from __future__ import annotations` here: FastAPI must see the real annotation
# objects to resolve dependencies defined inside create_app().
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..cards import render_card
from ..grading import grade_pass
from . import pages
from .db import Pass, Pilot, Source
from .service import Central, IngestError

MAX_SLICE_BYTES = 20 * 1024 * 1024
BOARD_COLUMNS = 20


def create_app(central: Central) -> FastAPI:
    app = FastAPI(title="dcs-lso central", docs_url="/api/docs", redoc_url=None)

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

    def _passes(days: int, pilot: str | None, source: str | None) -> list[Pass]:
        with central.sessions() as s:
            q = select(Pass).options(selectinload(Pass.grades), selectinload(Pass.pilot), selectinload(Pass.source))
            if days:
                q = q.where(Pass.occurred_at >= datetime.now(UTC) - timedelta(days=days))
            if pilot:
                q = q.join(Pass.pilot).where(Pilot.name == pilot)
            if source:
                q = q.join(Pass.source).where(Source.name == source)
            return list(s.scalars(q))

    @app.get("/api/v1/passes")
    def list_passes(days: int = 30, pilot: str | None = None, source: str | None = None) -> list[dict]:
        out = []
        for p in _passes(days, pilot, source):
            g = p.grade
            out.append({"id": p.id, "pilot": p.pilot.name, "source": p.source.name,
                        "occurred_at": p.occurred_at.isoformat() if p.occurred_at else None,
                        "mission": p.mission, "carrier": p.carrier_unit, "aircraft": p.aircraft_type,
                        "outcome": p.outcome, "wire": p.wire, "dcs_grade": p.dcs_grade,
                        "grade": g.grade if g else None, "text": g.text if g else None,
                        "points": g.points if g else None, "grading_version": g.version if g else None})
        return out

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
        try:
            result = central.load_pass(p)
            svg = render_card(result, grade_pass(result), p.mission or "", uid=f"p{p.id}")
            return pages.pass_page(p, svg)
        except (IngestError, OSError) as exc:
            return pages.pass_page(p, None, f"Trap card unavailable: {exc}")

    @app.get("/passes/{pass_id}/acmi")
    def pass_acmi(pass_id: int) -> FileResponse:
        p = _get(pass_id)
        stamp = p.occurred_at.strftime("%Y%m%d-%H%M%S") if p.occurred_at else f"pass{p.id}"
        name = "".join(c if c.isalnum() else "_" for c in p.pilot.name)
        return FileResponse(central.store.path(p.slice.sha256), media_type="application/zip",
                            filename=f"{stamp}_{name}_{p.id}.zip.acmi")

    return app
