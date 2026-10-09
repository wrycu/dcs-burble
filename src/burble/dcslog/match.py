"""Attach DCS LSO grades (and so the wire) to passes found in Tacview data."""

from __future__ import annotations

from ..acmi import Recording
from ..detect import PassResult
from .debrief import Debrief, DcsEvent, LsoGrade

# DCS posts the grade near the end of the rollout, which can fall shortly after
# the pass tracker has already stopped.
GRADE_AFTER_PASS_S = 15.0


def tacview_id_hint(dcs_object_id: int) -> int:
    """Likely Tacview object ID for a DCS object ID.

    Holds for every aircraft checked so far (6 objects across a single-player and
    a hosted multiplayer session, e.g. DCS 0x1002D01 -> Tacview 0x2D02). Still
    only used to break ties until it's confirmed on a dedicated server.
    """
    return (dcs_object_id & 0xFFFFFF) + 1


def _candidates(p: PassResult, carrier_unit: str, marks: list[DcsEvent]) -> list[DcsEvent]:
    return [
        e for e in marks
        if p.start_time <= e.time <= p.end_time + GRADE_AFTER_PASS_S
        and e.initiator_unit_type == p.aircraft_type
        and (e.place is None or e.place == carrier_unit)
    ]


def _score(p: PassResult, e: DcsEvent) -> tuple[bool, bool, float]:
    id_match = e.initiator_object_id is not None and tacview_id_hint(e.initiator_object_id) == p.aircraft_id
    pilot_match = bool(p.pilot) and e.initiator_pilot == p.pilot
    return id_match, pilot_match, -abs(e.time - p.end_time)


def attach_dcs_grades(passes: list[PassResult], recording: Recording, debrief: Debrief) -> None:
    """Fill `dcs_grade` and `wire` on each pass from the matching DCS grade event."""
    unused = debrief.landing_marks()
    for p in sorted(passes, key=lambda p: p.end_time):
        carrier = recording.objects.get(p.carrier_id)
        # Tacview stores an AI unit's name as its Pilot; DCS reports it as `place`.
        carrier_unit = carrier.pilot if carrier else ""
        candidates = _candidates(p, carrier_unit, unused)
        if not candidates:
            continue
        event = max(candidates, key=lambda e: _score(p, e))
        unused.remove(event)
        grade = LsoGrade.parse(event.comment or "")
        p.dcs_grade = grade
        p.wire = grade.wire


# Debrief events marking when a player took (or spawned in) an aircraft: the aircraft's first moment.
_SPAWN_EVENTS = ("under control", "birth")


def track_dcs_grades(recording: Recording, approaches: list, debrief: Debrief,
                     first_seen: dict[int, float] | None = None) -> dict:
    """DCS's grade for each own-track approach (`detect.approaches.Approach`), keyed by approach.

    A multiplayer client's recording starts when the client joined, while debrief times are mission
    time; the offset comes from the aircraft's spawn ("under control"/"birth") against its first
    sample in the recording (or `first_seen`, when the recording is only a slice of the session).
    Recordings that start with the mission need no offset.
    """
    marks = debrief.landing_marks()
    found = {}
    for a in approaches:
        track = recording.objects.get(a.aircraft_id)
        if track is None or not track.samples:
            continue
        own = [e for e in marks if e.initiator_object_id is not None
               and tacview_id_hint(e.initiator_object_id) == a.aircraft_id]
        spawn = next((e for e in debrief.events if e.type in _SPAWN_EVENTS and e.initiator_object_id is not None
                      and tacview_id_hint(e.initiator_object_id) == a.aircraft_id), None)
        appeared = (first_seen or {}).get(a.aircraft_id, track.samples[0].time)
        offset = spawn.time - appeared if spawn is not None else 0.0
        hits = [e for e in own if a.start_time <= e.time - offset <= a.end_time + GRADE_AFTER_PASS_S]
        if hits:
            found[a] = LsoGrade.parse(hits[0].comment or "")
    return found
