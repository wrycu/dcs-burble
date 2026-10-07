"""The greenie board as an image (SVG, dark): for Discord, where the board is one message kept up to date.

Laid out like the website's board (a section per airframe when there's more than one): each pilot's passes, average and trap rate, then their latest landings as
squares in the grade's colour with its short label, oldest first, night landings dotted, and a legend.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from ..grading import grade_name, grade_short
from .overlay import GRADE_COLORS

PAD = 28
ROW_H = 34
SECTION_H = 30  # an airframe's heading
CELL_W, CELL_H, CELL_GAP = 38, 26, 5
NAME_W, NUM_W = 210, 64
MAX_ROWS = 40

BG, PANEL, ROW_ALT, EMPTY = "#0d1117", "#161b22", "#1c2129", "#21262d"
TEXT, MUTED, BORDER = "#e6edf3", "#8d96a0", "#30363d"
FONT = 'font-family="DejaVu Sans, Verdana, Arial, sans-serif"'


@dataclass(frozen=True, slots=True)
class BoardRow:
    name: str
    passes: int
    average: float | None
    trap_rate: float | None  # 0..1
    cells: list[tuple[str, bool]]  # (grade, night), oldest first
    airframe: str = ""  # the table it's in (geometry.airframe); rows come grouped by it


def _color(grade: str) -> str:
    return GRADE_COLORS.get(grade, ("#6e7781", "#6e7781"))[1]  # the dark-mode shade


def render_board(rows: list[BoardRow], columns: int, title: str = "Greenie Board", subtitle: str = "") -> str:
    """`title`: "" to leave it out (e.g. when the message around the image already names it)."""
    shown, more = rows[:MAX_ROWS], len(rows) - MAX_ROWS
    # Each row's top within the table, with an airframe heading before each airframe's rows (if several).
    sectioned = len({r.airframe for r in shown}) > 1
    lines: list[tuple[str, BoardRow | str, float]] = []  # ("heading", airframe, y) or ("row", row, y)
    y = 0.0
    for i, row in enumerate(shown):
        if sectioned and (i == 0 or row.airframe != shown[i - 1].airframe):
            lines.append(("heading", row.airframe, y))
            y += SECTION_H
        lines.append(("row", row, y))
        y += ROW_H
    cells_x = PAD + NAME_W + 3 * NUM_W + 16
    width = cells_x + columns * (CELL_W + CELL_GAP) - CELL_GAP + PAD
    header_h = (30 if title else 0) + (22 if subtitle else 0)
    table_top = PAD + header_h
    body_h = max(y, ROW_H) + (24 if more > 0 else 0)
    legend_y = table_top + 30 + body_h + 34
    height = legend_y + 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" {FONT}>',
           f'<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>',
           ]
    if title:
        out.append(f'<text x="{PAD}" y="{PAD + 22}" font-size="24" font-weight="700" fill="{TEXT}">{escape(title)}</text>')
    if subtitle:
        out.append(f'<text x="{PAD}" y="{PAD + (30 if title else 0) + 13}" font-size="13" fill="{MUTED}">'
                   f'{escape(subtitle)}</text>')
    # Column headings.
    hy = table_top + 18
    out.append(f'<text x="{PAD + 10}" y="{hy}" font-size="12" font-weight="700" fill="{MUTED}">PILOT</text>')
    for i, label in enumerate(("PASSES", "AVG", "TRAPS")):
        out.append(f'<text x="{PAD + NAME_W + (i + 1) * NUM_W - 8}" y="{hy}" font-size="12" font-weight="700" '
                   f'fill="{MUTED}" text-anchor="end">{label}</text>')
    out.append(f'<text x="{cells_x}" y="{hy}" font-size="12" font-weight="700" fill="{MUTED}">'
               "LANDINGS (OLDEST → NEWEST)</text>")
    top = table_top + 30
    out.append(f'<rect x="{PAD}" y="{top}" width="{width - 2 * PAD}" height="{body_h}" rx="10" fill="{PANEL}" '
               f'stroke="{BORDER}"/>')
    if not shown:
        out.append(f'<text x="{width / 2}" y="{top + ROW_H / 2 + 5}" font-size="14" fill="{MUTED}" '
                   'text-anchor="middle">No passes yet.</text>')
    r = 0
    for kind, item, dy in lines:
        y = top + dy
        if kind == "heading":
            out.append(f'<text x="{PAD + 10}" y="{y + SECTION_H / 2 + 6}" font-size="15" font-weight="700" '
                       f'fill="{TEXT}">{escape(str(item).upper())}</text>')
            r = 0  # restart the row striping
            continue
        row = item
        r += 1
        if r % 2 == 0:
            out.append(f'<rect x="{PAD + 1}" y="{y}" width="{width - 2 * PAD - 2}" height="{ROW_H}" fill="{ROW_ALT}"/>')
        mid = y + ROW_H / 2 + 5
        name = row.name if len(row.name) <= 24 else row.name[:23] + "…"
        out.append(f'<text x="{PAD + 10}" y="{mid}" font-size="14" font-weight="700" fill="{TEXT}">{escape(name)}</text>')
        numbers = (str(row.passes), "–" if row.average is None else f"{row.average:.2f}",
                   "–" if row.trap_rate is None else f"{100 * row.trap_rate:.0f}%")
        for i, value in enumerate(numbers):
            out.append(f'<text x="{PAD + NAME_W + (i + 1) * NUM_W - 8}" y="{mid}" font-size="13" fill="{MUTED}" '
                       f'text-anchor="end">{value}</text>')
        cy = y + (ROW_H - CELL_H) / 2
        cells = row.cells[-columns:]
        for c in range(columns):
            cx = cells_x + c * (CELL_W + CELL_GAP)
            if c >= len(cells):
                out.append(f'<rect x="{cx}" y="{cy}" width="{CELL_W}" height="{CELL_H}" rx="5" fill="{EMPTY}"/>')
                continue
            grade, night = cells[c]
            out.append(f'<rect x="{cx}" y="{cy}" width="{CELL_W}" height="{CELL_H}" rx="5" fill="{_color(grade)}"/>')
            out.append(f'<text x="{cx + CELL_W / 2}" y="{cy + CELL_H / 2 + 4}" font-size="11" font-weight="700" '
                       f'fill="#ffffff" text-anchor="middle">{escape(grade_short(grade))}</text>')
            if night:
                out.append(f'<circle cx="{cx + CELL_W - 6}" cy="{cy + 6}" r="3.5" fill="#000000" stroke="#ffffff" '
                           'stroke-opacity="0.7" stroke-width="1"/>')
    if more > 0:
        out.append(f'<text x="{PAD + 10}" y="{top + y + 17}" font-size="12" fill="{MUTED}">'
                   f"and {more} more pilots</text>")
    # Legend.
    x = PAD
    for grade in GRADE_COLORS:
        label = f"{grade_short(grade)} {grade_name(grade)}"
        out.append(f'<rect x="{x}" y="{legend_y - 11}" width="14" height="14" rx="3" fill="{_color(grade)}"/>')
        out.append(f'<text x="{x + 20}" y="{legend_y}" font-size="12" fill="{MUTED}">{escape(label)}</text>')
        x += 40 + len(label) * 7
    out.append(f'<circle cx="{x + 6}" cy="{legend_y - 4}" r="4" fill="#000000" stroke="#ffffff" stroke-opacity="0.7"/>')
    out.append(f'<text x="{x + 16}" y="{legend_y}" font-size="12" fill="{MUTED}">Night</text>')
    out.append("</svg>")
    return "\n".join(out)
