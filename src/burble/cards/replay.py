"""The data for a pass's 3D replay (hub page /passes/{id}/replay): the jet's track in the carrier's frame.

Everything is carrier-local, as in geometry.data: x starboard, y up (meters above the deck), z forward, from the
carrier model's origin; the jet's heading is relative to the ship's. The carrier sits still and the sea moves past
it (`carrier.speed`), so the LSO's view is steady. Times count from `start` (seconds); the page interpolates.
"""

from __future__ import annotations

import math
import statistics

from ..acmi import Recording
from ..callouts.voice import spoken_text
from ..detect import PassResult
from ..detect.passes import CarrierTimeline
from ..geometry import AIRCRAFT, CARRIERS, DeckFrame
from ..grading import GradeResult, grade_name
from .svg import _said_order, call_label

# The replay starts this long before the pass (its detection, about 1.5 nm out) and ends this long after.
LEAD_S = 8.0
TAIL_S = 4.0

# The page's model for each aircraft type (anything else: the Hornet's).
MODELS = {"FA-18C_hornet": "hornet", "F-14A-135-GR": "tomcat", "F-14B": "tomcat", "F-14BU": "tomcat"}


# Calls further than this off the track's clock (see _calls_offset) are moved onto it.
CALLS_OFFSET_MAX_S = 1.0


def _r(v: float, places: int) -> float:
    return round(v, places)


def _calls_offset(result: PassResult, calls: list[dict]) -> float:
    """How far the calls' times are behind the track's clock: the median, over calls made in the groove, of when
    the track was where the call was made (its `along`, as the trap card places it) less the call's time. About 0
    normally; 5-13 s on landings from the first pilot hook (2026-10-05), whose calls were on another clock."""
    samples = result.samples
    if not samples:
        return 0.0
    near, far = min(s.along for s in samples), max(s.along for s in samples)
    offsets = [min(samples, key=lambda s: abs(s.along - c["along"])).time - c["time"]
               for c in calls if "along" in c and "time" in c and near <= c["along"] <= far]
    return statistics.median(offsets) if offsets else 0.0


def ordered_calls(calls: list[dict] | None) -> list[dict]:
    """A pass's recorded calls in the order said, as the replay lists them (its call audio is by this index)."""
    return [c for c in sorted(calls or [], key=_said_order) if "time" in c]


def replay_data(recording: Recording, result: PassResult, grade: GradeResult | None = None,
                calls: list[dict] | None = None, night: bool = False, title: str = "") -> dict:
    """The replay of `result`, found in `recording` (its `aircraft_id` and `carrier_id` are objects there)."""
    carrier_track = recording.objects[result.carrier_id]
    plane_track = recording.objects[result.aircraft_id]
    info, aircraft = CARRIERS[result.carrier_type], AIRCRAFT[result.aircraft_type]
    frame = DeckFrame(info, aircraft)
    timeline = CarrierTimeline(carrier_track.samples)

    start, end = result.start_time - LEAD_S, result.end_time + TAIL_S
    track: dict[str, list[float]] = {k: [] for k in ("t", "x", "y", "z", "heading", "pitch", "roll")}
    last_heading: float | None = None
    for sample in plane_track.samples:
        if not start <= sample.time <= end:
            continue
        t, pose = sample.transform, timeline.at(sample.time)
        x, z = pose.to_local(t.u or 0.0, t.v or 0.0)
        heading = ((t.heading or 0.0) - pose.heading + 180.0) % 360.0 - 180.0
        if last_heading is not None:  # unwrapped, so the page can interpolate it
            heading = last_heading + (heading - last_heading + 180.0) % 360.0 - 180.0
        last_heading = heading
        for key, value, places in (("t", sample.time - start, 3), ("x", x, 2), ("z", z, 2),
                                   ("y", (t.alt or 0.0) - pose.alt - info.deck_altitude, 2),
                                   ("heading", heading, 2), ("pitch", t.pitch or 0.0, 2), ("roll", t.roll or 0.0, 2)):
            track[key].append(_r(value, places))

    # The ship's speed through the water over the replay (the sea is drawn moving past it).
    a, b = timeline.at(start), timeline.at(end)
    speed = math.hypot(b.u - a.u, b.v - a.v) / (end - start) if end > start else 0.0

    groove = {k: [] for k in ("t", "along", "lateral", "glideslope", "aoa")}
    for s in result.samples:
        for key, value, places in (("t", s.time - start, 3), ("along", s.along, 1), ("lateral", s.lateral, 2),
                                   ("glideslope", s.glideslope_deviation, 2)):
            groove[key].append(_r(value, places))
        groove["aoa"].append(None if s.aoa is None else _r(s.aoa + (aircraft.derived_aoa_offset if s.aoa_derived
                                                                     else 0.0), 2))

    def local(along: float, lateral: float = 0.0) -> list[float]:
        """A landing-area point (meters short of the aim point, right of the centerline) as carrier-local x, z."""
        ax, az = frame.axis
        return [_r(frame.aim[0] - along * ax + lateral * az, 2), _r(frame.aim[1] - along * az - lateral * ax, 2)]

    calls = ordered_calls(calls)
    offset = _calls_offset(result, calls)
    if abs(offset) <= CALLS_OFFSET_MAX_S:
        offset = 0.0

    return {
        "title": title,
        "start": _r(start, 3),
        "pass": [_r(result.start_time - start, 3), _r(result.end_time - start, 3)],
        "outcome": str(result.outcome),
        "night": night,
        "grade": None if grade is None else {"grade": grade.grade, "name": grade_name(grade.grade),
                                             "text": grade.text},
        "wire": result.wire_label,
        "aircraft": {"type": result.aircraft_type, "model": MODELS.get(result.aircraft_type, "hornet"),
                     "hook": list(aircraft.hook), "on_speed_aoa": list(aircraft.on_speed_aoa)},
        "carrier": {
            "type": result.carrier_type,
            "deck_angle": info.deck_angle,
            "deck_altitude": info.deck_altitude,
            "wires": [[list(port), list(stbd)] for port, stbd in info.wires],
            "caught": result.wire if result.wire is not None else result.wire_estimate,
            "aim": [_r(v, 2) for v in frame.aim],
            "axis": [_r(v, 5) for v in frame.axis],
            "ramp": local(info.ramp_along_m),
            "landing_area_end": local(info.landing_area_forward_m),
            "glideslope": aircraft.glideslope,
            "speed": _r(speed, 2),
        },
        "track": track,
        "groove": groove,
        # `say`: what the LSO said, to be spoken (the pilot's calls are shown, not spoken).
        "calls": [{"t": _r(c["time"] + offset - start, 2), "text": call_label(c), "by": c.get("by", "lso"),
                   **({} if c.get("by") == "pilot" else {"say": spoken_text(c)})} for c in calls],
    }
