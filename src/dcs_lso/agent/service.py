"""Server agent (or pilot uploader): live Tacview stream -> session archive -> per-pass slices -> hub.

Runs next to DCS. For each stream connection (a "session") it:
- writes the raw stream to `archive/<start>.txt.acmi` (zipped when the session ends);
- detects passes live; `TAIL_S` after a pass ends, slices it from the archive with the
  same code as `dcs-lso slice`, attaches DCS's grade and wire from the dcs-lso hook's
  events in dcs.log, and queues slice + sidecar in the outbox;
- uploads the outbox to hub, retrying until accepted;
- at session end, reads debrief.log and fills in DCS grades the hook didn't provide (in pilot mode,
  the pilot's own DCS grades and wires for their track reports).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..acmi import AcmiParser, Frame, ObjectRemoved, ObjectUpdate, load_recording
from ..acmi.stream import DEFAULT_PORT, HandshakeError, TelemetryClient
from ..acmi.writer import write_slice
from ..dcslog import (Debrief, DcsEvent, HookEvent, LsoGrade, attach_dcs_grades, follow, load_debrief,
                      parse_hook_line, track_dcs_grades)
from ..dcslog.match import tacview_id_hint
from ..detect import Outcome, PassResult, find_passes
from ..detect.approaches import Approach, ApproachSegmenter
from ..geometry import AIRCRAFT, WindProfile
from ..grading import Grade, grade_pass
from ..slices import (LEAD_S, TAIL_S, apply_hooks, approach_window, sidecar, slice_name, slice_objects, track_sidecar,
                      track_slice_name)
from ..callouts.voice import ClipLibrary, choose_clip_set
from ..srs import Modulation, Radio
from .callouts import CallSink, CalloutSettings, LiveCallouts, SrsSink
from .live import LivePassDetector
from .outbox import Outbox

log = logging.getLogger(__name__)

RECONNECT_MIN_S, RECONNECT_MAX_S = 2.0, 30.0
UPLOAD_INTERVAL_S = 5.0
DEBRIEF_WAIT_S = 60.0
MATCH_START_TOLERANCE_S = 5.0
MARK_AFTER_PASS_S = 30.0  # DCS's LSO grade comes this soon after a pass ends, at most
# Warn when a connection delivers no frames for this long (Tacview's exporter isn't
# getting data from DCS, e.g. Export.lua lost its Tacview line, or the sim is paused).
NO_FRAMES_WARNING_S = 30.0
PRUNE_INTERVAL_S = 6 * 3600.0  # retention clean-up (also at start-up)
RETENTION_DEFAULT_DAYS = {"archives": 90.0, "sent": 14.0, "rejected": 30.0}
NO_FRAMES_REPEAT_S = 600.0  # repeat the warning this rarely while frames stay away
# Passes waiting to be sliced are due in mission time, which only advances with frames. DCS pauses an
# empty dedicated server (e.g. the pilot leaves right after landing), so after this long without
# frames, slice them anyway from what was recorded.
STALLED_SLICE_S = 10.0
WATCH_INTERVAL_S = 2.0
CONFIG_REFRESH_S = 60.0
PLAYERS_REPORT_S = 120.0  # report the connected players at least this often (the hub's list stays fresh)


@dataclass
class AgentConfig:
    work_dir: Path
    tacview_host: str = "127.0.0.1"
    tacview_port: int = DEFAULT_PORT
    tacview_password: str | None = None
    dcs_log: Path | None = None
    debrief: Path | None = None
    url: str | None = None
    token: str | None = None
    # "server": live callouts over SRS (if enabled in the hub's config); "pilot": record and upload
    # only, never transmit (a pilot uploader would be talking on someone else's server).
    mode: str = "server"
    voice_dir: Path | None = None  # a clip set from `dcs-lso voice build`, or a folder of them (see `_clips`)
    listen_model: Path | None = None  # a Vosk model, to hear pilots' calls (if the hub's config says listen)
    # Retention, in days (0: keep forever). Session archives allow re-slicing; uploaded slices are kept
    # on the hub, so the local copies only matter for a while (e.g. adding debrief.log grades). None:
    # not set here, so the hub's configuration for this source ("retention") applies, else the default.
    keep_archives_days: float | None = None
    keep_sent_days: float | None = None
    keep_rejected_days: float | None = None


class HookFeed:
    """Follows dcs.log in a background thread and keeps the dcs-lso hook's events (or, with `follow=False`,
    reads what's in it now: for a backfill, the events of the log's last mission)."""

    def __init__(self, path: Path, follow: bool = True) -> None:
        self.path = path
        self._events: list[HookEvent] = []
        self._lock = threading.Lock()
        if follow:
            threading.Thread(target=self._run, name="dcs-log-follower", daemon=True).start()
        else:
            with Path(path).open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if (event := parse_hook_line(line)) is not None:
                        self.add(event)

    def _run(self) -> None:
        for line in follow(self.path):
            if (event := parse_hook_line(line)) is not None:
                self.add(event)
                if event.event == "carrier" and (radio := self.carrier_radio(event.raw.get("name"))):
                    log.info("carrier in the mission: %s (%s) on %.3f %s", event.raw.get("name"), event.raw.get("type"),
                             radio.frequency_mhz, radio.modulation.name)
                if event.event == "landing_quality_mark":
                    log.info("DCS LSO: %s", event.comment)

    def add(self, event: HookEvent) -> None:
        with self._lock:
            if event.event == "handler_installed":
                # A new mission: its times restart at 0, so the previous mission's events would
                # otherwise match this mission's passes.
                self._events.clear()
            if event.event == "players":  # only the latest list matters
                self._events = [e for e in self._events if e.event != "players"]
            self._events.append(event)

    def players(self) -> list[dict] | None:
        """Who is connected to the DCS server now (UCID, IP, name), from the hook's latest list; None if it
        hasn't logged one yet."""
        with self._lock:
            latest = next((e for e in reversed(self._events) if e.event == "players"), None)
        if latest is None:
            return None
        return [{"ucid": str(x.get("ucid")), "ip": str(x.get("ip") or ""), "name": x.get("name")}
                for x in latest.raw.get("players") or [] if isinstance(x, dict) and x.get("ucid")]

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

    def live_wire(self, tacview_id: int, since: float) -> int | None:
        """DCS's wire for this aircraft since `since` (mission time), if it has arrived: the carrier's
        wire animation (hosted servers) or DCS's LSO grade (with comms)."""
        animated = self.wire_for(tacview_id, since, float("inf"))
        if animated is not None:
            return animated
        with self._lock:
            marks = [e for e in self._events if e.event == "landing_quality_mark" and (e.time or 0.0) >= since]
        for e in reversed(marks):
            who = (e.initiator or {}).get("object_id")
            if who is not None and tacview_id_hint(int(who)) == tacview_id and e.comment:
                if (wire := LsoGrade.parse(e.comment).wire) is not None:
                    return wire
        return None

    def carrier_radio(self, carrier_unit: str | None) -> Radio | None:
        """The radio frequency set for this carrier (by unit name) in the current mission, from the hook."""
        if not carrier_unit:
            return None
        with self._lock:
            found = [e for e in self._events if e.event == "carrier" and e.raw.get("name") == carrier_unit]
        if not found:
            return None
        raw = found[-1].raw
        try:
            frequency_hz = float(raw.get("frequency"))
        except (TypeError, ValueError):
            return None
        if frequency_hz <= 0:
            return None
        modulation = Modulation.FM if raw.get("modulation") == 1 else Modulation.AM
        return Radio(round(frequency_hz / 1e6, 4), modulation)

    def player_names(self) -> set[str] | None:
        """Everyone known to be a player in this mission: the connected players now, and anyone the hook saw
        take a slot (players who left since, and a listen server's host). None if the hook hasn't said (an
        older hook, or nothing logged yet): then AI can't be told from players."""
        with self._lock:
            events = [e for e in self._events if e.event in ("players", "slot")]
        if not events:
            return None
        names = {str(e.raw.get("player")) for e in events if e.event == "slot" and e.raw.get("player")}
        for e in events:
            if e.event == "players":
                names |= {str(x.get("name")) for x in e.raw.get("players") or [] if isinstance(x, dict) and x.get("name")}
        return names

    def pilot_for(self, pilot: str, aircraft_type: str, tacview_id: int, start: float, end: float) -> str:
        """Who flew an aircraft's pass (`start`-`end`, mission time): Tacview's pilot name, unless the hook says
        otherwise. Tacview can keep the name from an earlier object with the same id (seen: a player's
        Tomcat named after the player whose Hornet had the id before), so: the player DCS's LSO graded for this
        aircraft; else, when Tacview's pilot was in another type of aircraft then, the one player who was in
        this type."""
        with self._lock:
            events = [e for e in self._events if e.event in ("slot", "landing_quality_mark")]
        for e in events:
            who = e.initiator or {}
            if (e.event == "landing_quality_mark" and who.get("object_id") is not None and who.get("player")
                    and tacview_id_hint(int(who["object_id"])) == tacview_id
                    and start <= (e.time or 0.0) <= end + MARK_AFTER_PASS_S):
                return self._corrected(pilot, str(who["player"]), "DCS's LSO grade")
        latest: dict[str, dict] = {}  # each player's latest slot by `start`
        for e in sorted((e for e in events if e.event == "slot"), key=lambda e: e.time or 0.0):
            if (e.time or 0.0) <= start and e.raw.get("player"):
                latest[str(e.raw["player"])] = e.raw
        mine = latest.get(pilot)
        if mine is None or mine.get("type") == aircraft_type:
            return pilot
        others = [name for name, raw in latest.items() if name != pilot and raw.get("type") == aircraft_type]
        return self._corrected(pilot, others[0], "the hook's slot changes") if len(others) == 1 else pilot

    @staticmethod
    def _corrected(pilot: str, player: str, how: str) -> str:
        if player != pilot:
            log.info("pass flown by %s (says %s), not %s as Tacview names it", player, how, pilot or "-")
        return player

    def wall_clock(self, mission_time: float) -> datetime | None:
        """When (UTC) the mission clock read `mission_time`, from the hook event logged nearest to it (the
        mission clock stops while DCS pauses an empty server, so it can't be counted from the mission start)."""
        with self._lock:
            timed = [e for e in self._events if e.time is not None and e.logged_at is not None]
        if not timed:
            return None
        e = min(timed, key=lambda e: abs(e.time - mission_time))
        return e.logged_at + timedelta(seconds=mission_time - e.time)

    def slot_for(self, pilot: str | None, before: float) -> dict | None:
        """The aircraft a player was in (livery, side number, unit) at mission time `before`: their latest
        slot change logged by the hook in this mission."""
        if not pilot:
            return None
        with self._lock:
            slots = [e for e in self._events if e.event == "slot" and e.raw.get("player") == pilot
                     and (e.time or 0.0) <= before]
        if not slots:
            return None
        raw = slots[-1].raw
        return {k: raw.get(k) for k in ("livery", "onboard_num", "unit", "group") if raw.get(k) not in (None, "")}

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
    # When each object first appeared in this session (to line debrief.log's clock up with the stream's).
    first_seen: dict[int, float] = field(default_factory=dict)


class Agent:
    def __init__(self, config: AgentConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.outbox = Outbox(config.work_dir / "outbox")
        self.archive_dir = config.work_dir / "archive"
        self.hooks = HookFeed(config.dcs_log) if config.dcs_log else None
        if client is None and config.url and config.token:
            client = httpx.AsyncClient(base_url=config.url, headers={"Authorization": f"Bearer {config.token}"},
                                       timeout=60)
        self.client = client
        self._wake_uploader = asyncio.Event()
        self._players_reported: list | None = None  # the last player list reported to the hub
        self._players_reported_at = -PLAYERS_REPORT_S
        self.config_path = config.work_dir / "config.json"
        self.remote_config: dict = self._load_cached_config()
        self._clip_sets: dict[Path, ClipLibrary] = {}  # loaded once each
        self._recogniser = None  # loaded on the first session that listens
        # Overridable for tests; by default calls go to the SRS server named in the config.
        self.sink_factory = lambda settings: SrsSink(settings.srs, _radios(settings))

    # -- configuration from the hub -------------------------------------------------------------

    def _load_cached_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    async def refresh_config(self) -> None:
        """Fetch this source's configuration from the hub; keep the cached copy if that fails."""
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
            log.info("configuration updated from the hub (applies from the next session)")

    def _callouts(self) -> LiveCallouts | None:
        if self.config.mode != "server":
            log.info("live callouts OFF (pilot mode)")
            return None
        settings = CalloutSettings.from_config(self.remote_config)
        if not settings.enabled:
            log.info("live callouts OFF (not enabled in this source's config on the hub; "
                     "see `dcs-lso hub set-config`)")
            return None
        clips = self._clips(settings.voice)
        if clips is None:
            return None
        callouts = LiveCallouts(settings, clips, self.sink_factory(settings),
                                wind_for=self.hooks.wind_for if self.hooks is not None else None,
                                wire_for=self.hooks.live_wire if self.hooks is not None else None)
        if self.hooks is not None:
            callouts.carrier_radio = self.hooks.carrier_radio  # the mission's frequency for each carrier
        return callouts

    def _listener(self, callouts: LiveCallouts):
        """Listening to pilots' calls, if the hub's config says so and there's a model to recognise them with."""
        if not callouts.settings.listen:
            return None
        if self.config.listen_model is None:
            log.warning("listening OFF: on in the hub's config, but no --listen-model was given")
            return None
        if not hasattr(callouts.sink, "on_voice"):
            return None  # (a test sink)
        from ..callouts.heard import Recogniser
        from .listening import Listener
        try:
            if self._recogniser is None:
                self._recogniser = Recogniser(self.config.listen_model)
        except Exception as exc:  # e.g. Vosk not installed, or not a model
            log.warning("listening OFF: %s", exc)
            return None
        listener = Listener(self._recogniser, callouts.on_heard)
        listener.attach(callouts.sink)
        return listener

    def _clips(self, voice: str | None) -> ClipLibrary | None:
        """The voice for this session: `--voice-dir` is one clip set, or a folder of them where the hub's
        config (`callouts.voice`) picks one by folder name."""
        if self.config.voice_dir is None:
            log.warning("live callouts OFF: enabled in the hub's config, but no --voice-dir was given")
            return None
        path, note = choose_clip_set(self.config.voice_dir, voice)
        if path is None:
            log.warning("live callouts OFF: %s", note)
            return None
        log.info(note)
        if path not in self._clip_sets:
            self._clip_sets[path] = ClipLibrary.load(path)
        return self._clip_sets[path]

    # -- top level ----------------------------------------------------------------------------

    def retention(self) -> dict[str, float]:
        """Days to keep each kind of file (0: forever): set locally, else from the hub's configuration
        for this source (`{"retention": {"archives_days": 90, "sent_days": 14, "rejected_days": 30}}`),
        else the defaults."""
        hub = (self.remote_config or {}).get("retention") or {}
        out = {}
        for kind, default in RETENTION_DEFAULT_DAYS.items():
            local = getattr(self.config, f"keep_{kind}_days")
            try:
                out[kind] = float(local if local is not None else hub.get(f"{kind}_days", default))
            except (TypeError, ValueError):
                out[kind] = default
        return out

    def prune(self, now: float | None = None) -> dict[str, int]:
        """Apply the retention settings: old session archives and uploaded/rejected slices. Pending
        uploads and the archive of a session in progress are never removed."""
        now = time.time() if now is None else now
        keep = self.retention()
        removed = {"archives": 0, "sent": 0, "rejected": 0}
        if keep["archives"] > 0:
            cutoff = now - keep["archives"] * 86400
            for path in self.archive_dir.glob("*.zip.acmi"):  # closed sessions only (open ones are .txt.acmi)
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed["archives"] += 1
        if keep["sent"] > 0:
            removed["sent"] = self.outbox.prune(self.outbox.sent_dir, now - keep["sent"] * 86400)
        if keep["rejected"] > 0:
            removed["rejected"] = self.outbox.prune(self.outbox.rejected_dir, now - keep["rejected"] * 86400)
        if any(removed.values()):
            log.info("retention: removed %d session archives, %d uploaded slices, %d rejected slices",
                     removed["archives"], removed["sent"], removed["rejected"])
        return removed

    async def _prune_loop(self) -> None:
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.prune)
            except OSError as exc:
                log.warning("retention clean-up failed: %s", exc)
            await asyncio.sleep(PRUNE_INTERVAL_S)

    async def run_forever(self) -> None:
        uploader = asyncio.create_task(self.upload_loop())
        pruner = asyncio.create_task(self._prune_loop())
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
            pruner.cancel()

    async def upload_loop(self) -> None:
        if self.client is None:
            log.warning("no hub URL/token: passes stay in %s", self.outbox.pending_dir)
            return
        last_config = 0.0
        while True:
            if time.monotonic() - last_config >= CONFIG_REFRESH_S:
                await self.refresh_config()
                last_config = time.monotonic()
            await self.report_players()
            await self.upload_once()
            try:
                await asyncio.wait_for(self._wake_uploader.wait(), UPLOAD_INTERVAL_S)
            except TimeoutError:
                pass
            self._wake_uploader.clear()

    async def report_players(self, now: float | None = None) -> bool:
        """Tell the hub who is connected to this DCS server (from the server hook), so it can recognise their
        pilot hooks without a token: when the list changes, and every PLAYERS_REPORT_S anyway (the hub treats
        an old list as stale). Returns whether it reported."""
        if self.client is None or self.hooks is None or self.config.mode != "server":
            return False
        players = self.hooks.players()
        if players is None:
            return False
        now = time.monotonic() if now is None else now
        key = sorted((p["ucid"], p["ip"]) for p in players)
        if key == self._players_reported and now - self._players_reported_at < PLAYERS_REPORT_S:
            return False
        try:
            r = await self.client.post("/api/v1/players", json={"players": players})
        except httpx.HTTPError as exc:
            log.debug("player list not reported: %s", exc)
            return False
        if r.status_code != 200:
            log.warning("hub refused the player list (%s %s)", r.status_code, r.text[:200])
            return False
        self._players_reported, self._players_reported_at = key, now
        return True

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
            callouts.grade_for = lambda carrier_id, aircraft_id: _provisional_grade(session, carrier_id, aircraft_id)
            callouts.deck_foul = session.detector.landing_area_foul
            callouts.departed_from = session.detector.departed_from
            if self.hooks is not None:
                hooks = self.hooks
                callouts.side_number_for = lambda pilot, t: (hooks.slot_for(pilot, t) or {}).get("onboard_num")
        listening = None
        if callouts is not None:
            listener = self._listener(callouts)
            await callouts.sink.start()
            if listener is not None:
                listening = asyncio.create_task(listener.run(), name="listening")
        log.info("connected to Tacview stream from %r; archiving to %s%s", info.name, session.archive.path,
                 "; live callouts ON" + (", listening to pilots" if listening else "") if callouts else "")
        watchdog = asyncio.create_task(self._watch_frames(session))
        try:
            async for line in client.lines():
                await self._line(session, line)
        finally:
            watchdog.cancel()
            if listening is not None:
                listening.cancel()
            await client.close()
            for result in session.detector.flush():
                self._queue(session, result)
            for segmenter in (session.segmenters or {}).values():
                if (approach := segmenter.flush()) is not None:
                    session.approaches.append(_PendingApproach(approach, approach.end_time + TAIL_S))
            if session.callouts is not None:
                await session.callouts.drain()  # finish calls first (a welcome may still be waiting for the wire)
            await self._slice_waiting(session)
            if session.callouts is not None:
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
            if isinstance(record, ObjectUpdate) and record.moved:
                session.first_seen.setdefault(record.id, record.time)
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
        if self.config.mode == "server" and (pilot_for := getattr(self.hooks, "pilot_for", None)) is not None:
            result.pilot = pilot_for(result.pilot, result.aircraft_type, result.aircraft_id, result.start_time,
                                     result.end_time)
        players = getattr(self.hooks, "player_names", lambda: None)() if self.hooks is not None else None
        if self.config.mode == "server" and players is not None and result.pilot not in players:
            # AI: the LSO still talks it down (live calls don't come through here), but it isn't graded or
            # put on the board.
            log.info("pass by %s not uploaded (AI: not a player in this mission)", result.pilot or hex(result.aircraft_id))
            return
        if self.config.mode == "pilot" and not _own_jet(result):
            # The pilot uploader sends only this PC's own jet (other players' jets are seen here at the
            # same rate the server sees them, and they aren't the pilot's to send).
            log.debug("pass by %s skipped (not this PC's own jet)", result.pilot or hex(result.aircraft_id))
            return
        objects = slice_objects(session.detector.recording(dict(session.parser.globals)), result)
        session.pending.append(_Pending(result, objects, result.end_time + TAIL_S))
        session.pass_windows.append((result.aircraft_id, result.start_time - LEAD_S, result.end_time + TAIL_S))
        log.info("pass detected: %s %s, %s; slicing in %.0fs", result.pilot or hex(result.aircraft_id),
                 result.aircraft_type, result.outcome.value, TAIL_S)

    async def _slice(self, session: Session, item: _Pending) -> None:
        session.archive.flush()
        live = item.result
        if session.callouts:
            await session.callouts.settled(live.aircraft_id)
        calls = (session.callouts.calls_for(live.aircraft_id, live.start_time - LEAD_S, live.end_time + TAIL_S)
                 if session.callouts else None)
        loop = asyncio.get_running_loop()
        try:
            name = await loop.run_in_executor(None, self._make_slice, session, item, calls)
        except Exception:  # never let one bad pass take the agent down
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
        except Exception:  # never let one bad approach take the agent down
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
            meta["pass"]["aircraft_first_seen"] = session.first_seen.get(approach.aircraft_id)
            name = track_slice_name(recording, approach)
            self.outbox.put(name, path, meta)
        log.info("queued %s (own track; the hub grades it with the server's report of this landing)", name)
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
            extra: dict = {"wire_source": None}
            if self.hooks is not None:
                p.pilot = live.pilot  # as checked against the hook when detected (_queue)
                # Again: DCS's LSO grade (naming the player) has likely arrived by now.
                extra = apply_hooks(self.hooks, recording, p, end, check_pilot=self.config.mode == "server")
            meta = sidecar(recording, p, session.archive.path.name, item.objects)
            meta.update(extra)
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
            grade = (_track_grade(recording, info, debrief) if meta.get("kind") == "track"
                     else _pass_grade(recording, info, debrief))
            if grade is None:
                continue
            meta["dcs"] = {"wire": grade.wire, "grade": asdict(grade)}
            if item.sidecar.parent == self.outbox.sent_dir:
                self.outbox.requeue(item, meta)
            else:
                item.sidecar.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            updated += 1
        return updated


def _pass_grade(recording, info: dict, debrief: Debrief) -> LsoGrade | None:
    """DCS's grade for an uploaded pass, from debrief.log."""
    passes = [p for p in find_passes(recording) if p.aircraft_id == info["aircraft_id"]
              and abs(p.start_time - info["start_time"]) <= MATCH_START_TOLERANCE_S]
    if not passes:
        return None
    attach_dcs_grades(passes, recording, debrief)
    return passes[0].dcs_grade


def _track_grade(recording, info: dict, debrief: Debrief) -> LsoGrade | None:
    """DCS's grade for an own-track report (the pilot uploader), from the pilot's debrief.log: a
    multiplayer client's DCS grades its own traps (with the wire) and writes them there at mission end."""
    aircraft = int(info["aircraft_id"])
    approach = Approach(aircraft, float(info["start_time"]), float(info["end_time"]), 0.0)
    first_seen = info.get("aircraft_first_seen")
    grades = track_dcs_grades(recording, [approach], debrief,
                              first_seen={aircraft: float(first_seen)} if first_seen is not None else None)
    return grades.get(approach)


def _provisional_grade(session: Session, carrier_id: int, aircraft_id: int) -> Grade | None:
    """Our grade of a trap still in progress (for the welcome); None if it can't be graded yet. The
    jet hasn't stopped yet, so the pass would still classify as a bolter: the welcome only follows a
    detected arrestment, so it's graded as the trap it is."""
    try:
        result = session.detector.provisional(carrier_id, aircraft_id)
        return grade_pass(replace(result, outcome=Outcome.TRAP)).grade if result is not None else None
    except Exception:  # never let grading trouble stop a call
        log.exception("could not grade the pass in progress")
        return None


def _radios(settings: CalloutSettings):
    """Every LSO frequency this agent may transmit on (the SRS client announces them)."""
    radios = [settings.radio_for("")]
    for unit in settings.carriers:
        r = settings.radio_for(unit)
        if r not in radios:
            radios.append(r)
    return radios


def _own_jet(result: PassResult) -> bool:
    """Flown on this PC: Tacview records AOA only for the local player's jet."""
    return bool(result.samples) and not result.samples[0].aoa_derived
