"""Edge collector: live Tacview stream -> session archive -> per-pass slices -> central.

Runs next to DCS. For each stream connection (a "session") it:
- writes the raw stream to `archive/<start>.txt.acmi` (zipped when the session ends);
- detects passes live; `TAIL_S` after a pass ends, slices it from the archive with the
  same code as `dcs-lso slice`, attaches DCS's grade and wire from the dcs-lso hook's
  events in dcs.log, and queues slice + sidecar in the outbox;
- uploads the outbox to central, retrying until accepted;
- at session end, reads debrief.log and fills in DCS grades the hook didn't provide.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..acmi import AcmiParser, Frame, load_recording
from ..acmi.stream import DEFAULT_PORT, HandshakeError, TelemetryClient
from ..acmi.writer import write_slice
from ..dcslog import Debrief, DcsEvent, HookEvent, attach_dcs_grades, follow, load_debrief, parse_hook_line
from ..detect import PassResult, find_passes
from ..slices import LEAD_S, TAIL_S, sidecar, slice_name, slice_objects
from .live import LivePassDetector
from .outbox import Outbox

log = logging.getLogger(__name__)

RECONNECT_MIN_S, RECONNECT_MAX_S = 2.0, 30.0
UPLOAD_INTERVAL_S = 5.0
DEBRIEF_WAIT_S = 60.0
MATCH_START_TOLERANCE_S = 5.0
# Warn when a connection delivers no frames for this long (Tacview's exporter isn't
# getting data from DCS, e.g. Export.lua lost its Tacview line, or the sim is paused).
NO_FRAMES_WARNING_S = 30.0


@dataclass
class CollectorConfig:
    work_dir: Path
    tacview_host: str = "127.0.0.1"
    tacview_port: int = DEFAULT_PORT
    tacview_password: str | None = None
    dcs_log: Path | None = None
    debrief: Path | None = None
    url: str | None = None
    token: str | None = None


class HookFeed:
    """Follows dcs.log in a background thread and keeps the dcs-lso hook's events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._events: list[HookEvent] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="dcs-log-follower", daemon=True).start()

    def _run(self) -> None:
        for line in follow(self.path):
            if (event := parse_hook_line(line)) is not None:
                with self._lock:
                    self._events.append(event)
                if event.event == "landing_quality_mark":
                    log.info("DCS LSO: %s", event.comment)

    def add(self, event: HookEvent) -> None:
        with self._lock:
            self._events.append(event)

    def debrief(self) -> Debrief:
        """The hook's landing grades in debrief.log form, for `attach_dcs_grades`."""
        with self._lock:
            events = list(self._events)
        return Debrief(None, [to_dcs_event(e) for e in events if e.event == "landing_quality_mark"])


def to_dcs_event(e: HookEvent) -> DcsEvent:
    who, place = e.initiator or {}, e.place or {}
    return DcsEvent(type="landing quality mark", time=e.time or 0.0, place=place.get("name"),
                    initiator_pilot=who.get("player") or who.get("name"), initiator_unit_type=who.get("type"),
                    initiator_object_id=who.get("object_id"), comment=e.comment)


class SessionArchive:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.started = datetime.now(UTC)
        self.path = directory / f"{self.started:%Y%m%d-%H%M%S}-session.txt.acmi"
        self._fh = self.path.open("w", encoding="utf-8", newline="")

    def write(self, line: str) -> None:
        self._fh.write(line if line.endswith("\n") else line + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> Path:
        """Close and compress; returns the `.zip.acmi` path."""
        self._fh.close()
        zipped = self.path.with_name(self.path.name.removesuffix(".txt.acmi") + ".zip.acmi")
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.path, self.path.name)
        self.path.unlink()
        return zipped


@dataclass
class _Pending:
    result: PassResult
    objects: set[int]
    due: float  # sim time at which the slice window is complete


@dataclass
class Session:
    archive: SessionArchive
    parser: AcmiParser = field(default_factory=AcmiParser)
    detector: LivePassDetector = field(default_factory=LivePassDetector)
    first_frame: float | None = None
    now: float = 0.0
    pending: list[_Pending] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # outbox items produced by this session
    frames: int = 0


class Collector:
    def __init__(self, config: CollectorConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.outbox = Outbox(config.work_dir / "outbox")
        self.archive_dir = config.work_dir / "archive"
        self.hooks = HookFeed(config.dcs_log) if config.dcs_log else None
        if client is None and config.url and config.token:
            client = httpx.AsyncClient(base_url=config.url, headers={"Authorization": f"Bearer {config.token}"},
                                       timeout=60)
        self.client = client
        self._wake_uploader = asyncio.Event()

    # -- top level ----------------------------------------------------------------------------

    async def run_forever(self) -> None:
        uploader = asyncio.create_task(self.upload_loop())
        delay = RECONNECT_MIN_S
        try:
            while True:
                try:
                    await self.run_session()
                    delay = RECONNECT_MIN_S
                except (OSError, HandshakeError) as exc:
                    log.info("Tacview stream unavailable (%s); retrying in %.0fs", exc, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_S)
        finally:
            uploader.cancel()

    async def upload_loop(self) -> None:
        if self.client is None:
            log.warning("no central URL/token: passes stay in %s", self.outbox.pending_dir)
            return
        while True:
            await self.upload_once()
            try:
                await asyncio.wait_for(self._wake_uploader.wait(), UPLOAD_INTERVAL_S)
            except TimeoutError:
                pass
            self._wake_uploader.clear()

    async def upload_once(self) -> tuple[int, int]:
        if self.client is None:
            return 0, len(self.outbox.pending())
        return await self.outbox.upload_pending(self.client)

    # -- one stream connection ----------------------------------------------------------------

    async def run_session(self) -> Session:
        c = self.config
        client = TelemetryClient(c.tacview_host, c.tacview_port, password=c.tacview_password)
        info = await client.connect()
        session = Session(SessionArchive(self.archive_dir))
        log.info("connected to Tacview stream from %r; archiving to %s", info.name, session.archive.path)
        watchdog = asyncio.create_task(self._watch_frames(session))
        try:
            async for line in client.lines():
                await self._line(session, line)
        finally:
            watchdog.cancel()
            await client.close()
            for result in session.detector.flush():
                self._queue(session, result)
            for item in session.pending:
                await self._slice(session, item)
            session.pending.clear()
            archive = session.archive.close()
            log.info("session ended; archive saved as %s", archive)
            self._wake_uploader.set()
        if c.debrief is not None:
            asyncio.create_task(self._debrief_after(session))
        return session

    async def _watch_frames(self, session: Session) -> None:
        """Warn (repeatedly) while a connection delivers no frames."""
        last = -1
        while True:
            await asyncio.sleep(NO_FRAMES_WARNING_S)
            if session.frames == last:
                log.warning(
                    "connected to Tacview but no new frames for %.0fs. Is the mission paused? Does DCS's "
                    "Saved Games/DCS/Scripts/Export.lua load Scripts/TacviewGameExport.lua? (SRS's installer "
                    "can replace Export.lua without it.)", NO_FRAMES_WARNING_S)
            last = session.frames

    async def _line(self, session: Session, line: str) -> None:
        session.archive.write(line)
        for record in session.parser.feed(line):
            if isinstance(record, Frame):
                session.frames += 1
                session.now = record.time
                if session.first_frame is None:
                    session.first_frame = record.time
                due = [p for p in session.pending if p.due <= record.time]
                if due:
                    session.pending = [p for p in session.pending if p.due > record.time]
                    for item in due:
                        await self._slice(session, item)
            for result in session.detector.feed(record):
                self._queue(session, result)

    def _queue(self, session: Session, result: PassResult) -> None:
        objects = slice_objects(session.detector.recording(dict(session.parser.globals)), result)
        session.pending.append(_Pending(result, objects, result.end_time + TAIL_S))
        log.info("pass detected: %s %s, %s; slicing in %.0fs", result.pilot or hex(result.aircraft_id),
                 result.aircraft_type, result.outcome.value, TAIL_S)

    async def _slice(self, session: Session, item: _Pending) -> None:
        session.archive.flush()
        loop = asyncio.get_running_loop()
        try:
            name = await loop.run_in_executor(None, self._make_slice, session, item)
        except Exception:  # never let one bad pass take the collector down
            log.exception("could not slice pass at %.1fs", item.result.start_time)
            return
        if name:
            session.names.append(name)
            self._wake_uploader.set()

    def _make_slice(self, session: Session, item: _Pending) -> str | None:
        live = item.result
        start = max(live.start_time - LEAD_S, session.first_frame or 0.0)
        # Stop just before the frame currently being received: its lines may still be arriving
        # (the archive keeps growing while this runs on a worker thread).
        end = min(live.end_time + TAIL_S, session.now - 1e-3)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_slice(session.archive.path, Path(tmp) / "pass.zip.acmi", start, end, item.objects)
            recording = load_recording(path)
            candidates = [p for p in find_passes(recording) if p.aircraft_id == live.aircraft_id
                          and abs(p.start_time - live.start_time) <= MATCH_START_TOLERANCE_S]
            if not candidates:
                log.warning("pass at %.1fs not found again in its slice; skipped", live.start_time)
                return None
            p = min(candidates, key=lambda c: abs(c.start_time - live.start_time))
            if self.hooks is not None:
                attach_dcs_grades([p], recording, self.hooks.debrief())
            meta = sidecar(recording, p, session.archive.path.name, item.objects)
            meta["recording"]["first_frame_time"] = session.first_frame
            meta["window"] = {"start": start, "end": end}
            name = slice_name(recording, p)
            self.outbox.put(name, path, meta)
        log.info("queued %s (%s%s)", name, p.outcome.value, f", DCS: {p.dcs_grade.raw}" if p.dcs_grade else "")
        return name

    # -- debrief.log at session end -----------------------------------------------------------

    async def _debrief_after(self, session: Session) -> None:
        path = self.config.debrief
        assert path is not None
        deadline = time.monotonic() + DEBRIEF_WAIT_S
        started = session.archive.started.timestamp()
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_mtime >= started:
                break
            await asyncio.sleep(2.0)
        else:
            log.info("no new debrief.log appeared for this session")
            return
        await asyncio.sleep(1.0)  # let DCS finish writing it
        updated = await asyncio.get_running_loop().run_in_executor(None, self.apply_debrief, session.names, path)
        if updated:
            log.info("debrief.log added DCS grades to %d passes", updated)
            self._wake_uploader.set()

    def apply_debrief(self, names: list[str], path: Path) -> int:
        """Attach DCS grades from debrief.log to this session's passes that lack one; requeue them."""
        debrief = load_debrief(path)
        updated = 0
        wanted = set(names)
        for item in self.outbox.sent() + self.outbox.pending():
            if item.name not in wanted:
                continue
            meta = item.meta()
            if (meta.get("dcs") or {}).get("grade"):
                continue
            recording = load_recording(item.acmi)
            info = meta["pass"]
            passes = [p for p in find_passes(recording) if p.aircraft_id == info["aircraft_id"]
                      and abs(p.start_time - info["start_time"]) <= MATCH_START_TOLERANCE_S]
            if not passes:
                continue
            attach_dcs_grades(passes, recording, debrief)
            p = passes[0]
            if p.dcs_grade is None:
                continue
            meta["dcs"] = {"wire": p.wire, "grade": asdict(p.dcs_grade)}
            if item.sidecar.parent == self.outbox.sent_dir:
                self.outbox.requeue(item, meta)
            else:
                item.sidecar.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            updated += 1
        return updated
