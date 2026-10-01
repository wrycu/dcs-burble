"""Central service logic: sources, ingest, regrading. No web concerns here."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..acmi import load_recording
from ..dcslog import LsoGrade
from ..detect import PassResult, find_passes
from ..grading import GRADING_VERSION, GradeResult, grade_pass
from .db import Grade, Pass, Pilot, Slice, Source, make_engine, make_sessionmaker
from .storage import SliceStore

START_TIME_TOLERANCE_S = 0.5


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

    def authenticate(self, token: str) -> Source | None:
        with self.sessions() as s:
            return s.scalar(select(Source).where(Source.token_hash == _hash_token(token)))

    # -- passes -----------------------------------------------------------------------------

    def load_pass(self, p: Pass) -> PassResult:
        """Rebuild the pass from its stored slice (plus DCS's grade, which may have arrived later)."""
        recording = load_recording(self.store.path(p.slice.sha256))
        for candidate in find_passes(recording):
            if (candidate.aircraft_id == p.aircraft_id
                    and abs(candidate.start_time - p.start_time) <= START_TIME_TOLERANCE_S):
                candidate.dcs_grade = LsoGrade.parse(p.dcs_grade) if p.dcs_grade else None
                candidate.wire = p.wire
                return candidate
        raise IngestError("pass not found in its slice")

    def ingest(self, source_id: int, data: bytes, sidecar: dict) -> IngestResult:
        try:
            info = sidecar["pass"]
            key = pass_key(sidecar)
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
                g = existing.grade
                return IngestResult(existing.id, False, g.grade if g else "", g.text if g else "")

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
            row = Pass(
                pass_key=key, source_id=source_id, slice=slice_row, pilot=pilot,
                occurred_at=occurred_at(sidecar), mission=(sidecar.get("recording") or {}).get("Title"),
                carrier_type=info["carrier_type"], carrier_unit=info.get("carrier_unit"),
                aircraft_type=info["aircraft_type"], aircraft_id=int(info["aircraft_id"]),
                start_time=float(info["start_time"]), end_time=float(info["end_time"]),
                outcome=info["outcome"], wire=dcs.get("wire"),
                dcs_grade=(dcs.get("grade") or {}).get("raw"),
            )
            try:
                result = grade_pass(self.load_pass(row))
            except (OSError, ValueError, KeyError) as exc:
                raise IngestError(f"could not analyse the slice: {exc}") from exc
            s.add(row)
            s.flush()
            row.grades.append(_grade_row(result))
            return IngestResult(row.id, True, result.grade.value, result.text)

    def regrade(self, force: bool = False) -> tuple[int, int]:
        """Grade every pass with the current grading version. Returns (regraded, unchanged)."""
        done = skipped = 0
        with self.sessions.begin() as s:
            for row in s.scalars(select(Pass)):
                current = next((g for g in row.grades if g.version == GRADING_VERSION), None)
                if current is not None and not force:
                    skipped += 1
                    continue
                result = grade_pass(self.load_pass(row))
                if current is not None:
                    row.grades.remove(current)
                    s.flush()
                row.grades.append(_grade_row(result))
                done += 1
        return done, skipped


def _grade_row(result: GradeResult) -> Grade:
    return Grade(version=result.version, grade=result.grade.value, points=result.points,
                 text=result.text, detail=result.to_dict())
