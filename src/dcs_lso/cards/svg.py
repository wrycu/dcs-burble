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
RAMP_ALONG_M = 70.0  # deck edge, meters short of the aim point (Nimitz class)
X_MAX_M = 0.8 * NM
X_MIN_M = -40.0

WIDTH = 920
PAD_L, PAD_R = 64, 24
PLOT_W = WIDTH - PAD_L - PAD_R
HEADER_H = 90
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

STYLE = """
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
@media (prefers-color-scheme: dark) {
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
}
"""


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


def _clip(clip_id: str, top: float, height: float, out: list[str]) -> None:
    out.append(f'<defs><clipPath id="{clip_id}"><rect x="{PAD_L}" y="{_f(top)}" width="{PLOT_W}" '
               f'height="{_f(height)}"/></clipPath></defs>')
    out.append(f'<g clip-path="url(#{clip_id})">')


def _text_w(text: str, size: float) -> float:
    return len(text) * size * CHAR_W


def _call_markers(calls: list[dict], samples: list[PassSample], x: Axis, y_of, top: float, height: float,
                  out: list[str]) -> None:
    """Live calls the LSO made: a marker on the track where each was given, with its label on the
    nearest free row above (or else below) the track, so labels never overlap each other."""
    if not samples:
        return
    size, row = 11, 14
    placed: list[tuple[float, float, float, float]] = []  # label boxes: x0, y0, x1, y1
    for call in calls:
        near = min((s for s in samples if s.along >= 0), key=lambda s: abs(s.along - call["along"]), default=samples[0])
        cx, cy = x(near.along), y_of(near)
        label = call["call"].capitalize()
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
               on_speed: tuple[float, float], uid: str, out: list[str], calls: list[dict]) -> None:
    top, h = SIDE_TOP, SIDE_H
    glide = frame.aircraft.glideslope
    ideal_far = X_MAX_M * math.tan(math.radians(glide))
    peak = max([s.hook_height for s in samples if s.along > 0] + [0.0])
    y_hi = min(max(ideal_far * 1.45, peak * 1.05, 40.0), 260.0)
    y = Axis(-6.0, y_hi, top + h, top)
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
    out.append(f'<rect class="tc-deck" x="{_f(x(RAMP_ALONG_M))}" y="{_f(y(0))}" '
               f'width="{_f(x(X_MIN_M) - x(RAMP_ALONG_M))}" height="{_f(y(-6.0) - y(0))}"/>')
    for n, along in enumerate(frame.wire_along, start=1):
        cls = "tc-wire-caught" if n == wire else "tc-wire"
        out.append(f'<line class="{cls}" x1="{_f(x(along))}" y1="{_f(y(0) - 5)}" x2="{_f(x(along))}" y2="{_f(y(0) + 3)}"/>')
    for cls, run in _runs(samples, on_speed):
        out.append(f'<polyline class="tc-track tc-{cls}" points="{_poly([(x(s.along), y(s.hook_height)) for s in run])}"/>')
    _call_markers([c for c in calls if c["call"] not in LINEUP_VALUES], samples, x,
                  lambda s: y(s.hook_height), top, h, out)
    out.append("</g>")
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top + h}" x2="{PAD_L + PLOT_W}" y2="{top + h}"/>')
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top}" x2="{PAD_L}" y2="{top + h}"/>')
    _distance_axis(top, h, x, out)
    step_ft = 100 if y_hi / FT > 300 else 50
    for ft in range(0, int(y_hi / FT) + 1, step_ft):
        py = y(ft * FT)
        out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(py + 4)}" font-size="11" text-anchor="end">{ft} ft</text>')
    if wire:
        out.append(f'<text class="tc-muted" x="{_f(x(frame.wire_along[wire - 1]))}" y="{_f(y(0) - 9)}" font-size="11" '
                   f'text-anchor="middle">#{wire}</text>')


def _top_view(samples: list[PassSample], x: Axis, on_speed: tuple[float, float], uid: str, out: list[str],
              calls: list[dict]) -> None:
    top, h = TOP_TOP, TOP_H
    reach = max([abs(s.lateral) for s in samples if s.along > 0] + [0.0])
    half = min(max(reach * 1.1, X_MAX_M * math.tan(math.radians(LINEUP_DEG[2])) * 1.2), 200.0)
    y = Axis(-half, half, top + h, top)  # + lateral (right of centerline) is up: the LSO's view from behind
    out.append(f'<text class="tc-text" x="{PAD_L}" y="{top - 6}" font-size="13" font-weight="600">'
               f'Lineup (right of centerline is up)</text>')
    _clip(f"{uid}-top", top, h, out)
    for cls, dev in (("tc-band3", LINEUP_DEG[2]), ("tc-band2", LINEUP_DEG[1]), ("tc-band1", LINEUP_DEG[0])):
        off = X_MAX_M * math.tan(math.radians(dev))
        out.append(f'<polygon class="{cls}" points="{_poly([(x(0), y(0)), (x(X_MAX_M), y(off)), (x(X_MAX_M), y(-off))])}"/>')
    out.append(f'<line class="tc-ideal" x1="{_f(x(X_MIN_M))}" y1="{_f(y(0))}" x2="{_f(x(X_MAX_M))}" y2="{_f(y(0))}"/>')
    for cls, run in _runs(samples, on_speed):
        out.append(f'<polyline class="tc-track tc-{cls}" points="{_poly([(x(s.along), y(s.lateral)) for s in run])}"/>')
    _call_markers([c for c in calls if c["call"] in LINEUP_VALUES], samples, x, lambda s: y(s.lateral), top, h, out)
    out.append("</g>")
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top + h}" x2="{PAD_L + PLOT_W}" y2="{top + h}"/>')
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top}" x2="{PAD_L}" y2="{top + h}"/>')
    _distance_axis(top, h, x, out)
    for m in (-half * 0.8, 0.0, half * 0.8):
        out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(y(m) + 4)}" font-size="11" '
                   f'text-anchor="end">{m / FT:+.0f} ft</text>')


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


def render_card(p: PassResult, grade: GradeResult, title: str = "", uid: str = "tc",
                calls: list[dict] | None = None) -> str:
    """`uid` prefixes element ids, so several cards can be inlined in one page. `calls` are the
    live LSO calls made during the pass ({"time", "along", "call"}), if any."""
    calls = sorted(calls or [], key=lambda c: c["time"])
    listed = _wrap("LSO calls:", [f"{c['call'].capitalize()} ({c['along'] / NM:.2f} nm)" for c in calls],
                   12, PLOT_W) if calls else []
    height = HEIGHT + (len(listed) * CALLS_LINE_H + 8 if listed else 0)
    aircraft = AIRCRAFT[p.aircraft_type]
    frame = DeckFrame(CARRIERS[p.carrier_type], aircraft)
    samples = [s for s in p.samples if X_MIN_M <= s.along <= X_MAX_M * 1.05]
    x = Axis(X_MAX_M, X_MIN_M, PAD_L, PAD_L + PLOT_W)
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="tc" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img" aria-label="Trap card: {escape(p.pilot)} {escape(grade.text)}">',
        f"<style>{STYLE}</style>",
        f'<rect class="tc-bg" width="{WIDTH}" height="{height}" rx="10"/>',
    ]
    pilot = escape(p.pilot or f"id {p.aircraft_id:x}")
    out.append(f'<text class="tc-text" x="{PAD_L}" y="34" font-size="20" font-weight="700">{pilot}</text>')
    sub = f"{p.aircraft_type} · {p.carrier_type} · {p.outcome.value} · t={p.start_time:.0f}s"
    if title:
        sub = f"{title} · {sub}"
    out.append(f'<text class="tc-muted" x="{PAD_L}" y="56" font-size="12">{escape(sub)}</text>')
    gx = PAD_L + PLOT_W
    out.append(f'<text class="{_grade_class(grade.grade.value)}" x="{gx}" y="36" font-size="26" font-weight="700" '
               f'text-anchor="end">{escape(grade_short(grade.grade.value))}</text>')
    detail = f"{grade_name(grade.grade.value)} · {grade.points:g} pts · grading v{grade.version}"
    if p.wire:
        detail = f"wire #{p.wire} · " + detail
    out.append(f'<text class="tc-muted" x="{gx}" y="56" font-size="12" text-anchor="end">{escape(detail)}</text>')
    if p.dcs_grade:
        dcs = p.dcs_grade.raw.removeprefix("LSO:").strip()
        out.append(f'<text class="tc-muted" x="{gx}" y="72" font-size="11" text-anchor="end">'
                   f'DCS LSO: {escape(dcs)}</text>')
    _legend(aircraft.on_speed_aoa, out)
    _side_view(p, frame, samples, x, p.wire, aircraft.on_speed_aoa, uid, out, calls)
    _top_view(samples, x, aircraft.on_speed_aoa, uid, out, calls)
    _table(grade, out)
    for i, line in enumerate(listed):
        text = escape(", ".join(line)) + ("," if i < len(listed) - 1 else "")
        lead = '<tspan font-weight="600">LSO calls:</tspan> ' if i == 0 else ""
        out.append(f'<text class="tc-text" x="{PAD_L}" y="{HEIGHT + 4 + i * CALLS_LINE_H}" font-size="12">'
                   f'{lead}{text}</text>')
    out.append("</svg>")
    return "\n".join(out)
