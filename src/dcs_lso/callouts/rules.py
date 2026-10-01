"""Decide LSO calls from live groove estimates.

Thresholds are first guesses to be tuned against real passes; the structure (groove
gating, zones, persistence, hysteresis, repeat suppression, priority) is what the
stability analysis exercises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .estimator import GrooveState

NM = 1852.0


class Call(StrEnum):
    WAVE_OFF = "wave off"
    POWER = "power"
    LOW = "you're low"
    HIGH = "you're high"
    RIGHT_FOR_LINEUP = "right for lineup"
    COME_LEFT = "come left"
    FAST = "you're fast"
    SLOW = "you're slow"


# Lower number = more urgent.
PRIORITY = {Call.WAVE_OFF: 0, Call.POWER: 1, Call.LOW: 2, Call.HIGH: 3,
            Call.RIGHT_FOR_LINEUP: 4, Call.COME_LEFT: 5, Call.FAST: 6, Call.SLOW: 7}


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
    glideslope_deg: float = 0.5  # beyond this: high / low
    power_deg: float = 0.5  # low by this much and not correcting: power
    power_now_deg: float = 0.9  # this low: power regardless of trend
    wave_off_low_deg: float = 1.2
    lineup_deg: float = 1.0
    wave_off_lateral_m: float = 12.0
    aoa_fast: float = 6.9  # FA-18C (lso's bands)
    aoa_slow: float = 9.3
    # A call already active stays active until the value is this far back inside its threshold.
    aoa_hysteresis: float = 0.3
    angle_hysteresis_deg: float = 0.1
    # A deviation is "being corrected" when its rate toward zero is at least this.
    correcting_deg_s: float = 0.05
    # A condition must hold this long before it is called...
    hold_s: float = 0.5
    # ...the same call is not repeated within this time (and never while being corrected)...
    repeat_s: float = 5.0
    # ...and no two calls are made closer together than this (a call takes time to say).
    spacing_s: float = 1.2


@dataclass(frozen=True, slots=True)
class CallEvent:
    time: float
    along: float
    call: Call
    state: GrooveState


def conditions(s: GrooveState, th: Thresholds, previous: frozenset[Call] = frozenset()) -> set[Call]:
    """Calls whose condition is true right now (before persistence/suppression).

    `previous` gives hysteresis: a call that was active stays active until its value is
    comfortably back inside the threshold.
    """
    def relax(call: Call, margin: float) -> float:
        return margin if call in previous else 0.0

    active: set[Call] = set()
    gs = s.glideslope_deg
    if th.quiet_inside_m < s.along <= th.wave_off_inside_m and (
            gs <= -th.wave_off_low_deg or abs(s.lateral_m) >= th.wave_off_lateral_m):
        active.add(Call.WAVE_OFF)
    if s.along <= th.quiet_inside_m:
        return active
    h = th.angle_hysteresis_deg
    if gs <= -th.power_now_deg or (gs <= -th.power_deg + relax(Call.POWER, h) and s.glideslope_rate <= 0):
        active.add(Call.POWER)
    elif gs <= -th.glideslope_deg + relax(Call.LOW, h):
        active.add(Call.LOW)
    elif gs >= th.glideslope_deg - relax(Call.HIGH, h):
        active.add(Call.HIGH)
    if s.lineup_deg <= -th.lineup_deg + relax(Call.RIGHT_FOR_LINEUP, h):
        active.add(Call.RIGHT_FOR_LINEUP)
    elif s.lineup_deg >= th.lineup_deg - relax(Call.COME_LEFT, h):
        active.add(Call.COME_LEFT)
    if s.aoa is not None:
        a = th.aoa_hysteresis
        if s.aoa <= th.aoa_fast + relax(Call.FAST, a):
            active.add(Call.FAST)
        elif s.aoa >= th.aoa_slow - relax(Call.SLOW, a):
            active.add(Call.SLOW)
    return active


def correcting(call: Call, s: GrooveState, th: Thresholds) -> bool:
    """Is the pilot already fixing the deviation this call is about?"""
    c = th.correcting_deg_s
    if call in (Call.LOW, Call.POWER):
        return s.glideslope_rate >= c
    if call is Call.HIGH:
        return s.glideslope_rate <= -c
    if call is Call.RIGHT_FOR_LINEUP:
        return s.lineup_rate >= c
    if call is Call.COME_LEFT:
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
        active = conditions(s, th, self._previous) if self.in_groove and s.along > 0 else set()
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
            if s.time - self._since[c] < th.hold_s:
                continue
            said = self._last_said.get(c)
            if said is not None and (s.time - said < th.repeat_s or correcting(c, s, th)):
                continue
            ready.append(c)
        if not ready:
            return None
        call = min(ready, key=PRIORITY.__getitem__)
        # A wave-off interrupts anything; other calls wait for the previous one to finish.
        if call is not Call.WAVE_OFF and s.time - self._last_any < th.spacing_s:
            return None
        self._last_said[call] = s.time
        self._last_any = s.time
        if call is Call.WAVE_OFF:
            self._waved_off = True
        return CallEvent(s.time, s.along, call, s)
