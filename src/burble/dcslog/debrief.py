"""Read DCS `Logs/debrief.log`, the source of truth for LSO grades and wires.

DCS writes the file when the mission ends. Its `landing quality mark` events carry
the built-in LSO comment, e.g. `LSO: GRADE:C : LNFIW  WIRE# 3`. Event times are
mission time, which lines up with Tacview's recording time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .luatable import as_list, parse_assignments

_WIRE = re.compile(r"WIRE\s*#\s*(\d)")
_GRADE = re.compile(r"GRADE:\s*([^\s:]+)")


@dataclass(frozen=True, slots=True)
class DcsEvent:
    type: str
    time: float
    place: str | None = None
    initiator_pilot: str | None = None
    initiator_unit_type: str | None = None
    initiator_object_id: int | None = None
    initiator_mission_id: str | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class LsoGrade:
    """A parsed DCS `landing quality mark` comment."""

    raw: str
    grade: str | None
    wire: int | None
    remarks: str

    @classmethod
    def parse(cls, comment: str) -> LsoGrade:
        grade = _GRADE.search(comment)
        wire = _WIRE.search(comment)
        remarks = comment.split("GRADE:", 1)[-1]
        remarks = remarks.split(":", 1)[1] if ":" in remarks else ""
        remarks = " ".join(_WIRE.sub("", remarks).split())
        return cls(
            raw=comment,
            grade=grade.group(1) if grade else None,
            wire=int(wire.group(1)) if wire else None,
            remarks=remarks,
        )


@dataclass(frozen=True, slots=True)
class Debrief:
    mission_time: float | None
    events: list[DcsEvent]

    def landing_marks(self) -> list[DcsEvent]:
        return [e for e in self.events if e.type == "landing quality mark" and e.comment]


def _event(raw: dict) -> DcsEvent:
    object_id = raw.get("initiator_object_id")
    return DcsEvent(
        type=raw.get("type", ""),
        time=float(raw.get("t", 0.0)),
        place=raw.get("place") or None,
        initiator_pilot=raw.get("initiatorPilotName") or None,
        initiator_unit_type=raw.get("initiator_unit_type") or None,
        initiator_object_id=int(object_id) if object_id is not None else None,
        initiator_mission_id=raw.get("initiatorMissionID") or None,
        comment=raw.get("comment") or None,
    )


def parse_debrief(text: str) -> Debrief:
    data = parse_assignments(text)
    mission_time = data.get("mission_time")
    return Debrief(
        mission_time=float(mission_time) if mission_time is not None else None,
        events=[_event(e) for e in as_list(data.get("events"))],
    )


def load_debrief(path: str | Path) -> Debrief:
    return parse_debrief(Path(path).read_text(encoding="utf-8", errors="replace"))
