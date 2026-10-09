"""Find carrier passes in a recording and classify their outcome.

`PassTracker` consumes one aircraft sample at a time (with the carrier pose at
that instant) so it can run live on the telemetry stream as well as offline.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ..acmi import ObjectTrack, Recording, Sample, Transform
from ..geometry import AIRCRAFT, CARRIERS, CarrierPose, DeckFrame, DeckWind, WindProfile, air_velocity, body_aoa, centred_velocity
from .wire import estimate_wire

if TYPE_CHECKING:
    from ..dcslog import LsoGrade

NM = 1852.0

# Recovery attempt detection, from DCS-gRPC/lso `is_recovery_attempt`.
MAX_DETECT_ALT_M = 500 * 0.3048
MAX_DETECT_DISTANCE_M = 1.5 * NM
MIN_DETECT_DISTANCE_M = 200.0
MIN_NOSE_TOWARDS_DOT = 0.65

# The pass ends once the aircraft is this much farther from the aim point than its closest approach.
END_RECEDING_M = 150.0
# ...or once it has been stopped on deck for this long.
STOPPED_SPEED_MS = 2.0
STOPPED_DURATION_S = 2.0
# Speed on deck is measured against the position at least this long before: between consecutive samples of a
# pilot's own 40-200 Hz track, position jitter alone reads as several m/s (a trap read as a bolter, 2026-10-05).
STOPPED_BASELINE_S = 0.5
# Passes that never got this low are not worth keeping.
MAX_MIN_HOOK_HEIGHT_M = 30.0

# Hook-on-deck test.
ON_DECK_HOOK_HEIGHT_M = 0.5
ON_DECK_MAX_LATERAL_M = 25.0
ON_DECK_ALONG_RANGE_M = (-250.0, 40.0)
# A track that ends on the deck faster than this was a crash (an arrested jet is nearly stopped by then; a
# bolter's track goes on past the bow).
CRASH_MIN_SPEED_MS = 30.0


class Outcome(StrEnum):
    TRAP = "trap"
    BOLTER = "bolter"
    WAVEOFF = "waveoff"
    CRASH = "crash"  # the jet's track ended on the deck, still at speed (destroyed)
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class PassSample:
    time: float
    along: float
    lateral: float
    hook_height: float
    glideslope_deviation: float
    ground_speed: float
    pitch: float
    aoa: float | None
    aoa_derived: bool


@dataclass(slots=True)
class PassResult:
    carrier_id: int
    carrier_type: str
    aircraft_id: int
    aircraft_type: str
    pilot: str
    outcome: Outcome
    start_time: float
    end_time: float
    samples: list[PassSample] = field(default_factory=list)
    # From DCS's own LSO (never estimated from telemetry); see `dcslog.attach_dcs_grades`.
    wire: int | None = None
    dcs_grade: LsoGrade | None = None
    # The mission's wind at the carrier (from the Burble hook), when known; used for derived AOA.
    wind: WindProfile | None = None
    # The wind over the angled deck as the pass ended, when the mission's wind is known (`wind`).
    deck_wind: DeckWind | None = None
    # The mission's weather settings (the Burble hook's `weather` event), when known: clouds, visibility, etc.
    weather: dict | None = None
    # Estimated from where the jet stopped (detect.wire); DCS's `wire` takes priority when known.
    wire_estimate: int | None = None
    # Which report's aircraft track this result was built from, when the hub merged several.
    track_source: str | None = None

    @property
    def wire_label(self) -> str | None:
        """The wire to show: DCS's own when known, else the estimate, marked as such."""
        if self.wire is not None:
            return f"#{self.wire}"
        return f"#{self.wire_estimate} (est.)" if self.wire_estimate is not None else None

    def to_dict(self) -> dict:
        return asdict(self)


class CarrierTimeline:
    """Carrier pose at arbitrary times, linearly interpolated between samples."""

    def __init__(self, samples: list[Sample]) -> None:
        self._times = [s.time for s in samples]
        self._poses = [CarrierPose.from_transform(s.transform) for s in samples]

    def at(self, time: float) -> CarrierPose:
        i = bisect.bisect_right(self._times, time)
        if i == 0:
            return self._poses[0]
        if i == len(self._times):
            return self._poses[-1]
        t0, t1 = self._times[i - 1], self._times[i]
        a, b = self._poses[i - 1], self._poses[i]
        f = (time - t0) / (t1 - t0)
        dh = (b.heading - a.heading + 180.0) % 360.0 - 180.0
        return CarrierPose(
            u=a.u + (b.u - a.u) * f,
            v=a.v + (b.v - a.v) * f,
            alt=a.alt + (b.alt - a.alt) * f,
            heading=(a.heading + dh * f) % 360.0,
        )


def _forward(heading: float, pitch: float) -> tuple[float, float, float]:
    h, p = math.radians(heading), math.radians(pitch)
    return math.sin(h) * math.cos(p), math.sin(p), math.cos(h) * math.cos(p)


def is_recovery_attempt(carrier: CarrierPose, plane: Transform) -> bool:
    if (plane.alt or 0.0) > MAX_DETECT_ALT_M:
        return False
    ray = ((carrier.u - (plane.u or 0.0)), carrier.alt - (plane.alt or 0.0), carrier.v - (plane.v or 0.0))
    dist = math.sqrt(sum(c * c for c in ray))
    if not MIN_DETECT_DISTANCE_M <= dist <= MAX_DETECT_DISTANCE_M:
        return False
    ray_n = tuple(c / dist for c in ray)
    carrier_fwd = _forward(carrier.heading, 0.0)
    if sum(a * b for a, b in zip(carrier_fwd, ray_n)) < 0.0:
        return False  # not behind the carrier
    plane_fwd = _forward(plane.heading or 0.0, plane.pitch or 0.0)
    return sum(a * b for a, b in zip(plane_fwd, ray_n)) >= MIN_NOSE_TOWARDS_DOT


class PassTracker:
    """Accumulates one pass. Feed samples until `feed()` returns False."""

    def __init__(self, frame: DeckFrame, wind: WindProfile | None = None) -> None:
        self.frame = frame
        self.wind = wind
        self._raw: list[tuple[float, CarrierPose, Transform, float | None]] = []
        self._min_distance = math.inf
        self._stopped_since: float | None = None
        self.stopped = False

    def feed(self, time: float, carrier: CarrierPose, plane: Transform, aoa: float | None) -> bool:
        self._raw.append((time, carrier, plane, aoa))
        pos = self.frame.position(carrier, plane)
        distance = math.hypot(pos.along, pos.lateral)
        self._min_distance = min(self._min_distance, distance)
        if distance - self._min_distance > END_RECEDING_M:
            return False
        earlier = next((r for r in reversed(self._raw[:-1]) if time - r[0] >= STOPPED_BASELINE_S), None)
        if earlier is not None:
            t0, c0, p0, _ = earlier
            a = self.frame.position(c0, p0)
            dt = time - t0
            if dt > 0 and math.hypot(pos.along - a.along, pos.lateral - a.lateral) / dt < STOPPED_SPEED_MS:
                if self._stopped_since is None:
                    self._stopped_since = t0
                if time - self._stopped_since >= STOPPED_DURATION_S:
                    self.stopped = True
                    return False
            else:
                self._stopped_since = None
        return True

    def samples(self) -> list[PassSample]:
        raw = self._raw
        positions = [self.frame.position(c, p) for _, c, p, _ in raw]
        out: list[PassSample] = []
        for i, ((t, _, plane, aoa), pos) in enumerate(zip(raw, positions)):
            # Hub differences (one-sided at the ends).
            j, k = max(i - 1, 0), min(i + 1, len(raw) - 1)
            dt = raw[k][0] - raw[j][0]
            pj, pk = raw[j][2], raw[k][2]
            ground_speed = math.hypot(positions[k].along - positions[j].along,
                                      positions[k].lateral - positions[j].lateral) / dt if dt > 0 else 0.0
            pitch = plane.pitch or 0.0
            # Without recorded AOA, derive it from the motion (see geometry.aoa).
            derived = aoa is None
            if derived:
                aoa = self._derived_aoa(raw[j], raw[i], raw[k]) if j < i < k else None
            out.append(PassSample(
                time=t,
                along=pos.along,
                lateral=pos.lateral,
                hook_height=pos.hook_height,
                glideslope_deviation=pos.hook_height - self.frame.glideslope_height(pos.along),
                ground_speed=ground_speed,
                pitch=pitch,
                aoa=aoa,
                aoa_derived=derived,
            ))
        return out

    def _derived_aoa(self, before, at, after) -> float | None:
        def where(p: Transform) -> tuple[float, float, float]:
            return p.u or 0.0, p.v or 0.0, p.alt or 0.0
        ground = centred_velocity(before[0], where(before[2]), at[0], where(at[2]), after[0], where(after[2]))
        if ground is None:
            return None
        plane = at[2]
        wind = self.wind.at(plane.alt or 0.0) if self.wind else (0.0, 0.0)
        aoa = body_aoa(air_velocity(ground, wind), plane.heading, plane.pitch or 0.0, plane.roll or 0.0)
        return aoa + self.frame.aircraft.derived_aoa_offset if aoa is not None else None


def _on_deck(s: PassSample) -> bool:
    lo, hi = ON_DECK_ALONG_RANGE_M
    return (s.hook_height < ON_DECK_HOOK_HEIGHT_M
            and abs(s.lateral) < ON_DECK_MAX_LATERAL_M
            and lo < s.along < hi)


def classify(samples: list[PassSample], stopped: bool, track_ended: bool = False) -> Outcome:
    """`track_ended`: the aircraft's track ends with the pass (nothing of it after its last sample)."""
    touched = any(_on_deck(s) for s in samples)
    if track_ended and samples and _on_deck(samples[-1]) and samples[-1].ground_speed > CRASH_MIN_SPEED_MS:
        return Outcome.CRASH
    if stopped and touched:
        return Outcome.TRAP
    if touched:
        return Outcome.BOLTER
    if samples and samples[-1].along < 0:
        return Outcome.WAVEOFF
    return Outcome.INCOMPLETE


def find_passes(recording: Recording, wind: WindProfile | None = None) -> Iterator[PassResult]:
    """Every carrier pass in `recording`. `wind` (at the carrier) refines AOA derived from motion."""
    carriers = [t for t in recording.objects.values() if t.name in CARRIERS and t.samples]
    # Each object separately where Tacview reused an id (ObjectTrack.lives).
    aircraft = [life for t in recording.objects.values() for life in t.lives()
                if life.name in AIRCRAFT and life.samples]
    for carrier in carriers:
        timeline = CarrierTimeline(carrier.samples)
        for plane in aircraft:
            frame = DeckFrame(CARRIERS[carrier.name], AIRCRAFT[plane.name])
            yield from _passes_for_pair(carrier, plane, timeline, frame, wind)


def _passes_for_pair(carrier: ObjectTrack, plane: ObjectTrack, timeline: CarrierTimeline,
                     frame: DeckFrame, wind: WindProfile | None = None) -> Iterator[PassResult]:
    tracker: PassTracker | None = None
    for sample in plane.samples:
        pose = timeline.at(sample.time)
        if tracker is None:
            if not is_recovery_attempt(pose, sample.transform):
                continue
            tracker = PassTracker(frame, wind)
        if not tracker.feed(sample.time, pose, sample.transform, sample.aoa):
            result = _finish(carrier, plane, frame, tracker, timeline=timeline)
            if result:
                yield result
            tracker = None
    if tracker is not None:  # the aircraft's track ended during the pass
        result = _finish(carrier, plane, frame, tracker, track_ended=True, timeline=timeline)
        if result:
            yield result


DECK_WIND_SPAN_S = 10.0  # the carrier's velocity: its movement over the last this many seconds of the pass


def _deck_wind(timeline: CarrierTimeline | None, frame: DeckFrame, wind: WindProfile | None,
               end: float) -> DeckWind | None:
    if wind is None or timeline is None:
        return None
    a, b = timeline.at(end - DECK_WIND_SPAN_S), timeline.at(end)
    ship = ((b.u - a.u) / DECK_WIND_SPAN_S, (b.v - a.v) / DECK_WIND_SPAN_S)
    return DeckWind.at(wind, ship, b.heading, frame.carrier.deck_angle)


def _finish(carrier: ObjectTrack, plane: ObjectTrack, frame: DeckFrame,
            tracker: PassTracker, track_ended: bool = False,
            timeline: CarrierTimeline | None = None) -> PassResult | None:
    samples = tracker.samples()
    if not samples or min(s.hook_height for s in samples) > MAX_MIN_HOOK_HEIGHT_M:
        return None
    outcome = classify(samples, tracker.stopped, track_ended)
    return PassResult(
        carrier_id=carrier.id,
        carrier_type=carrier.name,
        aircraft_id=plane.id,
        aircraft_type=plane.name,
        pilot=plane.pilot,
        outcome=outcome,
        start_time=samples[0].time,
        end_time=samples[-1].time,
        samples=samples,
        wind=tracker.wind,
        deck_wind=_deck_wind(timeline, frame, tracker.wind, samples[-1].time),
        wire_estimate=estimate_wire(samples, frame) if outcome is Outcome.TRAP else None,
    )
