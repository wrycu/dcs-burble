"""Hub service logic: sources, ingest, regrading. No web concerns here."""

from __future__ import annotations

import hashlib
import logging
import math
import secrets
import statistics
import tempfile
import ipaddress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..acmi import ObjectTrack, Recording, Sample, load_recording
from ..dcslog import Debrief, LsoGrade, load_debrief
from ..detect import PassResult, find_passes
from ..detect.wire import WireSignals, wire_signals
from ..geometry import AIRCRAFT, CARRIERS, DeckFrame, WindProfile
from ..slices import is_default_pilot, own_pilots, slice_recording
from ..sun import NIGHT_BELOW_DEG, is_night
from ..cards.overlay import OverlayPass
from ..grading import GRADING_VERSION, GradeResult, grade_name, grade_pass
from ..grading.trends import DEFAULT_PASSES, TrendPass, Trends, trends
from .db import Grade, Pass, Pilot, PilotAlias, PlayerSeen, Slice, Source, Upload, make_engine, make_sessionmaker
from .pilothook import HookUploadError, hook_reports, parse_upload
from .passwords import MIN_LENGTH as MIN_PASSWORD_LENGTH, FailureLimiter, hash_password, verify_password
from .storage import SliceStore

START_TIME_TOLERANCE_S = 0.5
log = logging.getLogger(__name__)

PUBLIC_UPLOADS = "uploads"  # source of recordings uploaded without a token
PILOT_HOOKS = "pilot hooks"  # source of pilot hook uploads recognised without a token (see pilot_hook_access)
INTERNAL_SOURCES = (PUBLIC_UPLOADS, PILOT_HOOKS)
# A player counts as flying on one of this hub's servers if a server agent reported them this recently
# (a pilot hook sends each approach right after it ends).
PLAYER_RECENT = timedelta(minutes=30)
# A server agent's player list is current if it reported it this recently (it reports at least every 2 min).
PLAYERS_FRESH = timedelta(minutes=10)
PILOT_HOOK_ACCEPT = ("ours", "any")
UPLOAD_PILOT_WAIT = timedelta(days=1)  # how long an upload waits for its uploader to pick a pilot
DEFAULT_PILOT_REFUSED = ("passes flown under DCS's default pilot name aren't recorded, since they can't be "
                         "credited to a pilot")
UPLOAD_PASSWORD_ATTEMPTS = 5  # wrong pilot passwords before an upload is given up
# Merging reports of one landing (see `Hub.ingest`).
MERGED_START_TOLERANCE_S = 15.0
SAME_POSITION_M = 30.0  # median distance between the two tracks of the aircraft
MIN_COMMON_SAMPLES = 5


class IngestError(ValueError):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def pass_key(sidecar: dict) -> str:
    rec, p = sidecar.get("recording") or {}, sidecar["pass"]
    return f"{rec.get('RecordingTime') or '?'}|{p['aircraft_id']}|{float(p['start_time']):.2f}"


def occurred_at(sidecar: dict) -> datetime | None:
    rec, p = sidecar.get("recording") or {}, sidecar["pass"]
    # Collectors record the wall clock time of each pass (mission time stops while DCS pauses an
    # empty server, so the recording's start time plus mission time can be hours off).
    if (stamped := _parse_time(p.get("occurred_at"))) is not None:
        return stamped
    start = _parse_time(rec.get("RecordingTime"))
    if start is None:
        return None
    return start + timedelta(seconds=float(p["start_time"]) - float(rec.get("first_frame_time") or 0.0))


@dataclass(frozen=True, slots=True)
class PilotSummary:
    trends: Trends  # across the latest `rows`
    rows: list[Pass]  # the graded landings looked at, newest first
    landings: int  # all of the pilot's landings
    first_seen: datetime | None  # their first and latest landing
    last_seen: datetime | None
    modex: str | None = None  # the pilot's side number (the first one seen)
    last_livery: str | None = None  # the livery of their newest landing that has one
    protected: bool = False  # they've set an upload password


@dataclass(frozen=True, slots=True)
class IngestResult:
    pass_id: int
    created: bool
    grade: str
    text: str


@dataclass(frozen=True, slots=True)
class WireCheck:
    landing_id: int
    report_id: int
    pilot: str
    occurred_at: datetime | None
    carrier_type: str
    known: int | None  # the wire caught, when known
    known_from: str | None  # "DCS" or "own track"
    signals: WireSignals
    overshoot_m: float | None  # how far the server's stop point overshot the real one (known wire only)


class Hub:
    def __init__(self, database_url: str, data_dir: str | Path, require_upload_token: bool = False,
                 pilot_hook_accept: str = "ours") -> None:
        if pilot_hook_accept not in PILOT_HOOK_ACCEPT:
            raise ValueError(f"pilot_hook_accept must be one of {PILOT_HOOK_ACCEPT}")
        # Pilot hook uploads: only traps flown on this hub's own servers ("ours"), or from anywhere with a
        # pilot token ("any"; traps from other communities' servers have no carrier here yet).
        self.pilot_hook_accept = pilot_hook_accept
        # Refuse recordings uploaded without a source's token (by default anyone may upload their passes).
        self.require_upload_token = require_upload_token
        self._upload_passwords: dict[int, str] = {}  # given with an upload, until its pilot is known (memory only)
        self._upload_attempts: dict[int, int] = {}
        self.password_failures = FailureLimiter()
        self.engine = make_engine(database_url)
        self.sessions: sessionmaker[Session] = make_sessionmaker(self.engine)
        self.store = SliceStore(Path(data_dir) / "slices")
        self.uploads_dir = Path(data_dir) / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._fail_interrupted_uploads()
        # Called with a landing's id after it's added or changed (None: many may have changed); see `ingest`.
        self.listeners: list = []
        self._backfill_night()
        self._backfill_reported_names()

    # -- sources ----------------------------------------------------------------------------

    def add_source(self, name: str, kind: str = "server") -> str:
        """Add a server agent (or, kind "pilot", an uploader not tied to a pilot, used only internally
        for token-less uploads); returns its token (only the hash is stored)."""
        if kind not in ("server", "pilot"):
            raise ValueError("kind must be 'server' or 'pilot'")
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            s.add(Source(name=name, kind=kind, token_hash=_hash_token(token)))
        return token

    # -- pilot tokens -----------------------------------------------------------------------

    def create_pilot_token(self, name: str, password: str | None, label: str = "") -> str:
        """A pilot creates a pilot token with their password (they must have set one). Everything
        uploaded with it is credited to them. Returns the token, which is shown only once."""
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            if pilot.password_hash is None:
                raise PermissionError("set a password first")
            self._check_password(pilot, password)
            return self._new_pilot_token(s, pilot, label)

    def add_pilot_token(self, name: str, label: str = "") -> str:
        """Admin: create a pilot token for a pilot (no password needed; the pilot is added if they have no
        passes here yet)."""
        with self.sessions.begin() as s:
            return self._new_pilot_token(s, self._resolve_pilot(s, name), label)

    def _new_pilot_token(self, s: Session, pilot: Pilot, label: str) -> str:
        label = (label or "").strip()[:100] or "pilot token"
        base = f"{pilot.name}: {label}"[:90]
        names = set(s.scalars(select(Source.name).where(Source.name.like(f"{base}%"))))
        source_name = next(n for n in (base, *(f"{base} ({i})" for i in range(2, 1000))) if n not in names)
        token = secrets.token_urlsafe(32)
        s.add(Source(name=source_name, kind="pilot", token_hash=_hash_token(token), pilot_id=pilot.id, label=label))
        return token

    def pilot_tokens(self, name: str) -> list[Source]:
        """A pilot's tokens, newest first (revoked ones included)."""
        with self.sessions() as s:
            pilot = self._pilot(s, name)
            return list(s.scalars(select(Source).where(Source.pilot_id == pilot.id).order_by(Source.id.desc())))

    def revoke_pilot_token(self, name: str, password: str | None, token_id: int) -> None:
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            self._check_password(pilot, password)
            source = s.get(Source, token_id)
            if source is None or source.pilot_id != pilot.id:
                raise LookupError("no such token")
            source.revoked_at = source.revoked_at or datetime.now(UTC)

    # -- aliases ----------------------------------------------------------------------------

    def pilot_aliases(self, name: str) -> list[PilotAlias]:
        with self.sessions() as s:
            pilot = self._pilot(s, name)
            return list(s.scalars(select(PilotAlias).where(PilotAlias.pilot_id == pilot.id).order_by(PilotAlias.name)))

    def claim_alias(self, name: str, password: str | None, alias: str) -> int:
        """A pilot claims another in-game name with their password: passes reported under it are credited to
        them from now on, and existing ones move over. Only a name nobody owns can be claimed: not another
        pilot's alias, and not a pilot who has set a password. Returns how many passes moved."""
        alias = (alias or "").strip()[:100]
        if not alias:
            raise ValueError("enter a name")
        if is_default_pilot(alias):
            raise ValueError("DCS's default name can't be claimed")
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            self._check_password(pilot, password)
            if alias == pilot.name:
                raise ValueError("that's already your name")
            existing = s.scalar(select(PilotAlias).where(PilotAlias.name == alias))
            if existing is not None and existing.pilot_id != pilot.id:
                raise PermissionError(f"{alias!r} is already another pilot's name")
            other = s.scalar(select(Pilot).where(Pilot.name == alias))
            if other is not None and other.password_hash is not None:
                raise PermissionError(f"{alias!r} is a pilot who has set a password")
            if existing is None:
                s.add(PilotAlias(name=alias, pilot_id=pilot.id, claimed=True))
            else:
                existing.claimed = True
            moved = 0
            if other is not None:
                for row in s.scalars(select(Pass).where(Pass.pilot_id == other.id)):
                    row.pilot_id = pilot.id
                    moved += 1
                for a in s.scalars(select(PilotAlias).where(PilotAlias.pilot_id == other.id)):
                    a.pilot_id = pilot.id
                if pilot.modex is None:
                    pilot.modex = other.modex
                s.flush()
                s.delete(other)
            return moved

    def remove_alias(self, alias: str) -> int:
        """Admin: undo an alias. Passes reported under that name (other than with the pilot's own tokens) go
        back to a pilot of that name. Returns how many passes moved."""
        with self.sessions.begin() as s:
            row = s.scalar(select(PilotAlias).where(PilotAlias.name == alias))
            if row is None:
                raise LookupError(f"no alias {alias!r}")
            owner_id = row.pilot_id
            s.delete(row)
            s.flush()
            own_tokens = select(Source.id).where(Source.pilot_id == owner_id)
            passes = list(s.scalars(select(Pass).where(Pass.pilot_id == owner_id, Pass.reported_name == alias,
                                                       Pass.source_id.not_in(own_tokens))))
            if passes:
                target = self._resolve_pilot(s, alias)
                for p in passes:
                    p.pilot_id = target.id
            return len(passes)

    def remove_pilot(self, name: str) -> None:
        """Admin: remove a pilot with no passes (e.g. a junk sign-up on an open board): their aliases go, their
        pilot tokens are revoked. A pilot with passes is refused (their landings would go with them)."""
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            passes = s.scalar(select(func.count()).select_from(Pass).where(Pass.pilot_id == pilot.id))
            if passes:
                raise ValueError(f"{name!r} has {passes} passes; only pilots with no passes can be removed")
            for alias in s.scalars(select(PilotAlias).where(PilotAlias.pilot_id == pilot.id)):
                s.delete(alias)
            for source in s.scalars(select(Source).where(Source.pilot_id == pilot.id)):
                source.pilot_id, source.revoked_at = None, source.revoked_at or datetime.now(UTC)
            s.flush()
            s.delete(pilot)

    def _resolve_pilot(self, s: Session, name: str) -> Pilot:
        """The pilot a report under this in-game name belongs to: an alias's pilot, else the pilot of that
        name (created on first sight)."""
        alias = s.scalar(select(PilotAlias).where(PilotAlias.name == name))
        if alias is not None:
            return s.get(Pilot, alias.pilot_id)
        pilot = s.scalar(select(Pilot).where(Pilot.name == name))
        if pilot is None:
            pilot = Pilot(name=name)
            s.add(pilot)
            s.flush()
        return pilot

    @staticmethod
    def _note_alias(s: Session, pilot: Pilot, reported: str | None) -> None:
        """A name seen on an upload with the pilot's own token becomes their alias, unless someone already
        has it (a pilot of that name, or another pilot's alias), which needs a claim instead."""
        if not reported or reported == pilot.name or is_default_pilot(reported):
            return
        if s.scalar(select(PilotAlias).where(PilotAlias.name == reported)) is not None:
            return
        if s.scalar(select(Pilot).where(Pilot.name == reported)) is not None:
            return
        s.add(PilotAlias(name=reported[:100], pilot_id=pilot.id, claimed=False))
        s.flush()

    def set_config(self, name: str, config: dict) -> None:
        with self.sessions.begin() as s:
            source = s.scalar(select(Source).where(Source.name == name))
            if source is None:
                raise ValueError(f"no source named {name!r}")
            source.config = config

    def authenticate(self, token: str) -> Source | None:
        with self.sessions() as s:
            return s.scalar(select(Source).where(Source.token_hash == _hash_token(token), Source.revoked_at.is_(None)))

    # -- passes -----------------------------------------------------------------------------

    def reports(self, landing: Pass, s: Session | None = None) -> list[Pass]:
        """Every report of this landing: the landing itself first, then those merged into it."""
        if landing.id is None:
            return [landing]
        q = (select(Pass).where(Pass.merged_into_id == landing.id).order_by(Pass.id)
             .options(selectinload(Pass.slice), selectinload(Pass.source)))
        if s is not None:
            return [landing, *s.scalars(q)]
        with self.sessions() as own:
            return [landing, *own.scalars(q)]

    def load_pass(self, p: Pass, reports: list[Pass] | None = None, s: Session | None = None) -> PassResult:
        """Rebuild the landing from its stored slices: the most detailed aircraft track among its
        reports, graded against the carrier in this (gradable) report, plus DCS's grade and wire."""
        if p.is_track:
            raise IngestError("a track report has no carrier; it is graded as part of its landing")
        reports = self.reports(p) if reports is None else reports
        best = max(reports, key=lambda r: _detail(r, r is p))
        recording = load_recording(self.store.path(p.slice.sha256))
        wind = WindProfile.from_dict((p.slice.sidecar or {}).get("wind"))
        aircraft_id, tolerance = p.aircraft_id, START_TIME_TOLERANCE_S
        if best is not p:
            recording, aircraft_id = self._with_track(recording, p, best, s)
            tolerance = MERGED_START_TOLERANCE_S  # passes are detected a little differently at another rate
        candidates = [c for c in find_passes(recording, wind) if c.aircraft_id == aircraft_id
                      and abs(c.start_time - p.start_time) <= tolerance]
        if not candidates:
            raise IngestError("pass not found in its slice")
        result = min(candidates, key=lambda c: abs(c.start_time - p.start_time))
        result.dcs_grade = LsoGrade.parse(p.dcs_grade) if p.dcs_grade else None
        result.wire = p.wire
        result.track_source = best.source.name if best.source is not None else None
        return result

    def _with_track(self, recording: Recording, landing: Pass, other: Pass,
                    s: Session | None = None) -> tuple[Recording, int]:
        """`recording` with the landing's aircraft track replaced by `other`'s, moved onto this
        recording's clock. Both count from their own ReferenceTime (mission time)."""
        theirs = load_recording(self.store.path(other.slice.sha256))
        offset = self._clock_offset(other, landing, s)
        track = theirs.objects[other.aircraft_id]
        objects = {i: t for i, t in recording.objects.items() if i != landing.aircraft_id}
        new_id = other.aircraft_id if other.aircraft_id not in objects else max(objects) + 1
        objects[new_id] = ObjectTrack(new_id, dict(track.props),
                                      [Sample(x.time + offset, x.transform, x.aoa) for x in track.samples])
        return Recording(recording.globals, objects, recording.first_frame), new_id

    def _same_landing(self, a: Pass, b: Pass, s: Session | None = None) -> bool:
        """Two reports of the same landing: same pilot, aircraft and mission (checked by the caller),
        overlapping in mission time, and the aircraft in the same place at the same moments."""
        wa, wb = _mission_window(a, self._reference_time(a, s)), _mission_window(b, self._reference_time(b, s))
        if wa is None or wb is None or not (wa[0] < wb[1] and wb[0] < wa[1]):
            return False
        ra = load_recording(self.store.path(a.slice.sha256))
        rb = load_recording(self.store.path(b.slice.sha256))
        offset = self._clock_offset(b, a, s)
        ta, tb = ra.objects.get(a.aircraft_id), rb.objects.get(b.aircraft_id)
        if ta is None or tb is None:
            return False
        return _median_distance(ta.samples, tb.samples, offset) <= SAME_POSITION_M

    def _reference_time(self, p: Pass, s: Session | None = None) -> datetime | None:
        """When the report's clock starts (its time 0). Usually the recording's ReferenceTime. A pilot hook's
        report is on the mission clock: it starts at mission start, which is the ReferenceTime of a server
        agent's recording of the same mission (a server's recording starts at mission start; a mission's
        start date and time are fixed in the mission file). None until such a report is known."""
        sidecar = p.slice.sidecar or {}
        if sidecar.get("clock") != "mission":
            return _parse_time((sidecar.get("recording") or {}).get("ReferenceTime"))
        if p.mission is None:
            return None
        q = (select(Slice.sidecar).join(Pass, Pass.slice_id == Slice.id).join(Source, Pass.source_id == Source.id)
             .where(Pass.mission == p.mission, Source.kind == "server").order_by(Pass.id.desc()).limit(20))
        if s is None:
            with self.sessions() as own:
                sidecars = list(own.scalars(q))
        else:
            sidecars = list(s.scalars(q))  # within an ingest: includes the report being stored
        for other in sidecars:
            if (other or {}).get("clock") != "mission":
                if (ref := _parse_time(((other or {}).get("recording") or {}).get("ReferenceTime"))) is not None:
                    return ref
        return None

    def _clock_offset(self, theirs: Pass, ours: Pass, s: Session | None = None) -> float:
        """Seconds to add to a time in `theirs` to get the same moment on `ours`'s clock."""
        a, b = self._reference_time(theirs, s), self._reference_time(ours, s)
        return (a - b).total_seconds() if a is not None and b is not None else 0.0

    def _attach(self, s: Session, landing: Pass, report: Pass) -> None:
        report.merged_into_id = landing.id
        # The landing keeps whatever any report knows: DCS's grade and wire, the live calls.
        if landing.dcs_grade is None and report.dcs_grade:
            landing.dcs_grade, landing.wire = report.dcs_grade, report.wire
        if landing.wire is None and report.wire is not None:
            landing.wire = report.wire
        if landing.calls is None and report.calls:
            landing.calls = report.calls
        if landing.livery is None and report.livery:
            landing.livery = report.livery
        s.flush()

    def _regrade(self, s: Session, row: Pass) -> GradeResult:
        loaded = self.load_pass(row, self.reports(row, s), s)
        row.outcome = loaded.outcome.value  # from the best track (and the current detection)
        result = grade_pass(loaded)
        current = next((g for g in row.grades if g.version == result.version), None)
        if current is not None:
            row.grades.remove(current)
            s.flush()
        row.grades.append(_grade_row(result))
        return result

    def ingest(self, source_id: int, data: bytes, sidecar: dict) -> IngestResult:
        """Store a report (a pass or an own-jet track) and merge it with others of the same landing; then tell the
        listeners (e.g. Discord) which landing changed."""
        result = self._ingest(source_id, data, sidecar)
        self._notify(result.pass_id)
        return result

    def _notify(self, landing_id: int | None) -> None:
        """`landing_id`: a landing that was added or changed; None: many may have (e.g. regrading)."""
        for listener in self.listeners:
            try:
                listener(landing_id)
            except Exception:  # a listener must never break storing passes
                log.exception("hub listener failed")

    def _ingest(self, source_id: int, data: bytes, sidecar: dict) -> IngestResult:
        try:
            info = sidecar["pass"]
            key = pass_key(sidecar)
            kind = sidecar.get("kind") or "pass"
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestError(f"invalid sidecar: {exc}") from exc
        with self.sessions.begin() as s:
            source = s.get(Source, source_id)
            token_pilot = s.get(Pilot, source.pilot_id) if source is not None and source.pilot_id else None
            if token_pilot is None and source is not None and source.kind == "pilot" and source.name not in INTERNAL_SOURCES:
                raise IngestError("this pilot token isn't tied to a pilot; create a new one on your settings page")
            if token_pilot is None and is_default_pilot(info.get("pilot")):
                raise IngestError(DEFAULT_PILOT_REFUSED)
            if source is not None:
                source.last_used_at = datetime.now(UTC)
            existing = s.scalar(select(Pass).where(Pass.source_id == source_id, Pass.pass_key == key))
            if existing is not None:
                # The only thing a re-upload can add: DCS's grade, if it wasn't known the first time
                # (e.g. found in debrief.log at mission end).
                dcs = sidecar.get("dcs") or {}
                if existing.dcs_grade is None and (dcs.get("grade") or {}).get("raw"):
                    existing.dcs_grade = dcs["grade"]["raw"]
                    existing.wire = dcs.get("wire")
                    if existing.merged_into_id is not None:
                        self._attach(s, s.get(Pass, existing.merged_into_id), existing)
                shown = s.get(Pass, existing.merged_into_id) if existing.merged_into_id else existing
                g = shown.grade
                return IngestResult(shown.id, False, g.grade if g else "", g.text if g else "")

            sha = self.store.put(data)
            slice_row = s.scalar(select(Slice).where(Slice.sha256 == sha))
            if slice_row is None:
                slice_row = Slice(sha256=sha, size=len(data), sidecar=sidecar, source_id=source_id)
                s.add(slice_row)
                s.flush()

            reported = info.get("pilot") or None
            if token_pilot is not None:
                pilot = token_pilot  # a pilot token: theirs, whatever name they flew under
                self._note_alias(s, pilot, reported)
            else:
                pilot = self._resolve_pilot(s, reported or f"id {int(info['aircraft_id']):x}")

            dcs = sidecar.get("dcs") or {}
            try:
                row = Pass(
                    pass_key=key, source_id=source_id, slice=slice_row, pilot=pilot, kind=kind,
                    reported_name=(reported or "")[:100] or None,
                    occurred_at=occurred_at(sidecar), mission=(sidecar.get("recording") or {}).get("Title"),
                    carrier_type=info.get("carrier_type") or "", carrier_unit=info.get("carrier_unit"),
                    aircraft_type=info["aircraft_type"], aircraft_id=int(info["aircraft_id"]),
                    start_time=float(info["start_time"]), end_time=float(info["end_time"]),
                    outcome=info["outcome"], wire=dcs.get("wire"),
                    dcs_grade=(dcs.get("grade") or {}).get("raw"),
                    calls=sidecar.get("calls"),
                    livery=((sidecar.get("aircraft") or {}).get("livery") or None),
                )
                modex = str((sidecar.get("aircraft") or {}).get("onboard_num") or "").strip()
                if modex and pilot.modex is None:
                    pilot.modex = modex[:16]  # only the first one seen is kept
                if kind == "track":
                    if load_recording(self.store.path(sha)).objects.get(row.aircraft_id) is None:
                        raise ValueError("the track's aircraft isn't in its slice")
                else:
                    grade_pass(self.load_pass(row, [row]))  # check the slice on its own before storing it
                    row.night = self._night(row)
            except (OSError, ValueError, KeyError) as exc:
                raise IngestError(f"could not analyse the slice: {exc}") from exc
            s.add(row)
            s.flush()

            matches = [m for m in self._candidates(s, row) if self._same_landing(m, row, s)]
            landing = next((m for m in matches if not m.is_track), None)
            if landing is None and not row.is_track:
                landing = row
            if landing is None:
                return IngestResult(row.id, True, "", "waiting for a report of this landing that has the carrier")
            for report in [row, *matches]:
                if report is not landing and report.merged_into_id is None:
                    self._attach(s, landing, report)
            result = self._regrade(s, landing)
            return IngestResult(landing.id, True, result.grade.value, result.text)

    def _night(self, p: Pass) -> bool | None:
        """Was the pass flown at night (at the carrier, when it ended)? None if it can't be told."""
        try:
            carrier_id = int(((p.slice.sidecar or {}).get("pass") or {})["carrier_id"])
            if (p.slice.sidecar or {}).get("clock") == "mission":  # a pilot hook's: mission start is known only
                ref = self._reference_time(p)                       # from a server agent's report of the mission
                if ref is None:  # else DCS's own sun, as the pilot hook read it
                    elevation = (p.slice.sidecar or {}).get("sun_elevation")
                    return None if elevation is None else float(elevation) < NIGHT_BELOW_DEG
                return is_night(load_recording(self.store.path(p.slice.sha256)), carrier_id, p.end_time, ref)
            return is_night(load_recording(self.store.path(p.slice.sha256)), carrier_id, p.end_time)
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def _backfill_night(self) -> None:
        """Work out day or night for passes stored before it was recorded."""
        with self.sessions.begin() as s:
            q = (select(Pass).where(Pass.night.is_(None), or_(Pass.kind.is_(None), Pass.kind != "track"))
                 .options(selectinload(Pass.slice)))
            for p in s.scalars(q):
                p.night = self._night(p)

    def _backfill_reported_names(self) -> None:
        """Record the in-game name of passes stored before it was kept (from their sidecars)."""
        with self.sessions.begin() as s:
            for p in s.scalars(select(Pass).where(Pass.reported_name.is_(None)).options(selectinload(Pass.slice))):
                name = (((p.slice.sidecar or {}).get("pass") or {}).get("pilot") or "")[:100]
                if name:
                    p.reported_name = name

    def _candidates(self, s: Session, row: Pass) -> list[Pass]:
        """Unmerged reports that could be the same landing as `row`: from other sources, or from the same
        one (e.g. a server's Tacview file backfilled after its agent already sent the pass live)."""
        q = (select(Pass).where(Pass.id != row.id, Pass.pilot_id == row.pilot_id,
                                Pass.aircraft_type == row.aircraft_type, Pass.merged_into_id.is_(None))
             .order_by(Pass.id).options(selectinload(Pass.slice)))
        q = q.where(Pass.mission == row.mission) if row.mission is not None else q.where(Pass.mission.is_(None))
        return list(s.scalars(q))

    # -- the pilot hook ---------------------------------------------------------------------------

    # -- players on this hub's servers (from the server agents) ----------------------------------------

    def report_players(self, source_id: int, players: list[dict]) -> None:
        """A server agent's list of who is connected to its DCS server now (UCID, IP, name)."""
        now = datetime.now(UTC)
        with self.sessions.begin() as s:
            source = s.get(Source, source_id)
            if source is None or source.kind != "server":
                raise PermissionError("only a server agent reports players")
            source.players_at = now
            current = {}
            for x in players:
                ucid = str((x or {}).get("ucid") or "").strip()[:64]
                if ucid:
                    current[ucid] = x
            for row in s.scalars(select(PlayerSeen).where(PlayerSeen.source_id == source_id)):
                if row.ucid in current:
                    x = current.pop(row.ucid)
                    row.ip, row.name = _ip(x.get("ip")), (x.get("name") or row.name or "")[:100] or None
                    row.connected, row.last_seen = True, now
                else:
                    row.connected = False
            for ucid, x in current.items():
                s.add(PlayerSeen(source_id=source_id, ucid=ucid, ip=_ip(x.get("ip")), name=(x.get("name") or "")[:100] or None,
                                 connected=True, last_seen=now))

    def _players(self, s: Session, ucid: str, connected_now: bool) -> list[PlayerSeen]:
        """Where this UCID was seen on this hub's servers: connected now (by a current player list), or
        recently (PLAYER_RECENT)."""
        now = datetime.now(UTC)
        q = select(PlayerSeen).join(Source, PlayerSeen.source_id == Source.id).where(
            PlayerSeen.ucid == ucid, Source.kind == "server", Source.revoked_at.is_(None))
        if connected_now:
            q = q.where(PlayerSeen.connected.is_(True), Source.players_at >= now - PLAYERS_FRESH)
        else:
            q = q.where(or_(PlayerSeen.connected.is_(True), PlayerSeen.last_seen >= now - PLAYER_RECENT))
        return list(s.scalars(q))

    def pilot_hook_access(self, token: str | None, ucid: str | None, client_ip: str | None) -> tuple[int, str | None]:
        """Who may send this pilot hook upload, as (source id, the pilot's name per the server or None).
        - With a pilot token: the token's pilot. With "ours", only for a pilot seen on this hub's servers.
        - Without one: a player on this hub's servers (by UCID), sending from the IP address the DCS server
          sees them at, or from a LAN address (the DCS server sees LAN players at their LAN address while the
          hub may see another). Credited under the name the server knows them by.
        Raises PermissionError otherwise."""
        ucid = (ucid or "").strip()
        with self.sessions.begin() as s:
            seen = self._players(s, ucid, connected_now=False) if ucid else []
            if token:
                source = s.scalar(select(Source).where(Source.token_hash == _hash_token(token), Source.revoked_at.is_(None)))
                if source is None or source.pilot_id is None:
                    raise PermissionError("not a pilot token")
                if self.pilot_hook_accept == "ours" and not seen:
                    raise LookupError("this hub only accepts traps flown on its own servers")
                return source.id, None
            lan = _is_lan(client_ip)
            match = next((p for p in seen if p.ip and p.ip == _ip(client_ip)), None) or (seen[0] if seen and lan else None)
            if match is None:
                raise PermissionError("a pilot token is needed (not recognised as a player on this hub's servers)")
            source = s.scalar(select(Source).where(Source.name == PILOT_HOOKS))
            if source is None:
                source = Source(name=PILOT_HOOKS, kind="pilot", token_hash=_hash_token(secrets.token_urlsafe(32)))
                s.add(source)
                s.flush()
            return source.id, match.name

    def pilot_hook_here(self, ucid: str | None, client_ip: str | None) -> bool:
        """Is this UCID connected to one of this hub's servers now, asked from that player's address (or the
        LAN)? Lets the pilot hook find the hub of the server it's on."""
        if not ucid:
            return False
        with self.sessions() as s:
            seen = self._players(s, ucid.strip(), connected_now=True)
        return any(p.ip and p.ip == _ip(client_ip) for p in seen) or (bool(seen) and _is_lan(client_ip))

    def pilot_hook_calls(self, token: str | None, ucid: str | None, client_ip: str | None, pass_id: int) -> dict:
        """The live LSO calls on the landing a pilot hook upload became part of, so the pilot hook can relay them to
        its other hubs (they have no server agent here to make them). Only for the pilot hook that sent that report:
        with the same pilot token, or recognised as the same player (UCID and address). `ready`: a server agent
        has reported the landing (until then the calls may still come)."""
        source_id, _ = self.pilot_hook_access(token, ucid, client_ip)
        with self.sessions() as s:
            found = s.get(Pass, pass_id)  # the landing the upload's answer named (or the report itself)
            landing = s.get(Pass, found.merged_into_id) if found is not None and found.merged_into_id else found
            reports = self.reports(landing, s) if landing is not None else []

            def mine(r: Pass) -> bool:
                hook = (r.slice.sidecar or {}).get("pilot_hook") or {}
                return r.source_id == source_id and (bool(token) or hook.get("ucid") == (ucid or "").strip())

            if not any(mine(r) for r in reports):
                raise LookupError("no such report from this pilot hook")
            ready = any(r.source.kind == "server" for r in reports)
            return {"ready": ready, "calls": landing.calls or []}

    def ingest_pilot_hook(self, source_id: int, body: dict, pilot: str | None = None) -> list[IngestResult]:
        """An upload from the pilot hook (one approach of the pilot's own jet, see `hub.pilothook`), from a
        source `pilot_hook_access` allowed: stored as own-jet track reports, merged with the server agent's
        reports. `pilot`: the name to credit it under (the server's name for the player), if not a token's."""
        try:
            upload = parse_upload(body)
        except HookUploadError as exc:
            raise IngestError(str(exc)) from exc
        if pilot:
            upload = replace(upload, pilot=pilot[:100])
        with tempfile.TemporaryDirectory() as tmp:
            reports = hook_reports(upload, Path(tmp))
        return [self.ingest(source_id, data, meta) for data, meta in reports]

    # -- backfill: whole recordings --------------------------------------------------------------

    def ingest_recording(self, source_id: int, path: str | Path, debrief: Debrief | None = None,
                         pilot: str | None = None, own_only: bool = False) -> list[dict]:
        """Slice every pass (and own-jet track) out of a whole recording, or only `pilot`'s, and ingest
        each, merging with the landings already known. Returns one result per pass or track found."""
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            for acmi, meta in slice_recording(path, tmp, debrief, pilot=pilot, own_only=own_only):
                info = meta["pass"]
                entry = {"kind": meta.get("kind", "pass"), "pilot": info.get("pilot"), "outcome": info.get("outcome"),
                         "start_time": info.get("start_time")}
                try:
                    r = self.ingest(source_id, acmi.read_bytes(), meta)
                    entry.update(pass_id=r.pass_id, created=r.created, grade=r.grade, text=r.text)
                except IngestError as exc:
                    entry["error"] = str(exc)
                results.append(entry)
        return results

    def upload_source(self) -> int:
        """The source that token-less uploads are recorded under (created on first use)."""
        with self.sessions.begin() as s:
            source = s.scalar(select(Source).where(Source.name == PUBLIC_UPLOADS))
            if source is None:
                source = Source(name=PUBLIC_UPLOADS, kind="pilot", token_hash=_hash_token(secrets.token_urlsafe(32)))
                s.add(source)
                s.flush()
            return source.id

    def add_upload(self, source_id: int, filename: str, size: int, choose_pilot: bool) -> tuple[int, str]:
        """Record an upload; returns (id, key). `choose_pilot`: uploaded without a token, so only the
        recording's own pilot's passes are imported (normally found automatically; if a file has several,
        the uploader, who alone has the key, picks)."""
        key = secrets.token_urlsafe(16)
        with self.sessions.begin() as s:
            upload = Upload(source_id=source_id, filename=filename[:300], size=size, key=key,
                            status="inspecting" if choose_pilot else "queued", choose_pilot=choose_pilot)
            s.add(upload)
            s.flush()
            return upload.id, key

    def upload_path(self, upload_id: int, filename: str) -> Path:
        """Where an upload's recording waits to be processed; keeps .zip.acmi/.txt.acmi (our reader
        tells the two apart by name)."""
        suffix = ".zip.acmi" if filename.lower().endswith(".zip.acmi") else ".txt.acmi"
        return self.uploads_dir / f"{upload_id}{suffix}"

    def debrief_path(self, upload_id: int) -> Path:
        return self.uploads_dir / f"{upload_id}.debrief.log"

    def inspect_upload(self, upload_id: int) -> bool:
        """Find a token-less upload's own pilot (worker thread). Returns True if it can go straight on to
        processing (exactly one, chosen automatically); otherwise it waits for the uploader, or ends."""
        with self.sessions.begin() as s:
            upload = s.get(Upload, upload_id)
            path = self.upload_path(upload_id, upload.filename)
        try:
            found = own_pilots(load_recording(path))
            pilots, error = [p for p in found if not is_default_pilot(p)], None
        except Exception as exc:  # a bad file must not take the worker down
            pilots, error = [], f"could not read the recording: {exc}"[:500]
        with self.sessions.begin() as s:
            upload = s.get(Upload, upload_id)
            upload.pilots = pilots
            if not error and found and not pilots:
                error = (f"this recording was flown as {found[0]!r}, DCS's default pilot name: {DEFAULT_PILOT_REFUSED}. "
                         "Set your own pilot name in DCS for future flights (the Logbook in single player, your nickname "
                         "in multiplayer)")
            if error or not pilots:
                upload.status, upload.message = ("failed", error) if error else (
                    "done", "this recording has no own pilot in an aircraft the LSO grades: only passes flown on "
                            "the PC that recorded it can be uploaded without a token (a server's recording needs "
                            "that server's token)")
                upload.finished_at = datetime.now(UTC)
                self._remove_files(upload)
                return False
            if len(pilots) == 1:
                upload.pilot = pilots[0]
                return self._authorize(s, upload, self._upload_passwords.pop(upload_id, None))
            upload.status = "choose_pilot"
            return False

    def remember_upload_password(self, upload_id: int, password: str | None) -> None:
        """A password given with an upload, used once its pilot is known (kept in memory, never stored)."""
        if password:
            self._upload_passwords[upload_id] = password

    def _authorize(self, s: Session, upload: Upload, password: str | None) -> bool:
        """Queue the upload if its pilot has no password or `password` is theirs; otherwise it waits for the
        password (`needs_password`). Returns whether it was queued."""
        pilot = s.scalar(select(Pilot).where(Pilot.name == upload.pilot))
        if pilot is None or pilot.password_hash is None:
            upload.status, upload.message = "queued", None
            return True
        if password and not self.password_failures.blocked(pilot.name) and verify_password(password, pilot.password_hash):
            upload.status, upload.message = "queued", None
            return True
        if password:
            self.password_failures.failed(pilot.name)
            attempts = self._upload_attempts[upload.id] = self._upload_attempts.get(upload.id, 0) + 1
            if attempts >= UPLOAD_PASSWORD_ATTEMPTS:
                upload.status, upload.message = "failed", "too many wrong passwords; please upload again"
                upload.finished_at = datetime.now(UTC)
                self._remove_files(upload)
                return False
            upload.message = "That password isn't right."
        upload.status = "needs_password"
        return False

    def choose_pilot(self, upload_id: int, key: str, pilot: str, password: str | None = None) -> bool:
        """The uploader picks whose passes to import; returns whether it's ready to process (the pilot may
        need their password first)."""
        with self.sessions.begin() as s:
            upload = self._own_upload(s, upload_id, key)
            if upload.status != "choose_pilot":
                raise ValueError(f"this upload isn't waiting for a pilot (it's {upload.status})")
            if pilot not in (upload.pilots or []):
                raise ValueError(f"{pilot!r} isn't one of the pilots in this recording")
            upload.pilot = pilot
            return self._authorize(s, upload, password)

    def give_upload_password(self, upload_id: int, key: str, password: str) -> bool:
        """The uploader gives the pilot's password; returns whether the upload is ready to process."""
        with self.sessions.begin() as s:
            upload = self._own_upload(s, upload_id, key)
            if upload.status != "needs_password":
                raise ValueError(f"this upload isn't waiting for a password (it's {upload.status})")
            return self._authorize(s, upload, password)

    @staticmethod
    def _own_upload(s: Session, upload_id: int, key: str) -> Upload:
        upload = s.get(Upload, upload_id)
        if upload is None or not secrets.compare_digest(upload.key or "", key or ""):
            raise PermissionError("this isn't your upload (the link you were given has its key)")
        return upload

    # -- pilot settings ---------------------------------------------------------------------------

    def register_pilot(self, name: str, password: str) -> str:
        """A pilot joins this board before flying here: claims a name with a password (then they can create
        pilot tokens and claim other names). Returns the name as stored."""
        name = (name or "").strip()
        if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError("enter a name of up to 100 characters")
        if is_default_pilot(name):
            raise ValueError("DCS's default name can't be used; set your own pilot name in DCS")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(f"use a password of at least {MIN_PASSWORD_LENGTH} characters")
        with self.sessions.begin() as s:
            if s.scalar(select(PilotAlias).where(PilotAlias.name == name)) is not None:
                raise PermissionError(f"{name!r} is already another pilot's name")
            existing = s.scalar(select(Pilot).where(Pilot.name == name))
            if existing is not None:
                if existing.password_hash is not None:
                    raise PermissionError(f"{name!r} is already taken")
                raise FileExistsError(name)  # on the board, unclaimed: set the password on their settings page
            s.add(Pilot(name=name, password_hash=hash_password(password)))
        return name

    def set_pilot_password(self, name: str, new: str, current: str | None = None) -> None:
        """Set (first time: claims the name) or change a pilot's password."""
        if len(new or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(f"use at least {MIN_PASSWORD_LENGTH} characters")
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            if pilot.password_hash is not None:
                self._check_password(pilot, current)
            pilot.password_hash = hash_password(new)

    def reset_pilot_password(self, name: str) -> None:
        """Admin: clear a pilot's password (anyone may then set a new one)."""
        with self.sessions.begin() as s:
            self._pilot(s, name).password_hash = None

    def set_pilot_modex(self, name: str, password: str, modex: str) -> None:
        """A pilot with a password changes their side number."""
        modex = (modex or "").strip()
        if not modex.isdigit() or len(modex) > 4:
            raise ValueError("a side number is 1 to 4 digits")
        with self.sessions.begin() as s:
            pilot = self._pilot(s, name)
            if pilot.password_hash is None:
                raise PermissionError("set a password first; then you can change your side number")
            self._check_password(pilot, password)
            pilot.modex = modex

    def _check_password(self, pilot: Pilot, password: str | None) -> None:
        if self.password_failures.blocked(pilot.name):
            raise PermissionError("too many wrong passwords; try again later")
        if not verify_password(password or "", pilot.password_hash):
            self.password_failures.failed(pilot.name)
            raise PermissionError("that password isn't right")

    @staticmethod
    def _pilot(s: Session, name: str) -> Pilot:
        pilot = s.scalar(select(Pilot).where(Pilot.name == name))
        if pilot is None:
            raise LookupError(f"no pilot named {name!r} (a pilot appears once they have a pass on the board)")
        return pilot

    def process_upload(self, upload_id: int) -> None:
        """Run a queued upload (worker thread); the recording file is removed afterwards."""
        with self.sessions.begin() as s:
            upload = s.get(Upload, upload_id)
            upload.status = "processing"
            source_id, pilot, own_only = upload.source_id, upload.pilot, bool(upload.choose_pilot)
            if upload.source is not None and upload.source.pilot_id is not None:
                own_only = True  # a pilot token: only the jet flown on the PC that recorded it, credited to its pilot
            path, debrief_path = self.upload_path(upload_id, upload.filename), self.debrief_path(upload_id)
        try:
            debrief = load_debrief(debrief_path) if debrief_path.exists() else None
            results = self.ingest_recording(source_id, path, debrief, pilot=pilot, own_only=own_only)
            status = "done"
            message = f"no carrier passes flown by {pilot} in this recording" if pilot and not results else None
        except Exception as exc:  # a bad file must not take the worker down
            results, status, message = None, "failed", f"could not read the recording: {exc}"[:500]
        finally:
            path.unlink(missing_ok=True)
            debrief_path.unlink(missing_ok=True)
        with self.sessions.begin() as s:
            upload = s.get(Upload, upload_id)
            upload.status, upload.message, upload.results = status, message, results
            upload.finished_at = datetime.now(UTC)

    def _remove_files(self, upload: Upload) -> None:
        for leftover in self.uploads_dir.glob(f"{upload.id}.*"):
            leftover.unlink(missing_ok=True)

    def _fail_interrupted_uploads(self) -> None:
        """At startup: uploads that were being worked on when the service stopped are failed; those
        waiting for their uploader to pick a pilot keep waiting, for a day. Files left over from half-done
        uploads (a crash mid-transfer) or from finished ones are removed."""
        stale = datetime.now(UTC) - UPLOAD_PILOT_WAIT
        for leftover in self.uploads_dir.glob("incoming-*"):  # no transfer is in progress at startup
            leftover.unlink(missing_ok=True)
        with self.sessions.begin() as s:
            waiting = {u.id for u in s.scalars(select(Upload).where(Upload.status.in_(
                ("inspecting", "queued", "processing", "choose_pilot", "needs_password"))))}
        for path in self.uploads_dir.iterdir():
            owner = path.name.split(".", 1)[0]
            if owner.isdigit() and int(owner) not in waiting:
                path.unlink(missing_ok=True)
        with self.sessions.begin() as s:
            for upload in s.scalars(select(Upload).where(Upload.status.in_(("inspecting", "queued", "processing",
                                                                             "choose_pilot", "needs_password")))):
                created = upload.created_at if upload.created_at.tzinfo else upload.created_at.replace(tzinfo=UTC)
                waiting = upload.status in ("choose_pilot", "needs_password")
                if waiting and created > stale:
                    continue
                upload.message = ("no pilot or password was given within a day; please upload again"
                                  if waiting else
                                  "interrupted by a restart of the service; please upload again")
                upload.status = "failed"
                upload.finished_at = datetime.now(UTC)
                self._remove_files(upload)

    # -- meta grading ---------------------------------------------------------------------------

    def pilot_trends(self, name: str, passes: int = DEFAULT_PASSES) -> PilotSummary | None:
        """Themes across a pilot's last `passes` graded landings, those landings (newest first), and when
        the pilot was first and last seen; None for an unknown pilot."""
        with self.sessions() as s:
            pilot = s.scalar(select(Pilot).where(Pilot.name == name))
            if pilot is None:
                return None
            q = (select(Pass).where(Pass.pilot_id == pilot.id, Pass.merged_into_id.is_(None),
                                    or_(Pass.kind.is_(None), Pass.kind != "track"))
                 .order_by(func.coalesce(Pass.occurred_at, Pass.created_at).desc(), Pass.id.desc())
                 .options(selectinload(Pass.grades), selectinload(Pass.pilot), selectinload(Pass.source),
                          selectinload(Pass.slice)))
            landings = list(s.scalars(q))
            rows = [p for p in landings if p.grade is not None][:passes]
        seen = [p.occurred_at or p.created_at for p in landings]
        items = []
        for p in rows:
            aircraft = AIRCRAFT.get(p.aircraft_type)
            on_speed = aircraft.on_speed_aoa if aircraft else (7.4, 8.8)
            items.append(TrendPass.from_detail(p.grade.detail or {}, p.outcome, on_speed, wire=p.wire))
        return PilotSummary(trends(items), rows, len(landings), min(seen) if seen else None,
                            max(seen) if seen else None, pilot.modex,
                            next((p.livery for p in landings if p.livery), None), pilot.password_hash is not None)

    def overlay(self, rows: list[Pass]) -> list[OverlayPass]:
        """The landings' tracks for an overlay card (newest first); ones whose slice can't be read are left out."""
        items = []
        for p in rows:
            try:
                result = self.load_pass(p)
            except (IngestError, OSError, ValueError, KeyError):
                continue
            g = p.grade
            when = (p.occurred_at or p.created_at).strftime("%Y-%m-%d %H:%M UTC")
            items.append(OverlayPass(result, g.grade if g else "", f"/passes/{p.id}",
                                     f"{grade_name(g.grade) if g else '?'}: {g.text if g else ''} · {when}"))
        return items

    def wire_check(self, days: int = 0) -> list[WireCheck]:
        """For PLAN #25: each trap's server copy (a server agent's report, derived AOA) with the wire it caught,
        when known, and the two server-track wire signals (`detect.wire.wire_signals`). The known wire is DCS's,
        else the estimate from a pilot's own track merged into the landing. `days`: 0 for all."""
        q = (select(Pass).where(Pass.merged_into_id.is_(None), Pass.outcome == "trap",
                                or_(Pass.kind.is_(None), Pass.kind != "track"))
             .order_by(Pass.id).options(selectinload(Pass.slice), selectinload(Pass.source), selectinload(Pass.pilot)))
        if days:
            q = q.where(func.coalesce(Pass.occurred_at, Pass.created_at) >= datetime.now(UTC) - timedelta(days=days))
        out = []
        with self.sessions() as s:
            for landing in s.scalars(q):
                reports = self.reports(landing, s)
                try:
                    merged = self.load_pass(landing, reports, s)
                except IngestError:
                    continue
                known, how = (landing.wire, "DCS") if landing.wire else (merged.wire_estimate, "own track")
                for report in reports:
                    if report.is_track:
                        continue
                    try:
                        result = self.load_pass(report, [report], s)
                    except IngestError:
                        continue
                    if not any(x.aoa_derived for x in result.samples):
                        continue  # a recording PC's own jet, not a server's copy
                    frame = DeckFrame(CARRIERS[result.carrier_type], AIRCRAFT[result.aircraft_type])
                    signals = wire_signals(result.samples, frame)
                    if signals is None:
                        continue
                    overshoot = None
                    if known:
                        overshoot = frame.wire_along[known - 1] - (signals.stop_along + frame.aircraft.arrest_runout_m)
                    out.append(WireCheck(landing.id, report.id, landing.pilot.name, landing.occurred_at,
                                         landing.carrier_type, known, how if known else None, signals, overshoot))
        return out

    def regrade(self, force: bool = False) -> tuple[int, int]:
        """Grade every landing with the current grading version. Returns (regraded, unchanged)."""
        done = skipped = 0
        with self.sessions.begin() as s:
            landings = select(Pass).where(Pass.merged_into_id.is_(None), or_(Pass.kind.is_(None), Pass.kind != "track"))
            for row in s.scalars(landings):
                current = next((g for g in row.grades if g.version == GRADING_VERSION), None)
                if current is not None and not force:
                    skipped += 1
                    continue
                before = (row.outcome, row.grade.grade if row.grade else None, row.grade.text if row.grade else None)
                result = self._regrade(s, row)
                if before != (row.outcome, result.grade.value, result.text):
                    row.discord_stale = True  # the running hub's Discord worker edits its post and the board
                done += 1
        return done, skipped


def _detail(report: Pass, is_landing: bool) -> tuple:
    """How detailed a report's aircraft track is: recorded AOA first, then sample rate."""
    info = (report.slice.sidecar or {}).get("pass") or {}
    return bool(info.get("aoa_recorded")), float(info.get("sample_rate_hz") or 0.0), is_landing




def _mission_window(p: Pass, ref: datetime | None) -> tuple[datetime, datetime] | None:
    """When the report's pass (or a track report's whole window) happened, in mission time (`ref`: when the
    report's clock starts)."""
    sidecar = p.slice.sidecar or {}
    if ref is None:
        return None
    window = sidecar.get("window") or {}
    start = float(window.get("start", p.start_time)) if p.is_track else p.start_time
    end = float(window.get("end", p.end_time)) if p.is_track else p.end_time
    return ref + timedelta(seconds=start), ref + timedelta(seconds=end)


def _median_distance(a: list[Sample], b: list[Sample], b_offset: float) -> float:
    """Median distance between two tracks of one aircraft at `a`'s sample times (`b` moved by
    `b_offset` onto `a`'s clock); infinite if they barely overlap."""
    times = [x.time + b_offset for x in b]
    dists = []
    j = 0
    for x in a:
        while j + 1 < len(times) and times[j + 1] < x.time:
            j += 1
        if j + 1 >= len(times) or not times[j] <= x.time <= times[j + 1]:
            continue
        p, q = b[j].transform, b[j + 1].transform
        f = (x.time - times[j]) / (times[j + 1] - times[j]) if times[j + 1] > times[j] else 0.0
        if None in (p.u, p.v, p.alt, q.u, q.v, q.alt, x.transform.u, x.transform.v, x.transform.alt):
            continue
        dists.append(math.dist((x.transform.u, x.transform.v, x.transform.alt),
                               (p.u + f * (q.u - p.u), p.v + f * (q.v - p.v), p.alt + f * (q.alt - p.alt))))
    return statistics.median(dists) if len(dists) >= MIN_COMMON_SAMPLES else math.inf


def _grade_row(result: GradeResult) -> Grade:
    return Grade(version=result.version, grade=result.grade.value, points=result.points,
                 text=result.text, detail=result.to_dict())


def _ip(value: object) -> str | None:
    """An IP address without a port ("a.b.c.d:port" -> "a.b.c.d"), or None."""
    text = str(value or "").strip()
    if text.count(":") == 1:
        text = text.split(":")[0]
    return text or None


_LAN = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",  # RFC 1918, loopback, link-local
    "::1/128", "fc00::/7", "fe80::/10"))


def _is_lan(address: str | None) -> bool:
    """A LAN (private), loopback or link-local address."""
    try:
        ip = ipaddress.ip_address(_ip(address) or "")
    except ValueError:
        return False
    return any(ip in net for net in _LAN if net.version == ip.version)
