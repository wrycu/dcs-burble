"""The receiver's web app (`burble telemetry serve`): takes hubs' usage reports, and shows what they add up to on
its Usage page (behind its password) and as JSON at `/api/v1/usage` (an API key or the password)."""

from __future__ import annotations

import base64
import binascii
import json
from html import escape
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .pages import STYLE, usage_body
from .store import MAX_REPORT_BYTES, ReportError, Store, TooSoon

PAGE_STYLE = """
:root { color-scheme: light dark; --bg: #f6f8fa; --card: #ffffff; --text: #1f2328; --muted: #59636e; --border: #d1d9e0; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --card: #151b23; --text: #e6edf3; --muted: #9198a1; --border: #3d444d; }
}
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
.sub { color: var(--muted); }
"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
            f"<style>{PAGE_STYLE}{STYLE}</style></head><body><main>{body}</main></body></html>")


def _basic_password(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Basic "):
        return ""
    try:
        return base64.b64decode(authorization[6:]).decode("utf-8").partition(":")[2]
    except (binascii.Error, UnicodeDecodeError):
        return ""


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="burble usage reports")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/api/v1/telemetry", status_code=201)
    async def receive(request: Request) -> dict:
        if int(request.headers.get("content-length") or 0) > MAX_REPORT_BYTES:
            raise HTTPException(413, f"a report is at most {MAX_REPORT_BYTES // 1024} KB")
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_REPORT_BYTES:
                raise HTTPException(413, f"a report is at most {MAX_REPORT_BYTES // 1024} KB")
        try:
            body = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "not JSON") from None
        try:
            await run_in_threadpool(store.receive, body)
        except ReportError as exc:
            raise HTTPException(400, str(exc)) from exc
        except TooSoon:
            raise HTTPException(429, "one report an hour is plenty") from None
        return {"ok": True}

    @app.get("/api/v1/usage")
    def usage(authorization: Annotated[str | None, Header()] = None) -> dict:
        secret = (authorization or "").removeprefix("Bearer ").strip()
        if not store.allowed(secret):
            raise HTTPException(401, "an API key or the receiver's password is needed",
                                headers={"WWW-Authenticate": "Bearer"})
        return store.usage()

    @app.get("/", response_class=HTMLResponse)
    def page(authorization: Annotated[str | None, Header()] = None) -> HTMLResponse:
        if not store.check_password(_basic_password(authorization)):
            return HTMLResponse(
                _page("Usage", "<h1>Usage</h1><p>Sign in with the receiver's password (any user name). It's set with "
                               "<code>burble telemetry set-password</code> on the receiver's machine.</p>"),
                status_code=401, headers={"WWW-Authenticate": 'Basic realm="burble usage", charset="UTF-8"'})
        return HTMLResponse(_page("Usage", "<h1>Usage</h1>" + usage_body(store.usage())))

    return app
