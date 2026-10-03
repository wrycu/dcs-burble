"""Central service logic: sources, ingest, regrading. No web concerns here."""

from __future__ import annotations

import hashlib
import math
import secrets
import statistics
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..acmi import ObjectTrack, Recording, Sample, load_recording
from ..dcslog import Debrief, LsoGrade, load_debrief
from ..detect import PassResult, find_passes
from ..geometry import AIRCRAFT, WindProfile
from ..slices import own_pilots, slice_recording
from ..cards.overlay import OverlayPass
from ..grading import GRADING_VERSION, GradeResult, grade_name, grade_pass
from ..grading.trends import DEFAULT_PASSES, TrendPass, Trends, trends
from .db import Grade, Pass, Pilot, Slice, Source, Upload, make_engine, make_sessionmaker
from .passwords import MIN_LENGTH as MIN_PASSWORD_LENGTH, FailureLimiter, hash_password, verify_password
from .storage import SliceStore

START_TIME_TOLERANCE_S = 0.5
PUBLIC_UPLOADS = "uploads"  # source of recordings uploaded without a token
UPLOAD_PILOT_WAIT = timedelta(days=1)  # how long an upload waits for its uploader to pick a pilot
UPLOAD_PASSWORD_ATTEMPTS = 5  # wrong pilot passwords before an upload is given up
# Merging reports of one landing (see `Central.ingest`).
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


class Central:
    def __init__(self, database_url: str, data_dir: str | Path, require_upload_token: bool = False) -> None:
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

    # -- sources ----------------------------------------------------------------------------

    def add_source(self, name: str, kind: str = "server") -> str:
        """Create an upload source; returns its token (only the hash is stored)."""
        if kind not in ("server", "pilot"):
            raise ValueError("kind must be 'server' or 'pilot'")
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            s.add(Source(name=name, kind=kind, token_hash=_hash_token(token)))
        return token

    def set_config(self, name: str, config: dict) -> None:
        with self.sessions.begin() as s:
            source = s.scalar(select(Source).where(Source.name == name))
            if source is None:
                raise ValueError(f"no source named {name!r}")
            source.config = config

    def authenticate(self, token: str) -> Source | None:
        with self.sessions() as s:
            return s.scalar(select(Source).where(Source.token_hash == _hash_token(token)))

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

    def load_pass(self, p: Pass, reports: list[Pass] | None = None) -> PassResult:
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
            recording, aircraft_id = self._with_track(recording, p, best)
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

    def _with_track(self, recording: Recording, landing: Pass, other: Pass) -> tuple[Recording, int]:
        """`recording` with the landing's aircraft track replaced by `other`'s, moved onto this
        recording's clock. Both count from their own ReferenceTime (mission time)."""
        theirs = load_recording(self.store.path(other.slice.sha256))
        offset = _reference_offset(theirs.globals, recording.globals)
        track = theirs.objects[other.aircraft_id]
        objects = {i: t for i, t in recording.objects.items() if i != landing.aircraft_id}
        new_id = other.aircraft_id if other.aircraft_id not in objects else max(objects) + 1
        objects[new_id] = ObjectTrack(new_id, dict(track.props),
                                      [Sample(x.time + offset, x.transform, x.aoa) for x in track.samples])
        return Recording(recording.globals, objects, recording.first_frame), new_id

    def _same_landing(self, a: Pass, b: Pass) -> bool:
        """Two reports of the same landing: same pilot, aircraft and mission (checked by the caller),
        overlapping in mission time, and the aircraft in the same place at the same moments."""
        wa, wb = _mission_window(a), _mission_window(b)
        if wa is None or wb is None or not (wa[0] < wb[1] and wb[0] < wa[1]):
            return False
        ra = load_recording(self.store.path(a.slice.sha256))
        rb = load_recording(self.store.path(b.slice.sha256))
        offset = _reference_offset(rb.globals, ra.globals)
        ta, tb = ra.objects.get(a.aircraft_id), rb.objects.get(b.aircraft_id)
        if ta is None or tb is None:
            return False
        return _median_distance(ta.samples, tb.samples, offset) <= SAME_POSITION_M

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
        result = grade_pass(self.load_pass(row, self.reports(row, s)))
        current = next((g for g in row.grades if g.version == result.version), None)
        if current is not None:
            row.grades.remove(current)
            s.flush()
        row.grades.append(_grade_row(result))
        return result

    def ingest(self, source_id: int, data: bytes, sidecar: dict) -> IngestResult:
        try:
            info = sidecar["pass"]
            key = pass_key(sidecar)
            kind = sidecar.get("kind") or "pass"
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestError(f"invalid sidecar: {exc}") from exc
        with self.sessions.begin() as s:
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

            name = info.get("pilot") or f"id {int(info['aircraft_id']):x}"
            pilot = s.scalar(select(Pilot).where(Pilot.name == name))
            if pilot is None:
                pilot = Pilot(name=name)
                s.add(pilot)
                s.flush()

            dcs = sidecar.get("dcs") or {}
            try:
                row = Pass(
                    pass_key=key, source_id=source_id, slice=slice_row, pilot=pilot, kind=kind,
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
            except (OSError, ValueError, KeyError) as exc:
                raise IngestError(f"could not analyse the slice: {exc}") from exc
            s.add(row)
            s.flush()

            matches = [m for m in self._candidates(s, row) if self._same_landing(m, row)]
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

    def _candidates(self, s: Session, row: Pass) -> list[Pass]:
        """Unmerged reports that could be the same landing as `row`: from other sources, or from the same
        one (e.g. a server's Tacview file backfilled after its collector already sent the pass live)."""
        q = (select(Pass).where(Pass.id != row.id, Pass.pilot_id == row.pilot_id,
                                Pass.aircraft_type == row.aircraft_type, Pass.merged_into_id.is_(None))
             .order_by(Pass.id).options(selectinload(Pass.slice)))
        q = q.where(Pass.mission == row.mission) if row.mission is not None else q.where(Pass.mission.is_(None))
        return list(s.scalars(q))

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
            pilots = own_pilots(load_recording(path))
            error = None
        except Exception as exc:  # a bad file must not take the worker down
            pilots, error = [], f"could not read the recording: {exc}"[:500]
        with self.sessions.begin() as s:
            upload = s.get(Upload, upload_id)
            upload.pilots = pilots
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
                self._regrade(s, row)
                done += 1
        return done, skipped


def _detail(report: Pass, is_landing: bool) -> tuple:
    """How detailed a report's aircraft track is: recorded AOA first, then sample rate."""
    info = (report.slice.sidecar or {}).get("pass") or {}
    return bool(info.get("aoa_recorded")), float(info.get("sample_rate_hz") or 0.0), is_landing


def _reference(globals_: dict) -> datetime | None:
    return _parse_time(globals_.get("ReferenceTime"))


def _reference_offset(theirs: dict, ours: dict) -> float:
    """Seconds to add to a time in `theirs` to get the same moment in `ours`."""
    a, b = _reference(theirs), _reference(ours)
    return (a - b).total_seconds() if a is not None and b is not None else 0.0


def _mission_window(p: Pass) -> tuple[datetime, datetime] | None:
    """When the report's pass (or a track report's whole window) happened, in mission time."""
    sidecar = p.slice.sidecar or {}
    ref = _parse_time((sidecar.get("recording") or {}).get("ReferenceTime"))
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
