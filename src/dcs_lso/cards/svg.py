"""Trap cards as standalone SVG.

Side view (hook height vs distance, with the glideslope and its tolerance bands) and
top view (lineup vs distance), the track coloured by AOA, groove positions, deck edge
and wires, plus the grade, remarks and per-position numbers. Each SVG carries its own
styles (light and dark), so it renders the same inline, in an <img>, or on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape

from ..callouts.rules import LINEUP_CALLS
from ..detect import PassResult, PassSample
from ..geometry import AIRCRAFT, CARRIERS, DeckFrame
from ..grading import GradeResult, grade_name, grade_short
from ..grading.grade import GLIDESLOPE_DEG, LINEUP_DEG, POSITIONS

NM = 1852.0
FT = 0.3048
X_MAX_M = 0.8 * NM
X_MIN_M = -40.0

WIDTH = 920
PAD_L, PAD_R = 64, 24
PLOT_W = WIDTH - PAD_L - PAD_R
HEADER_H = 104
KT = 1852.0 / 3600.0  # m/s
SIDE_TOP, SIDE_H = HEADER_H + 18, 250
TOP_TOP, TOP_H = SIDE_TOP + SIDE_H + 44, 170
TABLE_TOP = TOP_TOP + TOP_H + 40
HEIGHT = TABLE_TOP + 132
CALLS_LINE_H = 18  # each line of the live-calls list below the table
CHAR_W = 0.56  # rough glyph width as a fraction of the font size, for laying out text

# AOA bands relative to the aircraft's on-speed band (lso's FA-18C bands are on-speed
# 7.4-8.8 with 0.5 deg "slightly" margins).
AOA_CLASSES = ("fast", "sfast", "onspeed", "sslow", "slow")
AOA_LABELS = {"fast": "Fast", "sfast": "Slightly fast", "onspeed": "On speed", "sslow": "Slightly slow",
              "slow": "Slow", "noaoa": "No AOA"}

_LIGHT = """
.tc { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tc-bg { fill: #ffffff; }
.tc-text { fill: #1f2328; }
.tc-muted { fill: #656d76; }
.tc-axis { stroke: #8c959f; stroke-width: 1; }
.tc-grid { stroke: #d0d7de; stroke-width: 1; stroke-dasharray: 2 4; }
.tc-pos { stroke: #8c959f; stroke-width: 1; stroke-dasharray: 5 4; }
.tc-ideal { stroke: #1f2328; stroke-width: 1.2; stroke-dasharray: 8 4; fill: none; }
.tc-band1 { fill: #2da44e; fill-opacity: 0.16; }
.tc-band2 { fill: #d4a72c; fill-opacity: 0.16; }
.tc-band3 { fill: #cf222e; fill-opacity: 0.12; }
.tc-deck { fill: #8c959f; fill-opacity: 0.35; }
.tc-wire { stroke: #656d76; stroke-width: 2; }
.tc-wire-caught { stroke: #0969da; stroke-width: 3.5; }
.tc-track { fill: none; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.tc-fast { stroke: #cf222e; }
.tc-sfast { stroke: #e16f24; }
.tc-onspeed { stroke: #1a7f37; }
.tc-sslow { stroke: #0969da; }
.tc-slow { stroke: #8250df; }
.tc-noaoa { stroke: #656d76; }
.tc-grade-bad { fill: #cf222e; }
.tc-grade-mid { fill: #9a6700; }
.tc-grade-good { fill: #1a7f37; }
.tc-rule { stroke: #d0d7de; stroke-width: 1; }
.tc-call { fill: #ffffff; stroke: #1f2328; stroke-width: 2; }
.tc-call-label { fill: #1f2328; font-weight: 600; }
"""
_DARK = """
  .tc-bg { fill: #0d1117; }
  .tc-text { fill: #e6edf3; }
  .tc-muted { fill: #8d96a0; }
  .tc-axis { stroke: #6e7681; }
  .tc-grid { stroke: #30363d; }
  .tc-pos { stroke: #6e7681; }
  .tc-ideal { stroke: #e6edf3; }
  .tc-band1 { fill: #3fb950; fill-opacity: 0.14; }
  .tc-band2 { fill: #d29922; fill-opacity: 0.14; }
  .tc-band3 { fill: #f85149; fill-opacity: 0.10; }
  .tc-deck { fill: #6e7681; }
  .tc-wire { stroke: #8d96a0; }
  .tc-wire-caught { stroke: #58a6ff; }
  .tc-fast { stroke: #f85149; }
  .tc-sfast { stroke: #f0883e; }
  .tc-onspeed { stroke: #3fb950; }
  .tc-sslow { stroke: #58a6ff; }
  .tc-slow { stroke: #bc8cff; }
  .tc-noaoa { stroke: #8d96a0; }
  .tc-grade-bad { fill: #f85149; }
  .tc-grade-mid { fill: #d29922; }
  .tc-grade-good { fill: #3fb950; }
  .tc-rule { stroke: #30363d; }
  .tc-call { fill: #0d1117; stroke: #e6edf3; }
  .tc-call-label { fill: #e6edf3; }
"""
# The card follows the viewer's light or dark mode; DARK_STYLE is always dark (e.g. images for Discord, where
# there is no viewer to ask).
STYLE = _LIGHT + "@media (prefers-color-scheme: dark) {\n" + _DARK + "}\n"
DARK_STYLE = _LIGHT + _DARK



# Live calls about lineup go on the lineup plot; the rest on the glideslope plot.
LINEUP_VALUES = frozenset(c.value for c in LINEUP_CALLS)


@dataclass(frozen=True, slots=True)
class Axis:
    lo: float
    hi: float
    px_lo: float  # pixel for `lo`
    px_hi: float  # pixel for `hi`

    def __call__(self, v: float) -> float:
        return self.px_lo + (v - self.lo) / (self.hi - self.lo) * (self.px_hi - self.px_lo)


def aoa_class(aoa: float | None, on_speed: tuple[float, float], margin: float = 0.5) -> str:
    if aoa is None:
        return "noaoa"
    low, high = on_speed
    if aoa < low - margin:
        return "fast"
    if aoa < low:
        return "sfast"
    if aoa <= high:
        return "onspeed"
    if aoa <= high + margin:
        return "sslow"
    return "slow"


def _runs(samples: list[PassSample], on_speed: tuple[float, float]) -> list[tuple[str, list[PassSample]]]:
    """Split the track into runs of one AOA class (adjacent runs share an endpoint)."""
    runs: list[tuple[str, list[PassSample]]] = []
    for s in samples:
        cls = aoa_class(s.aoa, on_speed)
        if runs and runs[-1][0] == cls:
            runs[-1][1].append(s)
        else:
            start = [runs[-1][1][-1]] if runs else []
            runs.append((cls, start + [s]))
    return runs


def _f(v: float) -> str:
    return f"{v:.1f}"


def _poly(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{_f(x)},{_f(y)}" for x, y in points)


def _grade_class(grade: str) -> str:
    if grade in ("C", "WO"):
        return "tc-grade-bad"
    if grade in ("---", "B"):
        return "tc-grade-mid"
    return "tc-grade-good"


def _distance_axis(top: float, height: float, x: Axis, out: list[str]) -> None:
    for nm in (0.75, 0.5, 0.25, 0.0):
        px = x(nm * NM)
        out.append(f'<line class="tc-grid" x1="{_f(px)}" y1="{_f(top)}" x2="{_f(px)}" y2="{_f(top + height)}"/>')
        out.append(f'<text class="tc-muted" x="{_f(px)}" y="{_f(top + height + 14)}" font-size="11" '
                   f'text-anchor="middle">{nm:g} nm</text>')
    for pos, (outer, inner) in POSITIONS.items():
        px = x(outer)
        out.append(f'<line class="tc-pos" x1="{_f(px)}" y1="{_f(top)}" x2="{_f(px)}" y2="{_f(top + height)}"/>')
        mid = x((outer + inner) / 2)
        out.append(f'<text class="tc-muted" x="{_f(mid)}" y="{_f(top + 13)}" font-size="12" font-weight="600" '
                   f'text-anchor="middle">{pos.value}</text>')


def _step(span: float, steps: tuple[float, ...], max_ticks: int) -> float:
    """The smallest step that keeps the number of ticks across `span` within `max_ticks`."""
    return next((st for st in steps if span / st <= max_ticks), steps[-1])


def _distance_axis_view(top: float, height: float, x: Axis, near: float, far: float, out: list[str]) -> None:
    """Distance grid and labels for any stretch of the approach, plus the groove positions in view."""
    # Round nautical miles for long stretches; round feet when zoomed in close.
    if (far - near) / NM > 0.3:
        unit, scale, steps = "nm", NM, (0.05, 0.1, 0.25, 0.5)
    else:
        unit, scale, steps = "ft", FT, (10, 25, 50, 100, 200, 250, 500)
    step = _step((far - near) / scale, steps, 8)
    tick = math.ceil(near / scale / step) * step
    while tick <= far / scale + 1e-9:
        px = x(tick * scale)
        label = f"{tick:g} nm" if unit == "nm" else f"{tick:,.0f} ft"
        out.append(f'<line class="tc-grid" x1="{_f(px)}" y1="{_f(top)}" x2="{_f(px)}" y2="{_f(top + height)}"/>')
        out.append(f'<text class="tc-muted" x="{_f(px)}" y="{_f(top + height + 14)}" font-size="11" '
                   f'text-anchor="middle">{label}</text>')
        tick = round(tick + step, 6)
    for pos, (outer, inner) in POSITIONS.items():
        if near <= outer <= far:
            px = x(outer)
            out.append(f'<line class="tc-pos" x1="{_f(px)}" y1="{_f(top)}" x2="{_f(px)}" y2="{_f(top + height)}"/>')
        lo, hi = max(inner, near), min(outer, far)
        if hi - lo > (far - near) * 0.06:  # label the position where enough of it is in view
            out.append(f'<text class="tc-muted" x="{_f(x((lo + hi) / 2))}" y="{_f(top + 13)}" font-size="12" '
                       f'font-weight="600" text-anchor="middle">{pos.value}</text>')


def _clip(clip_id: str, top: float, height: float, out: list[str]) -> None:
    out.append(f'<defs><clipPath id="{clip_id}"><rect x="{PAD_L}" y="{_f(top)}" width="{PLOT_W}" '
               f'height="{_f(height)}"/></clipPath></defs>')
    out.append(f'<g clip-path="url(#{clip_id})">')


def _text_w(text: str, size: float) -> float:
    return len(text) * size * CHAR_W


def call_label(call: dict) -> str:
    """How a recorded call reads: what was said when known ("Roger ball, 25 knots"), a pilot's in quotes."""
    text = call.get("text") or call["call"]
    if call.get("by") == "pilot":
        return f"\u201c{text}\u201d"
    return text[:1].upper() + text[1:]


def _said_order(call: dict) -> tuple[float, int]:
    """In the order said: a pilot's call before the LSO's answer to it (they're recorded at the same time)."""
    return call.get("time", 0.0), 0 if call.get("by") == "pilot" else 1


def _call_markers(calls: list[dict], samples: list[PassSample], x: Axis, y_of, top: float, height: float,
                  out: list[str]) -> None:
    """Live calls the LSO made: a marker on the track where each was given, with its label on the
    nearest free row above (or else below) the track, so labels never overlap each other. Placed latest
    first, so where labels stack they read in order from the top (a pilot's "ball" above "Roger ball")."""
    if not samples:
        return
    size, row = 11, 14
    placed: list[tuple[float, float, float, float]] = []  # label boxes: x0, y0, x1, y1
    for call in sorted(calls, key=_said_order, reverse=True):
        near = min((s for s in samples if s.along >= 0), key=lambda s: abs(s.along - call["along"]), default=samples[0])
        cx, cy = x(near.along), y_of(near)
        label = call_label(call)
        half = _text_w(label, size) / 2 + 3
        lx = min(max(cx, PAD_L + half), PAD_L + PLOT_W - half)  # keep the label inside the plot
        candidates = [cy - 14 - i * row for i in range(5)] + [cy + 24 + i * row for i in range(5)]
        fits = [ly for ly in candidates if top + size <= ly <= top + height - 2]
        free = [ly for ly in fits
                if not any(lx - half < x1 and x0 < lx + half and ly - size < y1 and y0 < ly + 2
                           for x0, y0, x1, y1 in placed)]
        ly = (free or fits or candidates)[0]
        placed.append((lx - half, ly - size, lx + half, ly + 2))
        tip = ly + 3 if ly < cy else ly - size
        out.append(f'<line class="tc-axis" x1="{_f(cx)}" y1="{_f(cy)}" x2="{_f(lx)}" y2="{_f(tip)}"/>')
        out.append(f'<circle class="tc-call" cx="{_f(cx)}" cy="{_f(cy)}" r="4"/>')
        out.append(f'<text class="tc-call-label" x="{_f(lx)}" y="{_f(ly)}" font-size="{size}" '
                   f'text-anchor="middle">{escape(label)}</text>')


def _wrap(prefix: str, items: list[str], size: float, width: float) -> list[list[str]]:
    """Split `items` (joined with ", ") into lines that fit `width`; the first line starts with `prefix`."""
    lines: list[list[str]] = [[]]
    used = _text_w(prefix + " ", size)
    for item in items:
        w = _text_w(item + ", ", size)
        if lines[-1] and used + w > width:
            lines.append([])
            used = 0.0
        lines[-1].append(item)
        used += w
    return lines


def _side_view(p: PassResult, frame: DeckFrame, samples: list[PassSample], x: Axis, wire: int | None,
               on_speed: tuple[float, float], uid: str, out: list[str], calls: list[dict],
               view: tuple[float, float] | None = None) -> None:
    top, h = SIDE_TOP, SIDE_H
    glide = frame.aircraft.glideslope
    ideal_far = X_MAX_M * math.tan(math.radians(glide))
    if view is None:
        peak = max([s.hook_height for s in samples if s.along > 0] + [0.0])
        y_lo, y_hi = -6.0, min(max(ideal_far * 1.45, peak * 1.05, 40.0), 260.0)
    else:  # zoomed in: fit the heights in view (and the ideal glideslope there)
        near, far = view
        ideal = lambda along: max(along, 0.0) * math.tan(math.radians(glide))  # noqa: E731
        heights = [s.hook_height for s in samples if near <= s.along <= far] + [ideal(near), ideal(far)]
        pad = max(1.5, (max(heights) - min(heights)) * 0.12)
        y_lo, y_hi = max(min(heights) - pad, -6.0), max(heights) + pad
    y = Axis(y_lo, y_hi, top + h, top)
    out.append(f'<text class="tc-text" x="{PAD_L}" y="{top - 6}" font-size="13" font-weight="600">'
               f'Glideslope (hook height above deck)</text>')
    _clip(f"{uid}-side", top, h, out)
    # Tolerance bands, widest first, as wedges from the aim point.
    for cls, dev in (("tc-band3", GLIDESLOPE_DEG[2]), ("tc-band2", GLIDESLOPE_DEG[1]), ("tc-band1", GLIDESLOPE_DEG[0])):
        hi_h = X_MAX_M * math.tan(math.radians(glide + dev))
        lo_h = X_MAX_M * math.tan(math.radians(max(glide - dev, 0.0)))
        out.append(f'<polygon class="{cls}" points="{_poly([(x(0), y(0)), (x(X_MAX_M), y(hi_h)), (x(X_MAX_M), y(lo_h))])}"/>')
    out.append(f'<line class="tc-ideal" x1="{_f(x(0))}" y1="{_f(y(0))}" x2="{_f(x(X_MAX_M))}" y2="{_f(y(ideal_far))}"/>')
    # Deck from the ramp forward, and the wires.
    ramp = frame.carrier.ramp_along_m
    out.append(f'<rect class="tc-deck" x="{_f(x(ramp))}" y="{_f(y(0))}" '
               f'width="{_f(x(X_MIN_M) - x(ramp))}" height="{_f(y(-6.0) - y(0))}"/>')
    for n, along in enumerate(frame.wire_along, start=1):
        cls = "tc-wire-caught" if n == wire else "tc-wire"
        out.append(f'<line class="{cls}" x1="{_f(x(along))}" y1="{_f(y(0) - 5)}" x2="{_f(x(along))}" y2="{_f(y(0) + 3)}"/>')
    for cls, run in _runs(samples, on_speed):
        out.append(f'<polyline class="tc-track tc-{cls}" points="{_poly([(x(s.along), y(s.hook_height)) for s in run])}"/>')
    _call_markers(_in_view([c for c in calls if c["call"] not in LINEUP_VALUES], view), samples, x,
                  lambda s: y(s.hook_height), top, h, out)
    out.append("</g>")
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top + h}" x2="{PAD_L + PLOT_W}" y2="{top + h}"/>')
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top}" x2="{PAD_L}" y2="{top + h}"/>')
    if view is None:
        _distance_axis(top, h, x, out)
        step_ft = 100 if y_hi / FT > 300 else 50
        for ft in range(0, int(y_hi / FT) + 1, step_ft):
            py = y(ft * FT)
            out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(py + 4)}" font-size="11" text-anchor="end">{ft} ft</text>')
    else:
        _distance_axis_view(top, h, x, view[0], view[1], out)
        step_ft = _step((y_hi - y_lo) / FT, (1, 2, 5, 10, 25, 50, 100, 200), 8)
        ft = math.ceil(y_lo / FT / step_ft) * step_ft
        while ft <= y_hi / FT:
            out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(y(ft * FT) + 4)}" font-size="11" '
                       f'text-anchor="end">{ft:g} ft</text>')
            ft += step_ft
    if wire:
        label = f"#{wire}" if p.wire is not None else f"#{wire} est."
        out.append(f'<text class="tc-muted" x="{_f(x(frame.wire_along[wire - 1]))}" y="{_f(y(0) - 9)}" font-size="11" '
                   f'text-anchor="middle">{label}</text>')


def deck_top(frame: DeckFrame, x: Axis, y: Axis, out: list[str], caught: int | None = None) -> None:
    """The landing area seen from above (lineup view): from the ramp forward, as wide as the wires'
    pendants, with the wires across it (`caught` highlighted)."""
    ends = frame.wire_ends
    port = min(min(a[1], b[1]) for a, b in ends)
    stbd = max(max(a[1], b[1]) for a, b in ends)
    ramp = frame.carrier.ramp_along_m
    corners = [(x(ramp), y(port)), (x(ramp), y(stbd)), (x(X_MIN_M), y(stbd)), (x(X_MIN_M), y(port))]
    out.append(f'<polygon class="tc-deck" points="{_poly(corners)}"/>')
    for n, ((pa, pl), (sa, sl)) in enumerate(ends, start=1):
        cls = "tc-wire-caught" if n == caught else "tc-wire"
        out.append(f'<line class="{cls}" x1="{_f(x(pa))}" y1="{_f(y(pl))}" x2="{_f(x(sa))}" y2="{_f(y(sl))}"/>')


def _top_view(samples: list[PassSample], x: Axis, on_speed: tuple[float, float], uid: str, out: list[str],
              calls: list[dict], frame: DeckFrame | None = None, wire: int | None = None,
              view: tuple[float, float] | None = None) -> None:
    top, h = TOP_TOP, TOP_H
    if view is None:
        reach = max([abs(s.lateral) for s in samples if s.along > 0] + [0.0])
        half = min(max(reach * 1.1, X_MAX_M * math.tan(math.radians(LINEUP_DEG[2])) * 1.2), 200.0)
    else:  # zoomed in: fit what's in view, and the whole deck width once the deck is in view
        near, far = view
        half = max(max([abs(s.lateral) for s in samples if near <= s.along <= far] + [0.0]) * 1.15, 3.0)
        if frame is not None and near < frame.carrier.ramp_along_m:
            half = max(half, max(abs(lat) for ends in frame.wire_ends for _, lat in ends) * 1.15)
    y = Axis(-half, half, top + h, top)  # + lateral (right of centerline) is up: the LSO's view from behind
    out.append(f'<text class="tc-text" x="{PAD_L}" y="{top - 6}" font-size="13" font-weight="600">'
               f'Lineup (right of centerline is up)</text>')
    _clip(f"{uid}-top", top, h, out)
    for cls, dev in (("tc-band3", LINEUP_DEG[2]), ("tc-band2", LINEUP_DEG[1]), ("tc-band1", LINEUP_DEG[0])):
        off = X_MAX_M * math.tan(math.radians(dev))
        out.append(f'<polygon class="{cls}" points="{_poly([(x(0), y(0)), (x(X_MAX_M), y(off)), (x(X_MAX_M), y(-off))])}"/>')
    if frame is not None:
        deck_top(frame, x, y, out, wire)
    out.append(f'<line class="tc-ideal" x1="{_f(x(X_MIN_M))}" y1="{_f(y(0))}" x2="{_f(x(X_MAX_M))}" y2="{_f(y(0))}"/>')
    for cls, run in _runs(samples, on_speed):
        out.append(f'<polyline class="tc-track tc-{cls}" points="{_poly([(x(s.along), y(s.lateral)) for s in run])}"/>')
    _call_markers(_in_view([c for c in calls if c["call"] in LINEUP_VALUES], view), samples, x,
                  lambda s: y(s.lateral), top, h, out)
    out.append("</g>")
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top + h}" x2="{PAD_L + PLOT_W}" y2="{top + h}"/>')
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top}" x2="{PAD_L}" y2="{top + h}"/>')
    if view is None:
        _distance_axis(top, h, x, out)
    else:
        _distance_axis_view(top, h, x, view[0], view[1], out)
    for m in (-half * 0.8, 0.0, half * 0.8):
        out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(y(m) + 4)}" font-size="11" '
                   f'text-anchor="end">{m / FT:+.0f} ft</text>')


def _in_view(calls: list[dict], view: tuple[float, float] | None) -> list[dict]:
    return calls if view is None else [c for c in calls if view[0] <= c["along"] <= view[1]]


def _table(grade: GradeResult, out: list[str]) -> None:
    top = TABLE_TOP
    cols = [PAD_L, PAD_L + 120, PAD_L + 260, PAD_L + 400, PAD_L + 520]
    heads = ["Position", "Glideslope", "Lineup", "AOA", "Remarks"]
    out.append(f'<line class="tc-rule" x1="{PAD_L}" y1="{top - 16}" x2="{PAD_L + PLOT_W}" y2="{top - 16}"/>')
    for cx, head in zip(cols, heads):
        out.append(f'<text class="tc-muted" x="{cx}" y="{top}" font-size="11" font-weight="600">{head}</text>')
    for i, st in enumerate(grade.positions):
        ry = top + 20 + i * 20
        remarks = " · ".join(r.english for r in grade.remarks if r.position is st.position)
        if st.samples == 0 or st.glideslope_deg is None:
            cells = [st.position.value, "not counted", "", "", ""]
        else:
            aoa = f"{st.aoa:.1f}°" if st.aoa is not None else "–"
            cells = [st.position.value, f"{st.glideslope_deg:+.2f}°", f"{st.lineup_deg:+.2f}° ({st.lateral_m / FT:+.0f} ft)",
                     aoa, remarks]
        for cx, cell in zip(cols, cells):
            out.append(f'<text class="tc-text" x="{cx}" y="{ry}" font-size="12">{escape(cell)}</text>')
    iw = " · ".join(r.english for r in grade.remarks if r.position.value == "IW")
    if iw:
        ry = top + 20 + len(grade.positions) * 20
        out.append(f'<text class="tc-text" x="{cols[0]}" y="{ry}" font-size="12">IW</text>')
        out.append(f'<text class="tc-text" x="{cols[4]}" y="{ry}" font-size="12">{escape(iw)}</text>')


def _legend(on_speed: tuple[float, float], out: list[str]) -> None:
    x0 = PAD_L + PLOT_W
    items = list(AOA_CLASSES)
    for i, cls in enumerate(reversed(items)):
        lx = x0 - (i + 1) * 104
        out.append(f'<line class="tc-track tc-{cls}" x1="{lx + 2}" y1="{SIDE_TOP - 14}" x2="{lx + 14}" y2="{SIDE_TOP - 14}"/>')
        out.append(f'<text class="tc-muted" x="{lx + 18}" y="{SIDE_TOP - 11}" font-size="11">{AOA_LABELS[cls]}</text>')


ACCURACY_COLORS = {"Full": "#1a7f37", "High": "#0969da", "Medium": "#bf8700", "Low": "#cf4d1a", "None": "#8c959f"}


def _badges(out: list[str], right: float, top: float, badges: list[tuple[str, str]]) -> None:
    """Small pills ending at `right`, right to left in the order given."""
    x = right
    for text, color in badges:
        w = 14 + len(text) * 6.3
        x -= w
        out.append(f'<rect x="{x:.1f}" y="{top}" width="{w:.1f}" height="18" rx="9" fill="{color}"/>'
                   f'<text x="{x + w / 2:.1f}" y="{top + 13}" font-size="11" font-weight="700" fill="#ffffff" '
                   f'text-anchor="middle">{escape(text)}</text>')
        x -= 6


WIND_UNKNOWN = "Wind not recorded (no dcs-lso server hook)"


def wind_text(p: PassResult) -> str:
    """The wind the pass was flown in, for the card and the pass page: over the angled deck, the wind itself, and
    the turbulence in the groove (WIND_UNKNOWN when the mission's wind isn't known)."""
    w = p.deck_wind
    if w is None:
        return WIND_UNKNOWN
    off = abs(w.off_axis)
    side = ("straight down the angled deck" if off < 1.0 else
            f"{off:.0f}° {'starboard' if w.off_axis > 0 else 'port'} of the angled deck")
    parts = [f"Wind over deck {w.speed / KT:.0f} kt, {side}"]
    if off >= 1.0 and abs(w.crosswind) / KT >= 1.0:
        parts[0] += f" ({abs(w.crosswind) / KT:.0f} kt across)"
    parts.append(f"wind {w.wind_speed / KT:.0f} kt from {w.wind_from:03.0f}°" if w.wind_speed / KT >= 0.5 else "winds calm")
    if w.turbulence is not None:
        parts.append(f"turbulence ±{w.turbulence / KT:.0f} kt" if w.turbulence / KT >= 0.5 else "smooth air, no turbulence")
    return " · ".join(parts)


def render_card(p: PassResult, grade: GradeResult, title: str = "", uid: str = "tc",
                calls: list[dict] | None = None, night: bool = False, view: tuple[float, float] | None = None,
                zoom_hint: bool = False, dark: bool = False, accuracy: str | None = None,
                elsewhere: bool = False) -> str:
    """`uid` prefixes element ids, so several cards can be inlined in one page. `calls` are the
    live LSO calls made during the pass ({"time", "along", "call"}), if any. `night`: flown at night
    (marked with a black dot, as on the greenie board). `view`: the stretch of the approach to show, as
    (near, far) meters short of the aim point (zoomed in); default the whole approach. The root element
    carries the view (data-near/data-far) so a page can zoom by dragging; `zoom_hint` says so on the card.
    `dark`: always the dark colours (otherwise they follow the viewer's light or dark mode). `accuracy`: the
    landing's overall accuracy level (hub/accuracy.py), shown as a badge; `elsewhere`: flown on another
    server than the hub's own, marked beside it."""
    calls = sorted(calls or [], key=_said_order)
    listed: list[tuple[str, list[str]]] = []  # (heading, items) for each line of the lists below the table
    for heading, group in (("Pilot:", [c for c in calls if c.get("by") == "pilot"]),
                           ("LSO calls:", [c for c in calls if c.get("by") != "pilot"])):
        if group:
            lines = _wrap(heading, [f"{call_label(c)} ({c['along'] / NM:.2f} nm)" for c in group], 12, PLOT_W)
            listed += [(heading if i == 0 else "", line) for i, line in enumerate(lines)]
    height = HEIGHT + (len(listed) * CALLS_LINE_H + 8 if listed else 0)
    aircraft = AIRCRAFT[p.aircraft_type]
    frame = DeckFrame(CARRIERS[p.carrier_type], aircraft)
    near, far = view if view else (X_MIN_M, X_MAX_M)
    margin = (far - near) * 0.05  # keep the lines running to the plot edges
    samples = [s for s in p.samples if near - margin <= s.along <= far + margin]
    x = Axis(far, near, PAD_L, PAD_L + PLOT_W)
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="tc" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img" aria-label="Trap card: {escape(p.pilot)} {escape(grade.text)}" '
        f'data-near="{near:.1f}" data-far="{far:.1f}" data-pad-l="{PAD_L}" data-plot-w="{PLOT_W}">',
        f"<style>{DARK_STYLE if dark else STYLE}</style>",
        f'<rect class="tc-bg" width="{WIDTH}" height="{height}" rx="10"/>',
    ]
    pilot = escape(p.pilot or f"id {p.aircraft_id:x}")
    out.append(f'<text class="tc-text" x="{PAD_L}" y="34" font-size="20" font-weight="700">{pilot}</text>')
    sub = f"{p.aircraft_type} · {p.carrier_type} · {p.outcome.value} · t={p.start_time:.0f}s"
    if title:
        sub = f"{title} · {sub}"
    if night:
        out.append(f'<circle cx="{PAD_L + 5}" cy="52" r="5" fill="#111" stroke="#fff" stroke-opacity="0.6"/>')
        out.append(f'<text class="tc-text" x="{PAD_L + 15}" y="56" font-size="12" font-weight="600">Night'
                   f'<tspan class="tc-muted" font-weight="400">\u00a0· {escape(sub)}</tspan></text>')
    else:
        out.append(f'<text class="tc-muted" x="{PAD_L}" y="56" font-size="12">{escape(sub)}</text>')
    gx = PAD_L + PLOT_W
    out.append(f'<text class="{_grade_class(grade.grade.value)}" x="{gx}" y="36" font-size="26" font-weight="700" '
               f'text-anchor="end">{escape(grade_short(grade.grade.value))}</text>')
    detail = f"{grade_name(grade.grade.value)} · {grade.points:g} pts · grading v{grade.version}"
    if p.wire_label:
        detail = f"wire {p.wire_label} · " + detail
    out.append(f'<text class="tc-muted" x="{gx}" y="56" font-size="12" text-anchor="end">{escape(detail)}</text>')
    if accuracy:
        _badges(out, gx - 78, 20, [(f"{accuracy} accuracy", ACCURACY_COLORS.get(accuracy, "#8c959f"))]
                + ([("another server", "#6e40c9")] if elsewhere else []))
    if p.dcs_grade:
        dcs = p.dcs_grade.raw.removeprefix("LSO:").strip()
        out.append(f'<text class="tc-muted" x="{gx}" y="72" font-size="11" text-anchor="end">'
                   f'DCS LSO: {escape(dcs)}</text>')
    wind = wind_text(p)
    out.append(f'<text class="{"tc-muted" if wind == WIND_UNKNOWN else "tc-text"}" x="{PAD_L}" y="{HEADER_H - 14}" '
               f'font-size="12">{escape(wind)}</text>')
    _legend(aircraft.on_speed_aoa, out)
    if zoom_hint:
        hint = "drag across a chart to zoom" if view is None else "zoomed in · double-click to reset"
        out.append(f'<text class="tc-muted" x="{PAD_L + PLOT_W}" y="{TOP_TOP - 6}" font-size="11" '
                   f'text-anchor="end">{hint}</text>')
    _side_view(p, frame, samples, x, p.wire if p.wire is not None else p.wire_estimate, aircraft.on_speed_aoa,
               uid, out, calls, view)
    _top_view(samples, x, aircraft.on_speed_aoa, uid, out, calls, frame,
              p.wire if p.wire is not None else p.wire_estimate, view)
    _table(grade, out)
    for i, (heading, line) in enumerate(listed):
        more = i + 1 < len(listed) and not listed[i + 1][0]  # the same list goes on to the next line
        text = escape(", ".join(line)) + ("," if more else "")
        lead = f'<tspan font-weight="600">{heading}</tspan> ' if heading else ""
        out.append(f'<text class="tc-text" x="{PAD_L}" y="{HEIGHT + 4 + i * CALLS_LINE_H}" font-size="12">'
                   f'{lead}{text}</text>')
    out.append("</svg>")
    return "\n".join(out)
