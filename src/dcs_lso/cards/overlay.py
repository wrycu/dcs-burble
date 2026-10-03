"""Several passes' tracks overlaid on one card (glideslope and lineup), coloured by grade.

Uses the trap card's axes, bands and groove positions (`cards.svg`), so a pilot's habits show as a
bundle of lines: where they cluster off the ideal is what keeps happening.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape

from ..detect import PassResult
from ..geometry import AIRCRAFT, CARRIERS, DeckFrame
from ..grading import grade_name
from ..grading.grade import GLIDESLOPE_DEG, LINEUP_DEG, POSITIONS
from .svg import (FT, NM, PAD_L, PLOT_W, STYLE, WIDTH, X_MAX_M, X_MIN_M, Axis, _clip, _f, _poly,
                  deck_top)

# Grade colours (light, dark), shared with the greenie board.
GRADE_COLORS = {
    "_OK_": ("#116329", "#2ea043"),
    "OK": ("#2da44e", "#3fb950"),
    "(OK)": ("#d4a72c", "#d29922"),
    "---": ("#8a5a2b", "#a0703c"),
    "B": ("#0969da", "#388bfd"),
    "WO": ("#6e7781", "#8b949e"),
    "C": ("#cf222e", "#f85149"),
}
_GRADE_CLASS = {g: f"ov-g{i}" for i, g in enumerate(GRADE_COLORS)}
_STYLE = (STYLE + "\n.ov-track { fill: none; stroke-width: 1.6; stroke-opacity: 0.75; stroke-linejoin: round; }\n"
          ".ov-track.latest { stroke-width: 2.6; stroke-opacity: 1; }\n"
          "a:hover .ov-track { stroke-width: 3.2; stroke-opacity: 1; }\n"
          + "\n".join(f".{c} {{ stroke: {GRADE_COLORS[g][0]}; }} .{c}-fill {{ fill: {GRADE_COLORS[g][0]}; }}"
                      for g, c in _GRADE_CLASS.items())
          + "\n@media (prefers-color-scheme: dark) {\n"
          + "\n".join(f"  .{c} {{ stroke: {GRADE_COLORS[g][1]}; }} .{c}-fill {{ fill: {GRADE_COLORS[g][1]}; }}"
                      for g, c in _GRADE_CLASS.items()) + "\n}\n")

LEGEND_H = 30
SIDE_TOP, SIDE_H = LEGEND_H + 24, 250
TOP_TOP, TOP_H = SIDE_TOP + SIDE_H + 44, 170
HEIGHT = TOP_TOP + TOP_H + 34


@dataclass(frozen=True, slots=True)
class OverlayPass:
    result: PassResult
    grade: str
    href: str | None = None  # where clicking the line goes (e.g. the pass page)
    label: str = ""  # hover text
    pass_id: int | None = None  # lets the page preview this pass's trap card on hover


def _track(item: OverlayPass, points: list[tuple[float, float]], latest: bool) -> str:
    cls = f"ov-track {_GRADE_CLASS.get(item.grade, '')}{' latest' if latest else ''}"
    line = f'<polyline class="{cls}" points="{_poly(points)}"><title>{escape(item.label)}</title></polyline>'
    data = f' data-pass="{item.pass_id}"' if item.pass_id is not None else ""
    return f'<a href="{escape(item.href)}"{data}>{line}</a>' if item.href else line


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


def render_overlay(items: list[OverlayPass], uid: str = "ov", title: str = "",
                   view: tuple[float, float] | None = None) -> str:
    """`items` newest first; the newest is drawn on top and thicker. `view`: the stretch of the approach
    to show, as (near, far) distances short of the aim point in meters (zoomed in); default the whole
    approach. The root element carries the view (data-near/data-far) so a page can zoom by dragging."""
    first = items[0].result if items else None
    aircraft = AIRCRAFT.get(first.aircraft_type) if first else None
    frame = DeckFrame(CARRIERS.get(first.carrier_type, next(iter(CARRIERS.values()))), aircraft) if aircraft else None
    ramp = frame.carrier.ramp_along_m if frame else next(iter(CARRIERS.values())).ramp_along_m
    glide = aircraft.glideslope if aircraft else 3.6
    near, far = view if view else (X_MIN_M, X_MAX_M)
    margin = (far - near) * 0.05  # keep the lines running to the plot edges
    shown = [(item, [s for s in item.result.samples if near - margin <= s.along <= far + margin]) for item in items]
    in_view = [s for _, ss in shown for s in ss if near <= s.along <= far]
    x = Axis(far, near, PAD_L, PAD_L + PLOT_W)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" class="tc" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" '
           f'height="{HEIGHT}" role="img" aria-label="{escape(title or "Passes overlaid")}" '
           f'data-near="{near:.1f}" data-far="{far:.1f}" data-pad-l="{PAD_L}" data-plot-w="{PLOT_W}">',
           f"<style>{_STYLE}</style>", f'<rect class="tc-bg" width="{WIDTH}" height="{HEIGHT}" rx="10"/>']

    # Legend: the grades present, with how many passes each.
    counts: dict[str, int] = {}
    for item in items:
        counts[item.grade] = counts.get(item.grade, 0) + 1
    lx = PAD_L
    for g in [g for g in GRADE_COLORS if g in counts]:
        label = f"{grade_name(g)} ×{counts[g]}"
        out.append(f'<rect class="{_GRADE_CLASS[g]}-fill" x="{lx}" y="{LEGEND_H - 11}" width="12" height="12" rx="3"/>')
        out.append(f'<text class="tc-muted" x="{lx + 16}" y="{LEGEND_H - 1}" font-size="12">{escape(label)}</text>')
        lx += 28 + len(label) * 7
    hint = "drag across to zoom · newest is thickest" if view is None else "zoomed in · double-click to reset"
    out.append(f'<text class="tc-muted" x="{PAD_L + PLOT_W}" y="{LEGEND_H - 1}" font-size="12" text-anchor="end">'
               f"{hint}</text>")

    # Glideslope (side view).
    def ideal(along: float) -> float:
        return max(along, 0.0) * math.tan(math.radians(glide))

    if view is None:
        peak = max([s.hook_height for s in in_view if s.along > 0] + [0.0])
        y_lo, y_hi = -6.0, min(max(ideal(X_MAX_M) * 1.45, peak * 1.05, 40.0), 260.0)
    else:
        heights = [s.hook_height for s in in_view] + [ideal(near), ideal(far)]
        lo, hi = min(heights), max(heights)
        pad = max(1.5, (hi - lo) * 0.12)
        y_lo, y_hi = max(lo - pad, -6.0), hi + pad
    y = Axis(y_lo, y_hi, SIDE_TOP + SIDE_H, SIDE_TOP)
    out.append(f'<text class="tc-text" x="{PAD_L}" y="{SIDE_TOP - 6}" font-size="13" font-weight="600">'
               "Glideslope (hook height above deck)</text>")
    _clip(f"{uid}-side", SIDE_TOP, SIDE_H, out)
    for cls, dev in (("tc-band3", GLIDESLOPE_DEG[2]), ("tc-band2", GLIDESLOPE_DEG[1]), ("tc-band1", GLIDESLOPE_DEG[0])):
        hi_h = X_MAX_M * math.tan(math.radians(glide + dev))
        lo_h = X_MAX_M * math.tan(math.radians(max(glide - dev, 0.0)))
        out.append(f'<polygon class="{cls}" points="{_poly([(x(0), y(0)), (x(X_MAX_M), y(hi_h)), (x(X_MAX_M), y(lo_h))])}"/>')
    out.append(f'<line class="tc-ideal" x1="{_f(x(0))}" y1="{_f(y(0))}" x2="{_f(x(X_MAX_M))}" y2="{_f(y(ideal(X_MAX_M)))}"/>')
    out.append(f'<rect class="tc-deck" x="{_f(x(ramp))}" y="{_f(y(0))}" '
               f'width="{_f(x(X_MIN_M) - x(ramp))}" height="{_f(abs(y(-6.0) - y(0)))}"/>')
    for along in (frame.wire_along if frame else ()):
        out.append(f'<line class="tc-wire" x1="{_f(x(along))}" y1="{_f(y(0) - 5)}" x2="{_f(x(along))}" y2="{_f(y(0) + 3)}"/>')
    for i, (item, ss) in reversed(list(enumerate(shown))):
        if len(ss) > 1:
            out.append(_track(item, [(x(s.along), y(s.hook_height)) for s in ss], i == 0))
    out.append("</g>")
    _frame(SIDE_TOP, SIDE_H, x, near, far, out)
    span_ft = (y_hi - y_lo) / FT
    step_ft = _step(span_ft, (1, 2, 5, 10, 25, 50, 100, 200), 8)
    ft = math.ceil(y_lo / FT / step_ft) * step_ft
    while ft <= y_hi / FT:
        out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(y(ft * FT) + 4)}" font-size="11" '
                   f'text-anchor="end">{ft:g} ft</text>')
        ft += step_ft

    # Lineup (top view).
    reach = max([abs(s.lateral) for s in in_view if s.along > 0 or view is not None] + [0.0])
    if view is None:
        half = min(max(reach * 1.1, X_MAX_M * math.tan(math.radians(LINEUP_DEG[2])) * 1.2), 200.0)
    else:
        half = max(reach * 1.15, 3.0)
        if frame is not None and near < ramp:  # the deck is in view: show all of its width
            half = max(half, max(abs(lat) for ends in frame.wire_ends for _, lat in ends) * 1.15)
    y = Axis(-half, half, TOP_TOP + TOP_H, TOP_TOP)
    out.append(f'<text class="tc-text" x="{PAD_L}" y="{TOP_TOP - 6}" font-size="13" font-weight="600">'
               "Lineup (right of centerline is up)</text>")
    _clip(f"{uid}-top", TOP_TOP, TOP_H, out)
    for cls, dev in (("tc-band3", LINEUP_DEG[2]), ("tc-band2", LINEUP_DEG[1]), ("tc-band1", LINEUP_DEG[0])):
        off = X_MAX_M * math.tan(math.radians(dev))
        out.append(f'<polygon class="{cls}" points="{_poly([(x(0), y(0)), (x(X_MAX_M), y(off)), (x(X_MAX_M), y(-off))])}"/>')
    if frame is not None:
        deck_top(frame, x, y, out)
    out.append(f'<line class="tc-ideal" x1="{_f(x(X_MIN_M))}" y1="{_f(y(0))}" x2="{_f(x(X_MAX_M))}" y2="{_f(y(0))}"/>')
    for i, (item, ss) in reversed(list(enumerate(shown))):
        if len(ss) > 1:
            out.append(_track(item, [(x(s.along), y(s.lateral)) for s in ss], i == 0))
    out.append("</g>")
    _frame(TOP_TOP, TOP_H, x, near, far, out)
    for m in (-half * 0.8, 0.0, half * 0.8):
        out.append(f'<text class="tc-muted" x="{PAD_L - 6}" y="{_f(y(m) + 4)}" font-size="11" '
                   f'text-anchor="end">{m / FT:+.0f} ft</text>')
    out.append("</svg>")
    return "\n".join(out)


def _frame(top: float, height: float, x: Axis, near: float, far: float, out: list[str]) -> None:
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top + height}" x2="{PAD_L + PLOT_W}" y2="{top + height}"/>')
    out.append(f'<line class="tc-axis" x1="{PAD_L}" y1="{top}" x2="{PAD_L}" y2="{top + height}"/>')
    _distance_axis_view(top, height, x, near, far, out)
