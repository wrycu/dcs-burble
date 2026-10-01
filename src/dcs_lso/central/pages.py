"""HTML pages for the central service: the greenie board and per-pass pages."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape

from .db import Pass

# Greenie board colours per grade (light, dark).
GRADE_COLORS = {
    "_OK_": ("#116329", "#2ea043"),
    "OK": ("#2da44e", "#3fb950"),
    "(OK)": ("#d4a72c", "#d29922"),
    "---": ("#8a5a2b", "#a0703c"),
    "B": ("#0969da", "#388bfd"),
    "WO": ("#6e7781", "#8b949e"),
    "C": ("#cf222e", "#f85149"),
}
GRADE_NAMES = {"_OK_": "Perfect", "OK": "OK", "(OK)": "Fair", "---": "No grade", "B": "Bolter",
               "WO": "Wave-off", "C": "Cut"}

STYLE = """
:root { --bg: #f6f8fa; --card: #ffffff; --text: #1f2328; --muted: #656d76; --border: #d0d7de; --empty: #eaeef2; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #010409; --card: #0d1117; --text: #e6edf3; --muted: #8d96a0; --border: #30363d; --empty: #161b22; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
a { color: inherit; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 16px; }
.panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
.scroll { overflow-x: auto; }
table.board { border-collapse: separate; border-spacing: 3px; }
table.board th { text-align: left; color: var(--muted); font-size: 12px; font-weight: 600; padding: 4px 8px; white-space: nowrap; }
table.board td.pilot { padding: 4px 8px; white-space: nowrap; font-weight: 600; }
table.board td.num { padding: 4px 8px; text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.cell { display: block; width: 34px; height: 30px; border-radius: 6px; color: #fff; text-decoration: none;
        font-size: 11px; font-weight: 700; line-height: 30px; text-align: center; }
.cell.empty { background: var(--empty); }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.legend span.swatch { display: inline-block; width: 14px; height: 14px; border-radius: 4px; vertical-align: -2px; margin-right: 4px; }
form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
form.filters select, form.filters button { font: inherit; padding: 4px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0 0 16px; }
dl.facts dt { color: var(--muted); }
dl.facts dd { margin: 0; }
.card svg { display: block; width: 100%; height: auto; }
.empty-state { color: var(--muted); padding: 24px; text-align: center; }
"""

GRADE_CSS = "\n".join(
    f'.g-{i} {{ background: {light}; }}' for i, (light, _) in enumerate(GRADE_COLORS.values())
) + "\n@media (prefers-color-scheme: dark) {\n" + "\n".join(
    f'  .g-{i} {{ background: {dark}; }}' for i, (_, dark) in enumerate(GRADE_COLORS.values())
) + "\n}"
GRADE_CLASS = {g: f"g-{i}" for i, g in enumerate(GRADE_COLORS)}


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{STYLE}\n{GRADE_CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown time"


def _short(grade: str) -> str:
    return {"_OK_": "OK+", "(OK)": "(OK)"}.get(grade, grade)


def board_page(passes: list[Pass], pilots: list[str], sources: list[str], days: int, pilot: str | None,
               source: str | None, columns: int) -> str:
    by_pilot: dict[str, list[Pass]] = defaultdict(list)
    for p in sorted(passes, key=lambda p: (p.occurred_at or p.created_at)):
        by_pilot[p.pilot.name].append(p)

    def option(value: str, label: str, selected: str | None) -> str:
        sel = " selected" if value == (selected or "") else ""
        return f'<option value="{escape(value)}"{sel}>{escape(label)}</option>'

    filters = (
        '<form class="filters" method="get">'
        '<label>Period <select name="days">'
        + "".join(option(str(d), label, str(days)) for d, label in ((7, "7 days"), (30, "30 days"), (90, "90 days"),
                                                                    (365, "1 year"), (0, "All time")))
        + '</select></label><label>Pilot <select name="pilot">' + option("", "All", pilot)
        + "".join(option(n, n, pilot) for n in pilots)
        + '</select></label><label>Source <select name="source">' + option("", "All", source)
        + "".join(option(n, n, source) for n in sources)
        + '</select></label><button type="submit">Apply</button></form>'
    )

    rows = []
    for name in sorted(by_pilot, key=str.lower):
        items = by_pilot[name]
        graded = [p for p in items if p.grade]
        traps = sum(p.outcome == "trap" for p in items)
        avg = sum(p.grade.points for p in graded) / len(graded) if graded else 0.0
        shown = items[-columns:]
        cells = []
        for p in shown:
            g = p.grade.grade if p.grade else "?"
            title = f"{g} · {p.grade.text if p.grade else ''} · {_when(p.occurred_at)} · {p.mission or ''}"
            cells.append(f'<td><a class="cell {GRADE_CLASS.get(g, "")}" href="/passes/{p.id}" '
                         f'title="{escape(title)}">{escape(_short(g))}</a></td>')
        cells += ['<td><span class="cell empty"></span></td>'] * (columns - len(shown))
        rows.append(f'<tr><td class="pilot">{escape(name)}</td><td class="num">{len(items)}</td>'
                    f'<td class="num">{avg:.2f}</td><td class="num">{100 * traps / len(items):.0f}%</td>'
                    + "".join(cells) + "</tr>")

    legend = '<div class="legend">' + "".join(
        f'<span><span class="swatch {GRADE_CLASS[g]}"></span>{escape(g)} {GRADE_NAMES[g]}</span>' for g in GRADE_COLORS
    ) + "</div>"
    period = f"last {days} days" if days else "all time"
    if rows:
        table = (f'<div class="scroll"><table class="board"><thead><tr><th>Pilot</th><th>Passes</th><th>Avg</th>'
                 f'<th>Traps</th><th colspan="{columns}">Passes (oldest → newest)</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        table = '<p class="empty-state">No passes yet for this filter.</p>'
    body = (f"<h1>Greenie Board</h1><p class=\"sub\">{len(passes)} passes · {period}</p>"
            f'{filters}<div class="panel">{table}{legend}</div>')
    return _page("Greenie Board", body)


def pass_page(p: Pass, card_svg: str | None, error: str | None = None) -> str:
    g = p.grade
    facts = [
        ("Pilot", p.pilot.name),
        ("When", _when(p.occurred_at)),
        ("Mission", p.mission or "–"),
        ("Carrier", f"{p.carrier_unit or ''} ({p.carrier_type})"),
        ("Aircraft", p.aircraft_type),
        ("Outcome", p.outcome),
        ("Grade", f"{g.text} ({g.points:g} pts, grading v{g.version})" if g else "not graded"),
        ("DCS LSO", p.dcs_grade or "–"),
        ("Wire", f"#{p.wire}" if p.wire else "–"),
        ("Source", p.source.name),
    ]
    dl = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)
    card = f'<div class="panel card">{card_svg}</div>' if card_svg else f'<p class="empty-state">{escape(error or "")}</p>'
    body = (f'<p class="sub"><a href="/">← Greenie board</a> · <a href="/passes/{p.id}/acmi">Download ACMI</a></p>'
            f"<h1>{escape(p.pilot.name)} · {escape(g.grade if g else '?')}</h1>"
            f'<dl class="facts">{dl}</dl>{card}')
    return _page(f"{p.pilot.name} {g.grade if g else ''}", body)

