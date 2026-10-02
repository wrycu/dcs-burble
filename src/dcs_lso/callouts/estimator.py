"""Live (causal) estimate of an aircraft's state in the groove.

Uses only samples up to "now", as the live callout engine must. Each quantity is a
least-squares line fitted over a short trailing window, which gives both a smoothed
current value (the line evaluated at now, so less lag than a plain average) and its
trend.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

DEFAULT_WINDOW_S = 0.8
# Derived AOA (pitch minus flight path) is much noisier than position; average it longer.
DEFAULT_AOA_WINDOW_S = 2.0


@dataclass(frozen=True, slots=True)
class LiveInput:
    time: float
    along: float  # meters short of the aim point (hook)
    lateral: float  # meters right of the landing-area centerline (hook)
    hook_height: float  # meters above the deck
    pitch: float  # degrees
    alt: float  # aircraft altitude, meters (world)
    u: float  # world east, meters
    v: float  # world north, meters
    aoa: float | None  # recorded AOA in degrees, if the exporter provides it
    heading_error: float = 0.0  # aircraft heading minus landing-area heading, degrees
    roll: float = 0.0  # degrees
    gear: float | None = None  # landing gear position 0 (up) .. 1 (down), when the exporter provides it


@dataclass(frozen=True, slots=True)
class GrooveState:
    time: float
    along: float
    # Angular deviation from the optimal glideslope as seen from the aim point; + is high.
    glideslope_deg: float
    glideslope_rate: float  # deg/s; + is going high
    # Angular lineup error as seen from the aim point; + is right of centerline.
    lineup_deg: float
    lineup_rate: float  # deg/s; + is drifting right
    lateral_m: float
    aoa: float | None
    aoa_derived: bool
    heading_error: float
    roll: float
    samples_in_window: int
    pitch_rate: float = 0.0  # deg/s
    gear: float | None = None


def _fit(points: list[tuple[float, float]], at: float) -> tuple[float, float]:
    """Least-squares line through (t, y); returns (value at `at`, slope)."""
    n = len(points)
    if n == 1:
        return points[0][1], 0.0
    mt = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    if var == 0:
        return my, 0.0
    slope = sum((t - mt) * (y - my) for t, y in points) / var
    return my + slope * (at - mt), slope


def derived_aoa(window: list[LiveInput], at: float) -> float | None:
    """AOA approximated as pitch minus flight path angle over `window` (world frame, so
    ship motion doesn't matter; wind and sideslip are ignored)."""
    if len(window) < 3:
        return None
    _, climb = _fit([(s.time, s.alt) for s in window], at)
    dist = math.hypot(window[-1].u - window[0].u, window[-1].v - window[0].v)
    dt = window[-1].time - window[0].time
    if dt <= 0 or dist <= 0:
        return None
    fpa = math.degrees(math.atan2(climb, dist / dt))
    pitch, _ = _fit([(s.time, s.pitch) for s in window], at)
    return pitch - fpa


def angle_deg(value: float, along: float, min_along_m: float = 30.0) -> float:
    """Angle subtended at the aim point by an offset `value` at `along` meters out."""
    return math.degrees(math.atan2(value, max(along, min_along_m)))


class LiveEstimator:
    def __init__(self, glideslope_deg: float, window_s: float = DEFAULT_WINDOW_S,
                 aoa_window_s: float = DEFAULT_AOA_WINDOW_S, min_along_m: float = 30.0) -> None:
        self.glideslope_deg = glideslope_deg
        self.window_s = window_s
        self.aoa_window_s = aoa_window_s
        # Angles are meaningless right at the aim point; clamp the range used for them.
        self.min_along_m = min_along_m
        self._window: deque[LiveInput] = deque()

    def update(self, x: LiveInput) -> GrooveState:
        self._window.append(x)
        keep = max(self.window_s, self.aoa_window_s)
        while self._window and x.time - self._window[0].time > keep:
            self._window.popleft()
        w = [s for s in self._window if x.time - s.time <= self.window_s]
        aoa_w = [s for s in self._window if x.time - s.time <= self.aoa_window_s]

        m = self.min_along_m
        gs, gs_rate = _fit([(s.time, angle_deg(s.hook_height, s.along, m) - self.glideslope_deg) for s in w], x.time)
        lu, lu_rate = _fit([(s.time, angle_deg(s.lateral, s.along, m)) for s in w], x.time)
        lateral, _ = _fit([(s.time, s.lateral) for s in w], x.time)
        _, pitch_rate = _fit([(s.time, s.pitch) for s in w], x.time)

        recorded = [s for s in w if s.aoa is not None]
        if recorded:
            aoa: float | None = sum(s.aoa for s in recorded) / len(recorded)
            derived = False
        else:
            aoa, derived = derived_aoa(aoa_w, x.time), True
        return GrooveState(
            time=x.time, along=x.along, glideslope_deg=gs, glideslope_rate=gs_rate,
            lineup_deg=lu, lineup_rate=lu_rate, lateral_m=lateral, aoa=aoa, aoa_derived=derived,
            heading_error=x.heading_error, roll=x.roll, samples_in_window=len(w),
            pitch_rate=pitch_rate, gear=x.gear,
        )
