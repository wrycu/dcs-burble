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
    WAVE_OFF_FOUL_DECK = "wave off, foul deck"  # another aircraft is in the landing area
    WAVE_OFF_GEAR = "wave off, gear"
    BOLTER = "bolter"  # decided by the bolter detector in the live collector, not by CalloutEngine
    TRAPPED = "welcome aboard"  # likewise (arrestment detected); spoken as one of several variants
    TRAPPED_WAVED_OFF = "welcome aboard, despite the wave off"  # trapped after ignoring our wave-off
    # TRAPPED with the wire, when DCS reports it in time (its LSO grade, or the wire animation).
    TRAPPED_WIRE_1 = "welcome aboard, one wire"
    TRAPPED_WIRE_2 = "welcome aboard, two wire"
    TRAPPED_WIRE_3 = "welcome aboard, three wire"
    TRAPPED_WIRE_4 = "welcome aboard, four wire"
    TRAPPED_WAVED_OFF_WIRE_1 = "one wire, despite the wave off"
    TRAPPED_WAVED_OFF_WIRE_2 = "two wire, despite the wave off"
    TRAPPED_WAVED_OFF_WIRE_3 = "three wire, despite the wave off"
    TRAPPED_WAVED_OFF_WIRE_4 = "four wire, despite the wave off"
    POWER_X3 = "power, power, power"
    POWER_X2 = "power, power"
    POWER = "power"
    LOW = "you're low"
    HIGH = "you're high"
    EASY_WITH_IT = "easy with it"
    RIGHT_FOR_LINEUP = "right for lineup"
    COME_LEFT = "come left"
    DONT_SETTLE = "don't settle"  # going low, in close
    DONT_CLIMB = "don't climb"  # going high, in close
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
    KEEP_TURN_IN = "keep your turn in"  # overshooting the turn to final (before the groove)
    KEEP_IT_COMING = "keep it coming"  # reassurance: on glideslope and centerline, nothing to say


# Lower number = more urgent (declaration order above).
PRIORITY = {call: i for i, call in enumerate(Call)}
POWER_CALLS = frozenset({Call.POWER, Call.POWER_X2, Call.POWER_X3})
WAVE_OFFS = frozenset({Call.WAVE_OFF, Call.WAVE_OFF_FOUL_DECK, Call.WAVE_OFF_GEAR})
# A trap welcome naming the wire: {plain welcome: {wire: call}}.
WELCOME_WIRE = {
    Call.TRAPPED: {n: Call[f"TRAPPED_WIRE_{n}"] for n in (1, 2, 3, 4)},
    Call.TRAPPED_WAVED_OFF: {n: Call[f"TRAPPED_WAVED_OFF_WIRE_{n}"] for n in (1, 2, 3, 4)},
}
LINEUP_CALLS = frozenset({Call.RIGHT_FOR_LINEUP, Call.COME_LEFT, Call.LITTLE_RIGHT, Call.LITTLE_LEFT,
                          Call.DRIFTING_LEFT, Call.DRIFTING_RIGHT})


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
    # Another aircraft in the landing area with the pilot this close: "wave off, foul deck".
    foul_deck_inside_m: float = 0.35 * NM
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
    # Reassurance ("keep it coming"): a pass this close to the glideslope and centerline, steady, with no
    # call for `keep_coming_after_s`, gets one; at most `keep_coming_max` per pass, between these distances.
    keep_coming_share: float = 0.6  # of the "a little" bands
    keep_coming_hold_s: float = 1.5
    keep_coming_after_s: float = 4.0
    keep_coming_max: int = 2
    keep_coming_from_m: float = 0.6 * NM
    keep_coming_to_m: float = 150.0
    # Before the groove: past the extended centerline by this much and still heading away from it.
    overshoot_lateral_m: float = 15.0
    overshoot_heading_deg: float = 10.0
    overshoot_from_m: float = 1.5 * NM
    overshoot_to_m: float = 0.3 * NM
    # A condition must hold this long before it is called (DCS: lineup 3 s, speed 4 s)...
    hold_s: float = 0.4
    lineup_hold_s: float = 3.0
    speed_hold_s: float = 4.0
    # ...the same call is not repeated until this long after it finished being said (and never
    # while being corrected)...
    repeat_s: float = 2.5
    # ...and a new call waits at least this long after the previous one finished being said.
    spacing_s: float = 0.5
    # Assumed length of a spoken call when the real clip length isn't known (e.g. replays).
    default_call_s: float = 0.8

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
    if s.foul_deck and 0 < s.along <= th.foul_deck_inside_m:
        active.add(Call.WAVE_OFF_FOUL_DECK)
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
        active.add(Call.DONT_SETTLE if s.along <= th.in_close_m else Call.GOING_LOW)
    elif rate >= th.going_deg_s:
        active.add(Call.DONT_CLIMB if s.along <= th.in_close_m else Call.GOING_HIGH)

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
    if call in POWER_CALLS or call in (Call.LOW, Call.LITTLE_LOW, Call.GOING_LOW, Call.DONT_SETTLE):
        return s.glideslope_rate >= c
    if call in (Call.HIGH, Call.LITTLE_HIGH, Call.GOING_HIGH, Call.DONT_CLIMB):
        return s.glideslope_rate <= -c
    if call in (Call.RIGHT_FOR_LINEUP, Call.LITTLE_RIGHT, Call.DRIFTING_LEFT):
        return s.lineup_rate >= c
    if call in (Call.COME_LEFT, Call.LITTLE_LEFT, Call.DRIFTING_RIGHT):
        return s.lineup_rate <= -c
    return False


@dataclass
class CalloutEngine:
    thresholds: Thresholds = field(default_factory=Thresholds)
    # How long each call takes to say (seconds), so gaps are measured from the end of a phrase.
    durations: dict[Call, float] = field(default_factory=dict)
    # Condition flips seen (a measure of chatter, before persistence smooths it out).
    toggles: int = 0

    def __post_init__(self) -> None:
        self._since: dict[Call, float] = {}
        self._said_until: dict[Call, float] = {}  # when each call last finished being said
        self._last_power: float | None = None
        self._busy_until = float("-inf")  # when the last call (of any kind) finishes
        self._previous: frozenset[Call] = frozenset()
        self._aligned_since: float | None = None
        self.in_groove = False
        self.waved_off = False
        self._steady_since: float | None = None  # for "keep it coming"
        self._keep_coming_said = 0
        self._overshoot_since: float | None = None
        self._overshoot_said = False

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
        if self.waved_off:
            return None
        if not self.in_groove:
            return self._pattern(s)
        ready = []
        for c in active:
            if s.time - self._since[c] < th.hold_for(c):
                continue
            said_until = self._said_until.get(c)
            if said_until is not None and (s.time - said_until < th.repeat_s or correcting(c, s, th)):
                continue
            ready.append(c)
        if not ready:
            return self._keep_coming(s, active)
        call = min(ready, key=PRIORITY.__getitem__)
        # A wave-off interrupts anything; other calls wait for the previous one to finish.
        if call not in WAVE_OFFS and s.time - self._busy_until < th.spacing_s:
            return None
        said_as = call
        if call is Call.POWER and self._last_power is not None and s.time - self._last_power <= th.power_escalate_s:
            said_as = Call.POWER_X2  # DCS: a second power call, "with more annoyed inflection"
        ends = s.time + self.durations.get(said_as, th.default_call_s)
        self._said_until[call] = ends
        self._busy_until = ends
        if said_as in POWER_CALLS:
            self._last_power = s.time
        if said_as in WAVE_OFFS:
            self.waved_off = True
        self._steady_since = None
        return CallEvent(s.time, s.along, said_as, s)

    def _say(self, s: GrooveState, call: Call) -> CallEvent:
        ends = s.time + self.durations.get(call, self.thresholds.default_call_s)
        self._said_until[call] = ends
        self._busy_until = ends
        return CallEvent(s.time, s.along, call, s)

    def _pattern(self, s: GrooveState) -> CallEvent | None:
        """Before the groove: "keep your turn in" when overshooting the extended centerline (passing it
        to starboard while still heading away from it), once per pass."""
        th = self.thresholds
        overshooting = (th.overshoot_to_m < s.along <= th.overshoot_from_m and s.lateral_m >= th.overshoot_lateral_m
                        and s.heading_error >= th.overshoot_heading_deg)
        if not overshooting or self._overshoot_said:
            self._overshoot_since = None
            return None
        if self._overshoot_since is None:
            self._overshoot_since = s.time
        if s.time - self._overshoot_since < th.hold_s or s.time - self._busy_until < th.spacing_s:
            return None
        self._overshoot_said = True
        return self._say(s, Call.KEEP_TURN_IN)

    def _keep_coming(self, s: GrooveState, active: set[Call]) -> CallEvent | None:
        """Reassurance when the pass is good and the LSO has been quiet for a while."""
        th = self.thresholds
        k = th.keep_coming_share
        steady = (not active and th.keep_coming_to_m < s.along <= th.keep_coming_from_m
                  and abs(s.glideslope_deg) < th.little_deg * k and abs(s.glideslope_rate) < th.going_deg_s * k
                  and abs(s.lineup_deg) < th.little_lineup_deg * k and abs(s.lineup_rate) < th.drifting_deg_s * k
                  and (s.aoa is None or th.aoa_fast < s.aoa < th.aoa_slow))
        if not steady:
            self._steady_since = None
            return None
        if self._steady_since is None:
            self._steady_since = s.time
        if (self._keep_coming_said >= th.keep_coming_max or s.time - self._steady_since < th.keep_coming_hold_s
                or s.time - self._busy_until < th.keep_coming_after_s):
            return None
        self._keep_coming_said += 1
        self._steady_since = None
        return self._say(s, Call.KEEP_IT_COMING)
