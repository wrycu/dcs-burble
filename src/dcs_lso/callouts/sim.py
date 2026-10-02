"""Replay recorded passes through the live estimator and callout rules.

For each pass this reports the calls the live engine would have made, how often the
raw conditions flickered, and how far the live (trailing-window) estimates were from a
hindsight estimate (centered window, all samples) — i.e. the cost of having to decide
in real time. Passes can be thinned to a lower sample rate to mimic remote aircraft.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..acmi import Recording, Sample
from ..detect import CarrierTimeline, PassResult
from ..geometry import AIRCRAFT, CARRIERS, DeckFrame
from .estimator import DEFAULT_WINDOW_S, GrooveState, LiveEstimator, LiveInput, _fit, angle_deg, derived_aoa
from .rules import CallEvent, CalloutEngine, Thresholds

NM = 1852.0


@dataclass
class PassReplay:
    result: PassResult
    rate_hz: float
    calls: list[CallEvent] = field(default_factory=list)
    toggles: int = 0
    # RMS / max of (live - hindsight) inside the groove, per quantity.
    errors: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Derived AOA vs recorded AOA (only when the recording has AOA and we derive anyway).
    derived_aoa_error: tuple[float, float] | None = None


def thin(samples: list[Sample], hz: float | None) -> list[Sample]:
    if not hz:
        return samples
    out: list[Sample] = []
    for s in samples:
        if not out or s.time - out[-1].time >= 1.0 / hz - 1e-6:
            out.append(s)
    return out


def _inputs(recording: Recording, p: PassResult, hz: float | None, strip_aoa: bool) -> list[LiveInput]:
    carrier = recording.objects[p.carrier_id]
    plane = recording.objects[p.aircraft_id]
    timeline = CarrierTimeline(carrier.samples)
    frame = DeckFrame(CARRIERS[carrier.name], AIRCRAFT[plane.name])
    samples = thin([s for s in plane.samples if p.start_time <= s.time <= p.end_time], hz)
    out = []
    for s in samples:
        pose = timeline.at(s.time)
        pos = frame.position(pose, s.transform)
        t = s.transform
        fb_heading = pose.heading - frame.carrier.deck_angle
        heading_error = ((t.heading or 0.0) - fb_heading + 180.0) % 360.0 - 180.0
        out.append(LiveInput(time=s.time, along=pos.along, lateral=pos.lateral, hook_height=pos.hook_height,
                             pitch=t.pitch or 0.0, alt=t.alt or 0.0, u=t.u or 0.0, v=t.v or 0.0,
                             aoa=None if strip_aoa else s.aoa, heading_error=heading_error,
                             roll=t.roll or 0.0, heading=t.heading))
    return out


def _hindsight(inputs: list[LiveInput], glideslope: float, window_s: float) -> list[GrooveState]:
    """Centered-window estimate at each sample (uses the future; not possible live)."""
    states = []
    half = window_s / 2
    lo = 0
    for i, x in enumerate(inputs):
        while inputs[lo].time < x.time - half:
            lo += 1
        w = [s for s in inputs[lo:] if s.time <= x.time + half]
        gs = _fit([(s.time, angle_deg(s.hook_height, s.along) - glideslope) for s in w], x.time)[0]
        lu = _fit([(s.time, angle_deg(s.lateral, s.along)) for s in w], x.time)[0]
        recorded = [s.aoa for s in w if s.aoa is not None]
        if recorded:
            aoa = sum(recorded) / len(recorded)
        elif 0 < i < len(inputs) - 1:
            aoa = derived_aoa(inputs[i - 1], x, inputs[i + 1])
        else:
            aoa = None
        states.append(GrooveState(x.time, x.along, gs, 0.0, lu, 0.0, x.lateral, aoa, not recorded,
                                  x.heading_error, x.roll, len(w), gear=x.gear))
    return states


def _rms_max(errors: list[float]) -> tuple[float, float]:
    if not errors:
        return (float("nan"), float("nan"))
    return math.sqrt(sum(e * e for e in errors) / len(errors)), max(abs(e) for e in errors)


def replay(recording: Recording, p: PassResult, hz: float | None = None, strip_aoa: bool = False,
           thresholds: Thresholds | None = None, window_s: float = DEFAULT_WINDOW_S) -> PassReplay:
    glideslope = AIRCRAFT[p.aircraft_type].glideslope
    inputs = _inputs(recording, p, hz, strip_aoa)
    estimator = LiveEstimator(glideslope, window_s=window_s)
    engine = CalloutEngine(thresholds or Thresholds())
    live: list[GrooveState] = []
    calls = []
    for x in inputs:
        state = estimator.update(x)
        live.append(state)
        if (event := engine.update(state)) is not None:
            calls.append(event)
    dts = [b.time - a.time for a, b in zip(inputs, inputs[1:])]
    rate = 1 / (sorted(dts)[len(dts) // 2]) if dts else 0.0
    out = PassReplay(result=p, rate_hz=rate, calls=calls, toggles=engine.toggles)

    # Compare with hindsight at full resolution (and with recorded AOA when available).
    truth_inputs = _inputs(recording, p, None, False)
    truth = {round(s.time, 3): s for s in _hindsight(truth_inputs, glideslope, window_s)}
    errs: dict[str, list[float]] = {"glideslope_deg": [], "lineup_deg": [], "aoa": []}
    for s in live:
        h = truth.get(round(s.time, 3))
        if h is None or not 60.0 < s.along <= 0.75 * NM:
            continue
        errs["glideslope_deg"].append(s.glideslope_deg - h.glideslope_deg)
        errs["lineup_deg"].append(s.lineup_deg - h.lineup_deg)
        if s.aoa is not None and h.aoa is not None:
            errs["aoa"].append(s.aoa - h.aoa)
    out.errors = {k: _rms_max(v) for k, v in errs.items()}
    if strip_aoa and truth_inputs and truth_inputs[0].aoa is not None:
        out.derived_aoa_error = out.errors["aoa"]
    return out
