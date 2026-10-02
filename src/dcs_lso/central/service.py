"""Central service logic: sources, ingest, regrading. No web concerns here."""

from __future__ import annotations

import hashlib
import math
import secrets
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ..acmi import ObjectTrack, Recording, Sample, load_recording
from ..dcslog import LsoGrade
from ..detect import PassResult, find_passes
from ..geometry import WindProfile
from ..grading import GRADING_VERSION, GradeResult, grade_pass
from .db import Grade, Pass, Pilot, Slice, Source, make_engine, make_sessionmaker
from .storage import SliceStore

START_TIME_TOLERANCE_S = 0.5
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
class IngestResult:
    pass_id: int
    created: bool
    grade: str
    text: str


class Central:
    def __init__(self, database_url: str, data_dir: str | Path) -> None:
        self.engine = make_engine(database_url)
        self.sessions: sessionmaker[Session] = make_sessionmaker(self.engine)
        self.store = SliceStore(Path(data_dir) / "slices")

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
                )
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
        """Unmerged reports from other sources that could be the same landing as `row`."""
        q = (select(Pass).where(Pass.id != row.id, Pass.source_id != row.source_id, Pass.pilot_id == row.pilot_id,
                                Pass.aircraft_type == row.aircraft_type, Pass.merged_into_id.is_(None))
             .order_by(Pass.id).options(selectinload(Pass.slice)))
        q = q.where(Pass.mission == row.mission) if row.mission is not None else q.where(Pass.mission.is_(None))
        return list(s.scalars(q))

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
