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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..acmi import AcmiParser, Frame, ObjectRemoved, ObjectUpdate, load_recording
from ..acmi.stream import DEFAULT_PORT, HandshakeError, TelemetryClient
from ..acmi.writer import write_slice
from ..dcslog import Debrief, DcsEvent, HookEvent, attach_dcs_grades, follow, load_debrief, parse_hook_line
from ..dcslog.match import tacview_id_hint
from ..detect import PassResult, find_passes
from ..detect.approaches import Approach, ApproachSegmenter
from ..geometry import AIRCRAFT, WindProfile
from ..slices import (LEAD_S, TAIL_S, approach_window, sidecar, slice_name, slice_objects, track_sidecar,
                      track_slice_name)
from ..callouts.voice import ClipLibrary
from .callouts import CallSink, CalloutSettings, LiveCallouts, SrsSink
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
NO_FRAMES_REPEAT_S = 600.0  # repeat the warning this rarely while frames stay away
# Passes waiting to be sliced are due in mission time, which only advances with frames. DCS pauses an
# empty dedicated server (e.g. the pilot leaves right after landing), so after this long without
# frames, slice them anyway from what was recorded.
STALLED_SLICE_S = 10.0
WATCH_INTERVAL_S = 2.0
CONFIG_REFRESH_S = 60.0


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
    # "server": live callouts over SRS (if enabled in central's config); "pilot": record and upload
    # only, never transmit (a pilot's collector would be talking on someone else's server).
    mode: str = "server"
    voice_dir: Path | None = None  # clip set from `dcs-lso voice build`


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
                self.add(event)
                if event.event == "landing_quality_mark":
                    log.info("DCS LSO: %s", event.comment)

    def add(self, event: HookEvent) -> None:
        with self._lock:
            if event.event == "handler_installed":
                # A new mission: its times restart at 0, so the previous mission's events would
                # otherwise match this mission's passes.
                self._events.clear()
            self._events.append(event)

    def wire_for(self, tacview_id: int, start: float, end: float) -> int | None:
        """The wire caught by this aircraft between `start` and `end` (mission time), from the hook's
        samples of the carrier's arresting-wire animation (1 on the caught wire, 0 elsewhere)."""
        with self._lock:
            events = [e for e in self._events if e.event == "wire_sample"]
        counts: dict[int, int] = {}
        for e in events:
            who = (e.raw.get("initiator") or {}).get("object_id")
            if who is None or tacview_id_hint(int(who)) != tacview_id or not start <= (e.time or 0.0) <= end:
                continue
            caught = [n for n in (1, 2, 3, 4) if ((e.raw.get("wires") or {}).get(f"w{n}") or 0) > 0.5]
            if len(caught) == 1:
                counts[caught[0]] = counts.get(caught[0], 0) + 1
        return max(counts, key=counts.get) if counts else None

    def wind_for(self, carrier_unit: str | None) -> WindProfile | None:
        """The latest wind the hook logged at this carrier (by unit name) in the current mission."""
        if not carrier_unit:
            return None
        with self._lock:
            events = [e for e in self._events if e.event == "wind" and e.raw.get("carrier") == carrier_unit]
        return wind_profile(events[-1].raw) if events else None

    def debrief(self) -> Debrief:
        """The hook's landing grades in debrief.log form, for `attach_dcs_grades`."""
        with self._lock:
            events = list(self._events)
        return Debrief(None, [to_dcs_event(e) for e in events if e.event == "landing_quality_mark"])


def wind_profile(raw: dict) -> WindProfile | None:
    """From a hook `wind` event. Lua arrays arrive as objects keyed "1", "2", ..."""
    levels = raw.get("levels") or []
    if isinstance(levels, dict):
        levels = [levels[k] for k in sorted(levels, key=lambda k: int(k))]
    try:
        return WindProfile.from_dict({"levels": levels})
    except (KeyError, TypeError, ValueError):
        return None


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
    # Wall clock when the pass ended (it was detected then). Mission time stops while DCS pauses an
    # empty server, so wall times can't be derived from mission time across a session.
    ended_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _PendingApproach:
    approach: Approach
    due: float
    ended_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Session:
    archive: SessionArchive
    detector: LivePassDetector
    callouts: LiveCallouts | None = None
    parser: AcmiParser = field(default_factory=AcmiParser)
    first_frame: float | None = None
    now: float = 0.0
    pending: list[_Pending] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # outbox items produced by this session
    frames: int = 0
    last_frame_at: float = field(default_factory=time.monotonic)  # wall clock (monotonic) of the last frame
    # Pilot mode: the own jet's approaches, found without a carrier (see `detect.approaches`).
    segmenters: dict[int, ApproachSegmenter] | None = None
    approaches: list[_PendingApproach] = field(default_factory=list)
    pass_windows: list[tuple[int, float, float]] = field(default_factory=list)  # (aircraft, start, end)


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
        self.config_path = config.work_dir / "config.json"
        self.remote_config: dict = self._load_cached_config()
        self.clips = ClipLibrary.load(config.voice_dir) if config.voice_dir else None
        # Overridable for tests; by default calls go to the SRS server named in the config.
        self.sink_factory = lambda settings: SrsSink(settings.srs, _radios(settings))

    # -- configuration from central -------------------------------------------------------------

    def _load_cached_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    async def refresh_config(self) -> None:
        """Fetch this source's configuration from central; keep the cached copy if that fails."""
        if self.client is None:
            return
        try:
            r = await self.client.get("/api/v1/config")
        except httpx.HTTPError as exc:
            log.debug("config fetch failed: %s", exc)
            return
        if r.status_code != 200:
            log.warning("config fetch refused (%s)", r.status_code)
            return
        config = r.json()
        if config != self.remote_config:
            self.remote_config = config
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            log.info("configuration updated from central (applies from the next session)")

    def _callouts(self) -> LiveCallouts | None:
        if self.config.mode != "server":
            log.info("live callouts OFF (pilot mode)")
            return None
        settings = CalloutSettings.from_config(self.remote_config)
        if not settings.enabled:
            log.info("live callouts OFF (not enabled in this source's config on central; "
                     "see `dcs-lso central set-config`)")
            return None
        if self.clips is None:
            log.warning("live callouts OFF: enabled in central's config, but no --voice-dir was given")
            return None
        return LiveCallouts(settings, self.clips, self.sink_factory(settings),
                            wind_for=self.hooks.wind_for if self.hooks is not None else None)

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
        last_config = 0.0
        while True:
            if time.monotonic() - last_config >= CONFIG_REFRESH_S:
                await self.refresh_config()
                last_config = time.monotonic()
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
        await self.refresh_config()
        callouts = self._callouts()
        session = Session(SessionArchive(self.archive_dir), LivePassDetector(callouts), callouts,
                          segmenters={} if c.mode == "pilot" else None)
        if callouts is not None:
            await callouts.sink.start()
        log.info("connected to Tacview stream from %r; archiving to %s%s", info.name, session.archive.path,
                 "; live callouts ON" if callouts else "")
        watchdog = asyncio.create_task(self._watch_frames(session))
        try:
            async for line in client.lines():
                await self._line(session, line)
        finally:
            watchdog.cancel()
            await client.close()
            for result in session.detector.flush():
                self._queue(session, result)
            for segmenter in (session.segmenters or {}).values():
                if (approach := segmenter.flush()) is not None:
                    session.approaches.append(_PendingApproach(approach, approach.end_time + TAIL_S))
            await self._slice_waiting(session)
            if session.callouts is not None:
                await session.callouts.drain()
                await session.callouts.sink.close()
            archive = session.archive.close()
            log.info("session ended; archive saved as %s", archive)
            self._wake_uploader.set()
        if c.debrief is not None:
            asyncio.create_task(self._debrief_after(session))
        return session

    async def _watch_frames(self, session: Session) -> None:
        """While frames stop: slice waiting passes after `STALLED_SLICE_S`, and warn now and then."""
        warned_at: float | None = None
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            idle = time.monotonic() - session.last_frame_at
            if idle < WATCH_INTERVAL_S:
                warned_at = None
                continue
            if idle >= STALLED_SLICE_S and (session.pending or session.approaches):
                log.info("no frames for %.0fs (DCS pauses an empty server); slicing %d waiting pass(es) now",
                         idle, len(session.pending) + len(session.approaches))
                await self._slice_waiting(session)
            if idle >= NO_FRAMES_WARNING_S and (warned_at is None or time.monotonic() - warned_at >= NO_FRAMES_REPEAT_S):
                warned_at = time.monotonic()
                log.warning(
                    "connected to Tacview but no new frames for %.0fs. Usually the mission is paused (a dedicated "
                    "server pauses with no players); otherwise check that DCS's Saved Games/DCS/Scripts/Export.lua "
                    "loads Scripts/TacviewGameExport.lua (SRS's installer can replace Export.lua without it).", idle)

    async def _slice_waiting(self, session: Session) -> None:
        pending, session.pending = session.pending, []
        approaches, session.approaches = session.approaches, []
        for item in pending:
            await self._slice(session, item)
        for waiting in approaches:
            await self._slice_approach(session, waiting.approach, waiting.ended_at)

    async def _line(self, session: Session, line: str) -> None:
        session.archive.write(line)
        for record in session.parser.feed(line):
            if isinstance(record, Frame):
                session.frames += 1
                session.last_frame_at = time.monotonic()
                session.now = record.time
                if session.first_frame is None:
                    session.first_frame = record.time
                due = [p for p in session.pending if p.due <= record.time]
                if due:
                    session.pending = [p for p in session.pending if p.due > record.time]
                    for item in due:
                        await self._slice(session, item)
                ready = [a for a in session.approaches if a.due <= record.time]
                if ready:
                    session.approaches = [a for a in session.approaches if a.due > record.time]
                    for pending in ready:
                        await self._slice_approach(session, pending.approach, pending.ended_at)
            for result in session.detector.feed(record):
                self._queue(session, result)
            if session.segmenters is not None:
                self._own_track(session, record)

    def _own_track(self, session: Session, record) -> None:
        """Pilot mode: follow the own jet (the aircraft with recorded AOA) for approaches."""
        assert session.segmenters is not None
        if isinstance(record, ObjectRemoved):
            segmenter = session.segmenters.pop(record.id, None)
            approach = segmenter.flush() if segmenter else None
        elif isinstance(record, ObjectUpdate) and record.moved and "AOA" in record.props:
            track = session.detector.tracks.get(record.id)
            if track is None or track.name not in AIRCRAFT or not track.samples:
                return
            segmenter = session.segmenters.setdefault(record.id, ApproachSegmenter(record.id))
            approach = segmenter.feed(track.samples[-1])
        else:
            return
        if approach is not None:
            session.approaches.append(_PendingApproach(approach, approach.end_time + TAIL_S))
            log.info("approach detected (own track): %.0fs-%.0fs; slicing in %.0fs",
                     approach.start_time, approach.end_time, TAIL_S)

    def _queue(self, session: Session, result: PassResult) -> None:
        objects = slice_objects(session.detector.recording(dict(session.parser.globals)), result)
        session.pending.append(_Pending(result, objects, result.end_time + TAIL_S))
        session.pass_windows.append((result.aircraft_id, result.start_time - LEAD_S, result.end_time + TAIL_S))
        log.info("pass detected: %s %s, %s; slicing in %.0fs", result.pilot or hex(result.aircraft_id),
                 result.aircraft_type, result.outcome.value, TAIL_S)

    async def _slice(self, session: Session, item: _Pending) -> None:
        session.archive.flush()
        live = item.result
        calls = (session.callouts.calls_for(live.aircraft_id, live.start_time - LEAD_S, live.end_time + TAIL_S)
                 if session.callouts else None)
        loop = asyncio.get_running_loop()
        try:
            name = await loop.run_in_executor(None, self._make_slice, session, item, calls)
        except Exception:  # never let one bad pass take the collector down
            log.exception("could not slice pass at %.1fs", item.result.start_time)
            return
        if name:
            session.names.append(name)
            self._wake_uploader.set()

    async def _slice_approach(self, session: Session, approach: Approach, ended_at: datetime) -> None:
        """Upload the own track around an approach, unless it was already handled as a full pass
        (the pilot's data had a carrier)."""
        start, end = approach_window(approach)
        if any(aircraft == approach.aircraft_id and s < end and start < e for aircraft, s, e in session.pass_windows):
            return
        session.archive.flush()
        try:
            name = await asyncio.get_running_loop().run_in_executor(None, self._make_track_slice, session, approach,
                                                                    ended_at)
        except Exception:  # never let one bad approach take the collector down
            log.exception("could not slice approach at %.1fs", approach.start_time)
            return
        session.names.append(name)
        self._wake_uploader.set()

    def _make_track_slice(self, session: Session, approach: Approach, ended_at: datetime) -> str:
        start, end = approach_window(approach)
        start = max(start, session.first_frame or 0.0)
        end = min(end, session.now - 1e-3)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_slice(session.archive.path, Path(tmp) / "track.zip.acmi", start, end, {approach.aircraft_id})
            recording = load_recording(path)
            meta = track_sidecar(recording, approach, session.archive.path.name)
            meta["recording"]["first_frame_time"] = session.first_frame
            meta["window"] = {"start": start, "end": end}
            meta["pass"]["occurred_at"] = (ended_at - timedelta(seconds=approach.end_time - approach.start_time)).isoformat()
            name = track_slice_name(recording, approach)
            self.outbox.put(name, path, meta)
        log.info("queued %s (own track; central grades it with the server's report of this landing)", name)
        return name

    def _make_slice(self, session: Session, item: _Pending, calls: list[dict] | None = None) -> str | None:
        live = item.result
        start = max(live.start_time - LEAD_S, session.first_frame or 0.0)
        # Stop just before the frame currently being received: its lines may still be arriving
        # (the archive keeps growing while this runs on a worker thread).
        end = min(live.end_time + TAIL_S, session.now - 1e-3)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_slice(session.archive.path, Path(tmp) / "pass.zip.acmi", start, end, item.objects)
            recording = load_recording(path)
            carrier = recording.objects.get(live.carrier_id)
            wind = self.hooks.wind_for(carrier.pilot) if self.hooks is not None and carrier else None
            candidates = [p for p in find_passes(recording, wind) if p.aircraft_id == live.aircraft_id
                          and abs(p.start_time - live.start_time) <= MATCH_START_TOLERANCE_S]
            if not candidates:
                log.warning("pass at %.1fs not found again in its slice; skipped", live.start_time)
                return None
            p = min(candidates, key=lambda c: abs(c.start_time - live.start_time))
            wire_source = None
            if self.hooks is not None:
                attach_dcs_grades([p], recording, self.hooks.debrief())
                if p.wire is not None:
                    wire_source = "dcs-lso"
                animated = self.hooks.wire_for(p.aircraft_id, p.start_time, end)
                if animated is not None:
                    if p.wire is not None and p.wire != animated:
                        log.warning("wire disagreement: carrier animation says #%d, DCS's LSO says #%d (using #%d)",
                                    animated, p.wire, animated)
                    p.wire, wire_source = animated, "carrier-animation"
            meta = sidecar(recording, p, session.archive.path.name, item.objects)
            meta["wire_source"] = wire_source
            meta["recording"]["first_frame_time"] = session.first_frame
            meta["window"] = {"start": start, "end": end}
            if calls is not None:
                meta["calls"] = calls
            meta["pass"]["occurred_at"] = (item.ended_at - timedelta(seconds=live.end_time - p.start_time)).isoformat()
            name = slice_name(recording, p)
            self.outbox.put(name, path, meta)
        log.info("queued %s (%s%s%s)", name, p.outcome.value, f", wire #{p.wire}" if p.wire else "",
                 f", DCS: {p.dcs_grade.raw}" if p.dcs_grade else "")
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


def _radios(settings: CalloutSettings):
    """Every LSO frequency this collector may transmit on (the SRS client announces them)."""
    radios = [settings.radio_for("")]
    for unit in settings.carriers:
        r = settings.radio_for(unit)
        if r not in radios:
            radios.append(r)
    return radios
