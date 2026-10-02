"""Analyse `hooks/dcs-lso-export-probe.lua` output: how often does the server really get positions?

The probe logs every aircraft near a carrier every server frame. Between network updates
from a client, DCS extrapolates (or holds) its position, so the real update rate shows up as:

- frames where the position didn't change at all (if DCS holds between updates), or
- spikes in acceleration where an update corrects the extrapolated path (if it extrapolates).

Both are measured, plus the dominant period of the acceleration series (autocorrelation),
which picks up regular updates even when DCS blends corrections over a few frames.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

# A frame's acceleration counts as a correction spike when above SPIKE_FACTOR times the local
# quiet level plus SPIKE_FLOOR (m/s^2): flown manoeuvres change smoothly, corrections jump. The quiet
# level is a low percentile, since at high update rates most frames carry a correction.
SPIKE_FACTOR = 4.0
SPIKE_FLOOR = 2.0
LOCAL_WINDOW = 15  # frames either side for the local quiet level
QUIET_PERCENTILE = 0.2
MIN_PERIOD_ACC = 0.1  # m/s^2: below this the acceleration series is only rounding noise


@dataclass(frozen=True, slots=True)
class Row:
    t: float
    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class RateReport:
    session: int
    aircraft_id: int
    unit: str
    frames: int
    seconds: float
    frame_hz: float
    unchanged_pct: float  # frames whose position equals the previous frame's
    change_hz: float  # frames with a changed position, per second
    spike_hz: float  # acceleration spikes (merged runs) per second
    spike_interval_s: float | None  # median time between spikes
    period_s: float | None  # dominant period of the acceleration series

    def describe(self) -> str:
        def opt(v: float | None, fmt: str) -> str:
            return fmt.format(v) if v is not None else "-"
        return (f"session {self.session} {self.unit or self.aircraft_id} ({self.aircraft_id}): "
                f"{self.frames} frames over {self.seconds:.0f}s at {self.frame_hz:.0f} fps | "
                f"unchanged {self.unchanged_pct:.0f}% -> changes {self.change_hz:.1f}/s | "
                f"correction spikes {self.spike_hz:.1f}/s (every {opt(self.spike_interval_s, '{:.3f}')}s) | "
                f"acceleration period {opt(self.period_s, '{:.3f}')}s "
                f"(~{opt(1 / self.period_s if self.period_s else None, '{:.1f}')} Hz)")


def load(path: str | Path) -> dict[tuple[int, int], tuple[str, list[Row]]]:
    """Tracks keyed by (session, DCS object id). A header line starts each mission (session)."""
    tracks: dict[tuple[int, int], tuple[str, list[Row]]] = {}
    session = 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("t,"):
            session += 1
            continue
        f = line.split(",")
        if len(f) < 6:
            continue
        try:
            key = (session, int(f[1]))
            row = Row(float(f[0]), float(f[3]), float(f[4]), float(f[5]))
        except ValueError:
            continue
        tracks.setdefault(key, (f[2], []))[1].append(row)
    return tracks


def analyse(session: int, aircraft_id: int, unit: str, rows: list[Row]) -> RateReport | None:
    rows = _dedupe(rows)
    if len(rows) < 3 * LOCAL_WINDOW:
        return None
    seconds = rows[-1].t - rows[0].t
    dts = [b.t - a.t for a, b in zip(rows, rows[1:])]
    unchanged = sum(1 for a, b in zip(rows, rows[1:]) if (a.x, a.y, a.z) == (b.x, b.y, b.z))
    vel = [((b.x - a.x) / dt, (b.y - a.y) / dt, (b.z - a.z) / dt) for a, b, dt in zip(rows, rows[1:], dts)]
    acc = [math.dist(v0, v1) / dt for v0, v1, dt in zip(vel, vel[1:], dts[1:])]
    times = [r.t for r in rows[2:]]
    spikes = _spikes(acc, times)
    intervals = [b - a for a, b in zip(spikes, spikes[1:])]
    return RateReport(
        session=session, aircraft_id=aircraft_id, unit=unit, frames=len(rows), seconds=seconds,
        frame_hz=1 / statistics.median(dts), unchanged_pct=100 * unchanged / len(dts),
        change_hz=(len(dts) - unchanged) / seconds if seconds else 0.0,
        spike_hz=len(spikes) / seconds if seconds else 0.0,
        spike_interval_s=statistics.median(intervals) if intervals else None,
        period_s=_period(acc, statistics.median(dts)),
    )


def analyse_file(path: str | Path) -> list[RateReport]:
    reports = [analyse(s, i, unit, rows) for (s, i), (unit, rows) in load(path).items()]
    return [r for r in reports if r is not None]


def _dedupe(rows: list[Row]) -> list[Row]:
    out: list[Row] = []
    for r in rows:
        if not out or r.t > out[-1].t:
            out.append(r)
    return out


def _spikes(acc: list[float], times: list[float]) -> list[float]:
    """Times of acceleration spikes; a run of consecutive spiking frames counts once."""
    found: list[float] = []
    previous = False
    for i, a in enumerate(acc):
        lo, hi = max(0, i - LOCAL_WINDOW), min(len(acc), i + LOCAL_WINDOW + 1)
        window = sorted(acc[lo:hi])
        local = window[int(QUIET_PERCENTILE * (len(window) - 1))]
        spike = a > SPIKE_FACTOR * local + SPIKE_FLOOR
        if spike and not previous:
            found.append(times[i])
        previous = spike
    return found


def _period(acc: list[float], frame_s: float, max_s: float = 1.5) -> float | None:
    """Lag of the first clear autocorrelation peak of the acceleration series, in seconds.
    Values are capped at the 95th percentile so a few outliers (a trap, a respawn) don't dominate."""
    cap = sorted(acc)[int(0.95 * (len(acc) - 1))]
    if cap < MIN_PERIOD_ACC:
        return None  # nothing but rounding noise (a straight, steady track)
    capped = [min(a, cap) for a in acc]
    mean = statistics.fmean(capped)
    x = [a - mean for a in capped]
    var = sum(v * v for v in x)
    if var == 0:
        return None
    max_lag = min(len(x) // 3, int(max_s / frame_s))
    r = [sum(x[i] * x[i + lag] for i in range(len(x) - lag)) / var for lag in range(max_lag + 1)]
    for lag in range(2, max_lag):
        if r[lag] > 0.2 and r[lag] >= r[lag - 1] and r[lag] >= r[lag + 1]:
            return lag * frame_s
    return None
