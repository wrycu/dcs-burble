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

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from ..cards import render_card
from ..cards.overlay import render_overlay
from ..cards.svg import X_MAX_M as OVERLAY_MAX_M, X_MIN_M as OVERLAY_MIN_M
from ..grading import grade_pass
from ..grading.trends import DEFAULT_PASSES
from . import pages
from .db import Pass, Pilot, Source, Upload
from .service import Hub, IngestError

MAX_SLICE_BYTES = 20 * 1024 * 1024
MAX_RECORDING_BYTES = 1024 * 1024 * 1024  # a whole session's Tacview recording (backfill)
MAX_DEBRIEF_BYTES = 50 * 1024 * 1024
BOARD_COLUMNS = 20
MIN_ZOOM_M = 15.0  # the narrowest stretch the overlay zooms to


def _view(near: float | None, far: float | None) -> tuple[float, float] | None:
    """A zoomed view of the approach from `near`/`far` (meters short of the aim point), kept within the chart and
    at least MIN_ZOOM_M wide; None for the whole approach."""
    if near is None or far is None:
        return None
    near, far = max(min(near, far), OVERLAY_MIN_M), min(max(near, far), OVERLAY_MAX_M)
    if far - near < MIN_ZOOM_M:
        near, far = (near + far - MIN_ZOOM_M) / 2, (near + far + MIN_ZOOM_M) / 2
    return near, far


def _client_ip(request: Request) -> str | None:
    """The address a request came from. Behind a reverse proxy on the same machine, uvicorn already puts the
    client's address here (from X-Forwarded-For, trusted only from 127.0.0.1 by default)."""
    return request.client.host if request.client else None


def create_app(hub: Hub) -> FastAPI:
    app = FastAPI(title="dcs-lso hub", docs_url="/api/docs", redoc_url=None)
    # Uploaded recordings are processed one at a time, off the request threads.
    uploads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload")

    def source_from_token(authorization: Annotated[str | None, Header()] = None) -> Source:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        source = hub.authenticate(authorization.removeprefix("Bearer ").strip())
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
            result = hub.ingest(source.id, data, meta)
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

    @app.post("/api/v1/pilot-hook/approaches")
    async def pilot_hook_approach(request: Request, authorization: Annotated[str | None, Header()] = None):
        """One approach from the pilot hook (JSON, see `hub.pilothook`). Plain HTTP is fine: DCS's Lua has no
        HTTPS. Accepted from a player on this hub's servers (by UCID and address, see
        `Hub.pilot_hook_access`), or with the pilot's token in the Authorization header or the body."""
        raw = await request.body()
        if len(raw) > MAX_SLICE_BYTES:
            raise HTTPException(413, "upload too large")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        token = (authorization or "").removeprefix("Bearer ").strip() or str(body.get("token") or "") or None
        try:
            source_id, pilot = hub.pilot_hook_access(token, body.get("ucid"), _client_ip(request))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(403, str(exc)) from exc
        try:
            results = await run_in_threadpool(hub.ingest_pilot_hook, source_id, body, pilot)
        except IngestError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"reports": [{"pass_id": r.pass_id, "created": r.created, "grade": r.grade, "text": r.text,
                             "url": f"/passes/{r.pass_id}"} for r in results]}

    @app.get("/api/v1/pilot-hook/here")
    def pilot_hook_here(request: Request, ucid: str = "") -> dict:
        """Is this player (UCID) on one of this hub's servers now? Answered only for the player's own address
        (or the LAN), so it can't be used to look players up."""
        return {"here": hub.pilot_hook_here(ucid, _client_ip(request)), "accept": hub.pilot_hook_accept}

    @app.post("/api/v1/players")
    def players(source: Annotated[Source, Depends(source_from_token)], body: Annotated[dict, Body()]) -> dict:
        """A server agent's list of players connected to its DCS server: [{"ucid", "ip", "name"}]."""
        listed = body.get("players")
        if not isinstance(listed, list):
            raise HTTPException(400, "expected {\"players\": [...]}")
        try:
            hub.report_players(source.id, listed)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"players": len(listed)}

    @app.post("/api/v1/recordings", status_code=202)
    async def upload_recording(recording: Annotated[UploadFile, File(description="a whole .zip.acmi or .txt.acmi recording")],
                               debrief: Annotated[UploadFile | None, File(description="that session's debrief.log")] = None,
                               password: Annotated[str | None, Form(description="without a token: the pilot's password, "
                                                                                "if they've set one")] = None,
                               authorization: Annotated[str | None, Header()] = None):
        """Backfill: a whole recording's passes are sliced, graded and merged with the known landings (in
        the background; follow `status_url`, or the uploader's `page`). With a source's token: every pass.
        Without (unless the server requires tokens): one pilot's passes, picked from the pilots in the file
        (automatically if there's only one; otherwise POST it to `choose_url` with the `key`)."""
        if authorization:
            source_id, choose = source_from_token(authorization).id, False
        elif hub.require_upload_token:
            raise HTTPException(401, "this server only accepts recordings uploaded with a source's token")
        else:
            source_id, choose = hub.upload_source(), True
        name = Path(recording.filename or "recording.acmi").name
        if not name.lower().endswith(".acmi"):
            raise HTTPException(400, "expected a Tacview .acmi recording")
        incoming = hub.uploads_dir / f"incoming-{uuid.uuid4().hex}.acmi"
        size = await _save(recording, incoming, MAX_RECORDING_BYTES)
        upload_id, key = hub.add_upload(source_id, name, size, choose_pilot=choose)
        incoming.rename(hub.upload_path(upload_id, name))
        if debrief is not None and debrief.filename:
            await _save(debrief, hub.debrief_path(upload_id), MAX_DEBRIEF_BYTES)
        if choose:
            hub.remember_upload_password(upload_id, password)
        uploads.submit(_inspect_then_process if choose else hub.process_upload, upload_id)
        return JSONResponse({"upload_id": upload_id, "key": key, "status_url": f"/api/v1/recordings/{upload_id}",
                             "choose_url": f"/api/v1/recordings/{upload_id}/pilot",
                             "page": f"/uploads/{upload_id}?key={key}"}, status_code=202)

    def _inspect_then_process(upload_id: int) -> None:
        if hub.inspect_upload(upload_id):
            hub.process_upload(upload_id)

    def _queue_if(step) -> bool:
        """Run an uploader's step (choosing a pilot, giving a password); process the upload if it's ready."""
        try:
            ready = step()
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return ready

    @app.post("/api/v1/recordings/{upload_id}/pilot", status_code=202)
    def choose_pilot(upload_id: int, key: Annotated[str, Form()], pilot: Annotated[str, Form()],
                     password: Annotated[str | None, Form()] = None) -> dict:
        """Pick whose passes to import from a token-less upload with several own pilots (with their
        password, if they've set one)."""
        if _queue_if(lambda: hub.choose_pilot(upload_id, key, pilot, password)):
            uploads.submit(hub.process_upload, upload_id)
        return {"upload_id": upload_id, "status_url": f"/api/v1/recordings/{upload_id}"}

    @app.post("/uploads/{upload_id}/pilot")
    def choose_pilot_form(upload_id: int, key: Annotated[str, Form()], pilot: Annotated[str, Form()],
                          password: Annotated[str | None, Form()] = None):
        if _queue_if(lambda: hub.choose_pilot(upload_id, key, pilot, password)):
            uploads.submit(hub.process_upload, upload_id)
        return RedirectResponse(f"/uploads/{upload_id}?key={key}", status_code=303)

    @app.post("/api/v1/recordings/{upload_id}/password", status_code=202)
    def give_password(upload_id: int, key: Annotated[str, Form()], password: Annotated[str, Form()]) -> dict:
        """The pilot's password, for a token-less upload of a pilot who has set one."""
        if _queue_if(lambda: hub.give_upload_password(upload_id, key, password)):
            uploads.submit(hub.process_upload, upload_id)
        return {"upload_id": upload_id, "status_url": f"/api/v1/recordings/{upload_id}"}

    @app.post("/uploads/{upload_id}/password")
    def give_password_form(upload_id: int, key: Annotated[str, Form()], password: Annotated[str, Form()]):
        if _queue_if(lambda: hub.give_upload_password(upload_id, key, password)):
            uploads.submit(hub.process_upload, upload_id)
        return RedirectResponse(f"/uploads/{upload_id}?key={key}", status_code=303)

    # -- pilot settings (password, side number) ---------------------------------------------------

    def _pilot_step(step) -> None:
        try:
            step()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/join", response_class=HTMLResponse)
    def join() -> str:
        return pages.join_page()

    @app.post("/join", response_class=HTMLResponse)
    def join_form(name: Annotated[str, Form()], password: Annotated[str, Form()], confirm: Annotated[str, Form()]):
        if password != confirm:
            return HTMLResponse(pages.join_page("the two passwords differ", name), status_code=400)
        try:
            stored = hub.register_pilot(name, password)
        except FileExistsError as exc:
            return HTMLResponse(pages.join_page(name=name, existing=str(exc)), status_code=409)
        except (PermissionError, ValueError) as exc:
            return HTMLResponse(pages.join_page(str(exc), name), status_code=400)
        done = quote("Welcome aboard! You can now create pilot tokens and claim other names.")
        return RedirectResponse(f"/pilots/{quote(stored, safe='')}/settings?done={done}", status_code=303)

    @app.post("/api/v1/pilots", status_code=201)
    def register_api(name: Annotated[str, Form()], password: Annotated[str, Form()]) -> dict:
        """Join the board: claim a pilot name with a password."""
        try:
            stored = hub.register_pilot(name, password)
        except FileExistsError as exc:
            raise HTTPException(409, f"{exc} is already on this board: set the password on their settings page") from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"pilot": stored}

    @app.post("/api/v1/pilots/{name}/password")
    def pilot_password(name: str, new: Annotated[str, Form()], current: Annotated[str | None, Form()] = None) -> dict:
        """Set a pilot's password (the first time, this claims the name) or change it (needs `current`)."""
        _pilot_step(lambda: hub.set_pilot_password(name, new, current))
        return {"pilot": name, "password": "set"}

    @app.post("/api/v1/pilots/{name}/modex")
    def pilot_modex(name: str, password: Annotated[str, Form()], modex: Annotated[str, Form()]) -> dict:
        """A pilot with a password changes their side number."""
        _pilot_step(lambda: hub.set_pilot_modex(name, password, modex))
        return {"pilot": name, "modex": modex.strip()}

    def _settings_page(name: str, done: str | None = None, error: str | None = None,
                       new_token: str | None = None) -> str:
        with hub.sessions() as s:
            pilot = s.scalar(select(Pilot).where(Pilot.name == name))
        if pilot is None:
            raise HTTPException(404, "no such pilot")
        return pages.pilot_settings_page(pilot, done, error, hub.pilot_tokens(name), hub.pilot_aliases(name), new_token)

    @app.get("/pilots/{name}/settings", response_class=HTMLResponse)
    def pilot_settings(name: str, done: str | None = None, error: str | None = None) -> str:
        return _settings_page(name, done, error)

    @app.post("/api/v1/pilots/{name}/tokens", status_code=201)
    def pilot_token_api(name: str, password: Annotated[str, Form()], label: Annotated[str, Form()] = "") -> dict:
        """A pilot creates a pilot token with their password (e.g. for the pilot hook's settings)."""
        token: list[str] = []
        _pilot_step(lambda: token.append(hub.create_pilot_token(name, password, label)))
        return {"pilot": name, "token": token[0]}

    @app.post("/pilots/{name}/settings/tokens", response_class=HTMLResponse)
    def pilot_token_form(name: str, password: Annotated[str, Form()], label: Annotated[str, Form()] = ""):
        # Rendered directly (not redirected), so the new token is never in a URL.
        try:
            token = hub.create_pilot_token(name, password, label)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            return HTMLResponse(_settings_page(name, error=str(exc)), status_code=403)
        return _settings_page(name, done="Pilot token created.", new_token=token)

    @app.post("/pilots/{name}/settings/tokens/{token_id}/revoke")
    def pilot_token_revoke(name: str, token_id: int, password: Annotated[str, Form()]):
        return _settings_redirect(name, lambda: hub.revoke_pilot_token(name, password, token_id), "Token revoked.")

    @app.post("/pilots/{name}/settings/aliases")
    def pilot_alias_form(name: str, password: Annotated[str, Form()], alias: Annotated[str, Form()]):
        moved: list[int] = []
        step = lambda: moved.append(hub.claim_alias(name, password, alias))  # noqa: E731
        quoted = quote(name, safe="")
        try:
            _pilot_step(step)
        except HTTPException as exc:
            return RedirectResponse(f"/pilots/{quoted}/settings?error={quote(str(exc.detail))}", status_code=303)
        done = f"{alias.strip()} is now one of your names" + (f"; {moved[0]} passes moved to you." if moved[0] else ".")
        return RedirectResponse(f"/pilots/{quoted}/settings?done={quote(done)}", status_code=303)

    def _settings_redirect(name: str, step, done: str):
        quoted = quote(name, safe="")
        try:
            _pilot_step(step)
        except HTTPException as exc:
            return RedirectResponse(f"/pilots/{quoted}/settings?error={quote(str(exc.detail))}", status_code=303)
        return RedirectResponse(f"/pilots/{quoted}/settings?done={quote(done)}", status_code=303)

    @app.post("/pilots/{name}/settings/password")
    def pilot_password_form(name: str, new: Annotated[str, Form()], confirm: Annotated[str, Form()],
                            current: Annotated[str | None, Form()] = None):
        if new != confirm:
            return RedirectResponse(f"/pilots/{quote(name, safe='')}/settings?error={quote('the two new passwords differ')}",
                                    status_code=303)
        return _settings_redirect(name, lambda: hub.set_pilot_password(name, new, current), "Password saved.")

    @app.post("/pilots/{name}/settings/modex")
    def pilot_modex_form(name: str, password: Annotated[str, Form()], modex: Annotated[str, Form()]):
        return _settings_redirect(name, lambda: hub.set_pilot_modex(name, password, modex), "Side number saved.")

    def _upload(upload_id: int) -> Upload:
        with hub.sessions() as s:
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
        return pages.upload_page(token_required=hub.require_upload_token)

    @app.get("/uploads/{upload_id}", response_class=HTMLResponse)
    def upload_detail(upload_id: int, key: str | None = None) -> str:
        upload = _upload(upload_id)
        owner = key is not None and upload.key is not None and secrets.compare_digest(upload.key, key)
        return pages.upload_status_page(upload, key if owner else None)

    def _passes(days: int, pilot: str | None, source: str | None) -> list[Pass]:
        with hub.sessions() as s:
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
        """Settings for this source's agent (callouts etc.)."""
        return source.config or {}

    @app.get("/api/v1/passes")
    def list_passes(days: int = 30, pilot: str | None = None, source: str | None = None) -> list[dict]:
        out = []
        for p in _passes(days, pilot, source):
            g = p.grade
            reports = hub.reports(p)
            out.append({"id": p.id, "pilot": p.pilot.name, "source": p.source.name,
                        "reports": [{"id": r.id, "source": r.source.name, "kind": r.kind or "pass"} for r in reports],
                        "occurred_at": p.occurred_at.isoformat() if p.occurred_at else None,
                        "mission": p.mission, "carrier": p.carrier_unit, "aircraft": p.aircraft_type,
                        "livery": p.livery, "modex": p.pilot.modex,
                        "outcome": p.outcome, "wire": p.wire, "dcs_grade": p.dcs_grade, "calls": p.calls,
                        # DCS's wire (above) takes priority; this is estimated from where the jet stopped.
                        "wire_estimated": ((g.detail or {}).get("wire_estimate") if g else None),
                        "grade": g.grade if g else None, "text": g.text if g else None,
                        "points": g.points if g else None, "grading_version": g.version if g else None})
        return out

    @app.get("/api/v1/pilots/{name}/trends")
    def pilot_trends_api(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES) -> dict:
        """Themes across a pilot's recent passes: what keeps going wrong, biases, speed, outcomes, wires."""
        found = hub.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        return {"pilot": name, "modex": found.modex, "last_livery": found.last_livery, "landings": found.landings,
                "first_seen": found.first_seen.isoformat() if found.first_seen else None,
                "last_seen": found.last_seen.isoformat() if found.last_seen else None,
                **found.trends.to_dict(), "pass_ids": [p.id for p in found.rows]}

    @app.get("/pilots/{name}", response_class=HTMLResponse)
    def pilot_page(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES) -> str:
        found = hub.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        items = hub.overlay(found.rows)
        svg = render_overlay(items, uid="ov", title=f"{name}: last {len(items)} passes overlaid") if items else None
        return pages.pilot_page(name, found, passes, svg, overlay_src=f"/pilots/{quote(name, safe='')}/overlay.svg?passes={passes}")

    @app.get("/pilots/{name}/overlay.svg")
    def pilot_overlay(name: str, passes: Annotated[int, Query(ge=3, le=50)] = DEFAULT_PASSES,
                      near: float | None = None, far: float | None = None) -> Response:
        """The pilot's passes overlaid; with `near`/`far` (meters short of the aim point), zoomed to that
        stretch of the approach."""
        found = hub.pilot_trends(name, passes)
        if found is None:
            raise HTTPException(404, "no such pilot")
        svg = render_overlay(hub.overlay(found.rows), uid="ov", title=f"{name}: passes overlaid", view=_view(near, far))
        return Response(svg, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def board(days: Annotated[int, Query(ge=0)] = 30, pilot: str | None = None, source: str | None = None) -> str:
        passes = _passes(days, pilot or None, source or None)
        with hub.sessions() as s:
            pilots = sorted(s.scalars(select(Pilot.name)), key=str.lower)
            sources = sorted(s.scalars(select(Source.name)), key=str.lower)
        return pages.board_page(passes, pilots, sources, days, pilot, source, BOARD_COLUMNS)

    def _get(pass_id: int) -> Pass:
        with hub.sessions() as s:
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
        reports = hub.reports(p)
        try:
            result = hub.load_pass(p, reports)
            svg = render_card(result, grade_pass(result), pages.card_title(p), uid=f"p{p.id}", calls=p.calls,
                              night=bool(p.night), zoom_hint=True)
            return pages.pass_page(p, svg, reports=reports, track_source=result.track_source)
        except (IngestError, OSError) as exc:
            return pages.pass_page(p, None, f"Trap card unavailable: {exc}", reports=reports)

    @app.get("/passes/{pass_id}/card.svg")
    def pass_card(pass_id: int, request: Request, near: float | None = None, far: float | None = None,
                  zoom: bool = False) -> Response:
        """The pass's trap card on its own (e.g. previewed on hover); with `near`/`far` (meters short of the aim
        point), zoomed to that stretch of the approach. `zoom`: for the pass page, which zooms by dragging."""
        p = _get(pass_id)
        if p.merged_into_id is not None:
            query = f"?{request.url.query}" if request.url.query else ""  # keep the zoom
            return RedirectResponse(f"/passes/{p.merged_into_id}/card.svg{query}", status_code=307)
        try:
            result = hub.load_pass(p)
        except (IngestError, OSError) as exc:
            raise HTTPException(404, f"trap card unavailable: {exc}") from exc
        view = _view(near, far)
        svg = render_card(result, grade_pass(result), pages.card_title(p), uid=f"p{p.id}" if zoom else f"hover{p.id}",
                          calls=p.calls, night=bool(p.night), view=view, zoom_hint=zoom)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})

    @app.get("/passes/{pass_id}/acmi")
    def pass_acmi(pass_id: int) -> FileResponse:
        p = _get(pass_id)
        stamp = p.occurred_at.strftime("%Y%m%d-%H%M%S") if p.occurred_at else f"pass{p.id}"
        name = "".join(c if c.isalnum() else "_" for c in p.pilot.name)
        return FileResponse(hub.store.path(p.slice.sha256), media_type="application/zip",
                            filename=f"{stamp}_{name}_{p.id}.zip.acmi")

    return app
