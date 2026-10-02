"""Decide LSO calls from live groove estimates.

The phrase set, escalation and hold times follow DCS's own LSO (its phrases and ED's trigger
notes in `Scripts/Speech/common_events.lua`). The deviation bands are ours (angles seen from
the aim point), tuned against recorded passes; ED's notes give degrees that can't be the same
measure (e.g. "high" at 5 degrees above the glidepath).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .estimator import GrooveState

NM = 1852.0


class Call(StrEnum):
    WAVE_OFF = "wave off"
    WAVE_OFF_GEAR = "wave off, gear"
    BOLTER = "bolter"  # decided by the bolter detector in the live collector, not by CalloutEngine
    POWER_X3 = "power, power, power"
    POWER_X2 = "power, power"
    POWER = "power"
    LOW = "you're low"
    HIGH = "you're high"
    EASY_WITH_IT = "easy with it"
    RIGHT_FOR_LINEUP = "right for lineup"
    COME_LEFT = "come left"
    GOING_LOW = "you're going low"
    LITTLE_LOW = "you're a little low"
    LITTLE_HIGH = "you're a little high"
    GOING_HIGH = "you're going high"
    LITTLE_RIGHT = "a little right for lineup"
    LITTLE_LEFT = "a little come left"
    DRIFTING_LEFT = "you're drifting left"
    DRIFTING_RIGHT = "you're drifting right"
    EASY_WINGS = "easy with your wings"
    EASY_NOSE = "easy with the nose"
    FAST = "you're fast"
    SLOW = "you're slow"


# Lower number = more urgent (declaration order above).
PRIORITY = {call: i for i, call in enumerate(Call)}
POWER_CALLS = frozenset({Call.POWER, Call.POWER_X2, Call.POWER_X3})
WAVE_OFFS = frozenset({Call.WAVE_OFF, Call.WAVE_OFF_GEAR})


@dataclass(frozen=True, slots=True)
class Thresholds:
    groove_start_m: float = 0.75 * NM
    # In the groove once out of the turn to final: wings roughly level and pointing roughly
    # down the landing area (lineup itself is what we call, so it isn't required)...
    groove_heading_deg: float = 15.0
    groove_roll_deg: float = 10.0
    # ...for this long...
    groove_settle_s: float = 1.0
    # ...or, regardless, once this close.
    groove_always_inside_m: float = 0.4 * NM
    # Inside this the pilot is at the ramp: no calls at all (too late to act on them).
    quiet_inside_m: float = 120.0
    wave_off_inside_m: float = 0.25 * NM
    gear_wave_off_inside_m: float = 0.5 * NM
    in_close_m: float = 0.25 * NM
    # Glideslope deviation bands, degrees: "a little" / plain.
    little_deg: float = 0.35
    deviation_deg: float = 0.7
    power_x3_deg: float = 1.0  # this low in close: "power, power, power"
    wave_off_low_deg: float = 1.2
    sinking_deg_s: float = 0.3  # a little low and sinking at least this fast: power
    going_deg_s: float = 0.15  # on glideslope but trending off it this fast: going high/low
    easy_with_it_deg_s: float = 0.6  # climbing this fast right after a power call: easy with it
    power_escalate_s: float = 6.0  # another power call within this: "power, power"
    # Lineup bands, degrees.
    little_lineup_deg: float = 0.5
    lineup_deg: float = 1.0
    drifting_deg_s: float = 0.2
    wave_off_lateral_m: float = 12.0
    # Attitude (DCS: pitch changing > 5 deg/s, roll > 20 deg).
    easy_nose_deg_s: float = 5.0
    easy_wings_deg: float = 20.0
    # AOA: outside the aircraft's on-speed band (DCS: 7.4 / 8.8 for the FA-18C).
    aoa_fast: float = 7.4
    aoa_slow: float = 8.8
    # Hysteresis: an active call stays active until the value is this far back inside.
    aoa_hysteresis: float = 0.3
    angle_hysteresis_deg: float = 0.1
    # A deviation is "being corrected" when its rate toward zero is at least this.
    correcting_deg_s: float = 0.05
    # A condition must hold this long before it is called (DCS: lineup 3 s, speed 4 s)...
    hold_s: float = 0.4
    lineup_hold_s: float = 3.0
    speed_hold_s: float = 4.0
    # ...the same call is not repeated within this time (and never while being corrected)...
    repeat_s: float = 2.5
    # ...and no two calls are made closer together than this (a call takes time to say).
    spacing_s: float = 1.2

    def hold_for(self, call: Call) -> float:
        if call in (Call.RIGHT_FOR_LINEUP, Call.COME_LEFT):
            return self.lineup_hold_s
        if call in (Call.FAST, Call.SLOW):
            return self.speed_hold_s
        return self.hold_s


@dataclass(frozen=True, slots=True)
class CallEvent:
    time: float
    along: float
    call: Call
    state: GrooveState


def conditions(s: GrooveState, th: Thresholds, previous: frozenset[Call] = frozenset(),
               recent_power: bool = False) -> set[Call]:
    """Calls whose condition is true right now (before persistence/suppression).

    `previous` gives hysteresis: a call that was active stays active until its value is
    comfortably back inside the threshold. `recent_power`: a power call was made moments ago.
    """
    def edge(call: Call, value: float, margin: float) -> float:
        return value - margin if call in previous else value

    h = th.angle_hysteresis_deg
    active: set[Call] = set()
    gs, rate = s.glideslope_deg, s.glideslope_rate
    if th.quiet_inside_m < s.along <= th.wave_off_inside_m and (
            gs <= -th.wave_off_low_deg or abs(s.lateral_m) >= th.wave_off_lateral_m):
        active.add(Call.WAVE_OFF)
    if s.gear is not None and s.gear < 0.5 and 0 < s.along <= th.gear_wave_off_inside_m:
        active.add(Call.WAVE_OFF_GEAR)
    if s.along <= th.quiet_inside_m:
        return active

    # Glideslope: one call at a time, most serious first.
    if recent_power and rate >= th.easy_with_it_deg_s:
        active.add(Call.EASY_WITH_IT)
    elif gs <= -th.power_x3_deg and s.along <= th.in_close_m:
        active.add(Call.POWER_X3)
    elif gs <= -edge(Call.LOW, th.deviation_deg, h):
        active.add(Call.POWER if rate <= 0 else Call.LOW)
    elif gs <= -edge(Call.LITTLE_LOW, th.little_deg, h):
        active.add(Call.POWER if rate <= -th.sinking_deg_s else Call.LITTLE_LOW)
    elif gs >= edge(Call.HIGH, th.deviation_deg, h):
        active.add(Call.HIGH)
    elif gs >= edge(Call.LITTLE_HIGH, th.little_deg, h):
        active.add(Call.LITTLE_HIGH)
    elif rate <= -th.going_deg_s:
        active.add(Call.GOING_LOW)
    elif rate >= th.going_deg_s:
        active.add(Call.GOING_HIGH)

    # Lineup (+ is right of centerline).
    lu, lu_rate = s.lineup_deg, s.lineup_rate
    if lu <= -edge(Call.RIGHT_FOR_LINEUP, th.lineup_deg, h):
        active.add(Call.RIGHT_FOR_LINEUP)
    elif lu >= edge(Call.COME_LEFT, th.lineup_deg, h):
        active.add(Call.COME_LEFT)
    elif lu <= -edge(Call.LITTLE_RIGHT, th.little_lineup_deg, h):
        active.add(Call.LITTLE_RIGHT)
    elif lu >= edge(Call.LITTLE_LEFT, th.little_lineup_deg, h):
        active.add(Call.LITTLE_LEFT)
    elif lu_rate <= -th.drifting_deg_s:
        active.add(Call.DRIFTING_LEFT)
    elif lu_rate >= th.drifting_deg_s:
        active.add(Call.DRIFTING_RIGHT)

    # Attitude.
    if abs(s.roll) >= th.easy_wings_deg:
        active.add(Call.EASY_WINGS)
    if abs(s.pitch_rate) >= th.easy_nose_deg_s:
        active.add(Call.EASY_NOSE)

    # Speed.
    if s.aoa is not None:
        a = th.aoa_hysteresis
        if s.aoa <= edge(Call.FAST, th.aoa_fast, -a):
            active.add(Call.FAST)
        elif s.aoa >= edge(Call.SLOW, th.aoa_slow, a):
            active.add(Call.SLOW)
    return active


def correcting(call: Call, s: GrooveState, th: Thresholds) -> bool:
    """Is the pilot already fixing the deviation this call is about?"""
    c = th.correcting_deg_s
    if call in POWER_CALLS or call in (Call.LOW, Call.LITTLE_LOW, Call.GOING_LOW):
        return s.glideslope_rate >= c
    if call in (Call.HIGH, Call.LITTLE_HIGH, Call.GOING_HIGH):
        return s.glideslope_rate <= -c
    if call in (Call.RIGHT_FOR_LINEUP, Call.LITTLE_RIGHT, Call.DRIFTING_LEFT):
        return s.lineup_rate >= c
    if call in (Call.COME_LEFT, Call.LITTLE_LEFT, Call.DRIFTING_RIGHT):
        return s.lineup_rate <= -c
    return False


@dataclass
class CalloutEngine:
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Condition flips seen (a measure of chatter, before persistence smooths it out).
    toggles: int = 0

    def __post_init__(self) -> None:
        self._since: dict[Call, float] = {}
        self._last_said: dict[Call, float] = {}
        self._last_power: float | None = None
        self._last_any = float("-inf")
        self._previous: frozenset[Call] = frozenset()
        self._aligned_since: float | None = None
        self.in_groove = False
        self._waved_off = False

    def _update_groove(self, s: GrooveState) -> None:
        th = self.thresholds
        if self.in_groove:
            if s.along > th.groove_start_m * 1.2:  # flew back out: a new approach must re-qualify
                self.in_groove = False
                self._aligned_since = None
            return
        if 0 < s.along <= th.groove_always_inside_m:
            self.in_groove = True
            return
        aligned = (0 < s.along <= th.groove_start_m and abs(s.heading_error) <= th.groove_heading_deg
                   and abs(s.roll) <= th.groove_roll_deg)
        if not aligned:
            self._aligned_since = None
        elif self._aligned_since is None:
            self._aligned_since = s.time
        elif s.time - self._aligned_since >= th.groove_settle_s:
            self.in_groove = True

    def update(self, s: GrooveState) -> CallEvent | None:
        th = self.thresholds
        self._update_groove(s)
        recent_power = self._last_power is not None and s.time - self._last_power <= 4.0
        active = (conditions(s, th, self._previous, recent_power)
                  if self.in_groove and s.along > 0 else set())
        self.toggles += len(active ^ self._previous)
        self._previous = frozenset(active)
        for call in list(self._since):
            if call not in active:
                del self._since[call]
        for call in active:
            self._since.setdefault(call, s.time)
        if self._waved_off:
            return None
        ready = []
        for c in active:
            if s.time - self._since[c] < th.hold_for(c):
                continue
            said = self._last_said.get(c)
            if said is not None and (s.time - said < th.repeat_s or correcting(c, s, th)):
                continue
            ready.append(c)
        if not ready:
            return None
        call = min(ready, key=PRIORITY.__getitem__)
        # A wave-off interrupts anything; other calls wait for the previous one to finish.
        if call not in WAVE_OFFS and s.time - self._last_any < th.spacing_s:
            return None
        said_as = call
        if call is Call.POWER and self._last_power is not None and s.time - self._last_power <= th.power_escalate_s:
            said_as = Call.POWER_X2  # DCS: a second power call, "with more annoyed inflection"
        self._last_said[call] = s.time
        self._last_any = s.time
        if said_as in POWER_CALLS:
            self._last_power = s.time
        if said_as in WAVE_OFFS:
            self._waved_off = True
        return CallEvent(s.time, s.along, said_as, s)
