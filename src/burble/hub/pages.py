"""HTML pages for the hub: the greenie board and per-pass pages."""

from __future__ import annotations

import json
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime
from html import escape
from urllib.parse import quote

from ..callouts.voice import EACH_CARRIER
from ..cards.overlay import GRADE_COLORS
from ..cards.theme import COLOR_VARS, FONT_VAR, GRADE_VARS, MAX_CSS, safe_css
from ..geometry import airframe
from ..grading import grade_name, grade_short
from ..telemetry.pages import STYLE as USAGE_STYLE, usage_body
from .accuracy import Accuracy
from .db import Pass, Upload

# Greenie board colours per grade (light, dark), shared with the overlay card.

STYLE = """
:root { --bg: #f6f8fa; --card: #ffffff; --text: #1f2328; --muted: #656d76; --border: #d0d7de; --empty: #eaeef2; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #010409; --card: #0d1117; --text: #e6edf3; --muted: #8d96a0; --border: #30363d; --empty: #161b22; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 var(--font, system-ui), -apple-system, "Segoe UI", sans-serif; }
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
.cell.night { position: relative; }
.token-once { border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
.token-once code { word-break: break-all; font-size: 13px; }
.notice { border: 1px solid #bf8700; border-radius: 8px; padding: 8px 12px; margin: 0 0 12px; }
.notice .actions { margin-top: 6px; }
.admin pre { font-size: 12px; overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }
.admin ul { margin: 4px 0 8px; padding-left: 20px; }
dialog.modal { max-width: min(640px, calc(100vw - 32px)); background: var(--card); color: var(--text);
  border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px; }
dialog.modal::backdrop { background: rgba(0, 0, 0, 0.5); }
dialog.modal h3 { margin: 8px 0 4px; }
button.link { background: none; border: 0; padding: 0; font: inherit; color: inherit; text-decoration: underline; cursor: pointer; }
form.inline { display: flex; gap: 6px; align-items: center; margin: 0; }
form.inline input { width: 9em; }
.cell.night::after, .legend span.night-dot { content: ""; position: absolute; top: 4px; right: 4px; width: 7px; height: 7px;
        border-radius: 50%; background: #111; box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.6); }
.legend span.night-dot { position: relative; display: inline-block; top: 0; right: 0; vertical-align: 0; margin-right: 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.legend span.swatch { display: inline-block; width: 14px; height: 14px; border-radius: 4px; vertical-align: -2px; margin-right: 4px; }
form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
form.filters label.check { display: flex; align-items: center; gap: 4px; }
form.filters select, form.filters button { font: inherit; padding: 4px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0 0 16px; }
dl.facts dt { color: var(--muted); }
dl.facts dd { margin: 0; }
.card svg { display: block; width: 100%; height: auto; }
.empty-state { color: var(--muted); padding: 24px; text-align: center; }
form.upload { display: grid; gap: 12px; max-width: 560px; }
form.upload label { display: grid; gap: 4px; font-weight: 600; }
form.upload .hint { font-weight: 400; color: var(--muted); font-size: 12px; }
form.upload input, form.upload button { font: inherit; padding: 6px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
form.upload button { justify-self: start; cursor: pointer; font-weight: 600; }
progress { width: 100%; }
table.results { border-collapse: collapse; width: 100%; }
table.results th, table.results td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
table.results th { color: var(--muted); font-size: 12px; }
.error { color: #cf222e; }
ul.themes { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
ul.themes li { display: flex; gap: 10px; align-items: baseline; }
.kind { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--muted); min-width: 64px; }
.bar { flex: none; width: 60px; height: 6px; border-radius: 3px; background: var(--empty); overflow: hidden; align-self: center; }
.bar span { display: block; height: 100%; background: var(--muted); }
.recent { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 8px; }
h2 { font-size: 16px; margin: 20px 0 8px; }
h2.airframe { margin: 4px 0 6px; }
.panel h2.airframe ~ h2.airframe { margin-top: 18px; }
h1 .modex { color: var(--muted); font-weight: 600; margin-left: 6px; }
#tc-pop { position: fixed; z-index: 10; width: min(640px, 92vw); pointer-events: none; background: var(--card);
  border: 1px solid var(--border); border-radius: 12px; padding: 6px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25); }
#tc-pop svg { display: block; width: 100%; height: auto; }
#tc-pop .loading { color: var(--muted); padding: 24px; text-align: center; }
.zoomable { position: relative; cursor: crosshair; user-select: none; }
.scroll-x { overflow-x: auto; }
.accuracy { width: 100%; border-collapse: collapse; margin: 4px 0 18px; font-size: 13px; }
.accuracy th { text-align: left; font-weight: 700; color: var(--muted); padding: 4px 10px 6px 0; }
.accuracy td { vertical-align: top; padding: 2px 10px 6px 0; }
.accuracy .split { border-left: 2px solid var(--border, #8c959f); padding-left: 10px; }
.accuracy .note { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.accuracy .yes { color: #1a7f37; font-weight: 700; } .accuracy .no { color: #cf222e; font-weight: 700; }
.level { display: inline-block; border-radius: 10px; padding: 1px 9px; color: #fff; font-size: 12px; font-weight: 700; }
.level-Full { background: #1a7f37; } .level-High { background: #0969da; } .level-Medium { background: #bf8700; }
.level-Low { background: #cf4d1a; } .level-None { background: #8c959f; } .level-na { background: #d0d7de; color: #57606a; }
.zoomable.loading svg { opacity: 0.5; }
.zoom-sel { position: absolute; top: 0; bottom: 0; background: rgba(9, 105, 218, 0.12);
  border-left: 1px solid #0969da; border-right: 1px solid #0969da; pointer-events: none; }
nav.top { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  font-size: 13px; color: var(--muted); margin: -8px 0 16px; }
nav.top a { color: var(--muted); text-decoration: none; } nav.top a:hover { color: var(--text); text-decoration: underline; }
nav.top .me { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
nav.top strong a { color: var(--text); }
nav.top form { display: inline; margin: 0; }
nav.top button { font: inherit; background: none; border: none; padding: 0; color: var(--muted); cursor: pointer; }
nav.top button:hover { color: var(--text); text-decoration: underline; }
nav.airframes { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--border); margin: 8px 0 12px; }
nav.airframes a { padding: 6px 12px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent;
  margin-bottom: -1px; font-weight: 600; }
nav.airframes a:hover { color: var(--text); }
nav.airframes a.current { color: var(--text); border-bottom-color: var(--text); }
nav.airframes .n { font-weight: 400; font-size: 12px; color: var(--muted); }
.admin .actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.admin .actions form { display: flex; gap: 6px; margin: 0; }
.admin button, .admin input, .admin textarea, .admin select { font: inherit; font-size: 13px; padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
.admin button { cursor: pointer; } .admin button.danger { color: #cf222e; }
.admin input.label { width: 8em; }
.admin textarea { width: 100%; min-height: 8em; font-family: ui-monospace, monospace; box-sizing: border-box; }
.admin .alias { display: inline-flex; gap: 4px; align-items: center; margin: 0 8px 2px 0; }
.admin .alias button { padding: 0 6px; font-size: 12px; }
.settings label.setting { display: grid; grid-template-columns: minmax(10em, 16em) minmax(0, 1fr); gap: 2px 12px;
  align-items: center; margin: 8px 0; }
.settings label.setting input:not([type=checkbox]), .settings label.setting select { max-width: 22em; width: 100%;
  box-sizing: border-box; }
.settings label.setting input[type=checkbox] { justify-self: start; }
.settings label.setting .hint { grid-column: 2; color: var(--muted); font-size: 12px; }
.settings h2 { margin: 18px 0 4px; font-size: 16px; } .settings h2:first-child { margin-top: 0; }
.settings .warn { color: #9a6700; font-size: 13px; }
.settings fieldset { border: 1px solid var(--border); border-radius: 8px; margin: 8px 0; padding: 6px 10px; }
.settings fieldset label { display: inline-flex; gap: 4px; align-items: center; margin: 2px 14px 2px 0; }
.settings fieldset .note { color: var(--muted); font-size: 12px; }
.settings table input { width: 7em; }
.settings table td.note { color: var(--muted); font-size: 12px; }
.settings .carriers input.unit { width: 14em; max-width: 38vw; }
.settings td.default { white-space: nowrap; }
@media (max-width: 600px) { .settings label.setting { grid-template-columns: minmax(0, 1fr); }
  .settings label.setting .hint { grid-column: 1; } }
.admin .tag { font-size: 11px; font-weight: 700; border-radius: 8px; padding: 1px 7px; background: #0969da; color: #fff; }
.zoom-reset { font: inherit; font-size: 12px; font-weight: 400; margin-left: 8px; padding: 2px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; vertical-align: 2px; }
"""

# Each grade's colour, overridable by its variable (cards.theme.GRADE_VARS) in the admins' custom CSS.
GRADE_CSS = "\n".join(
    f'.g-{i} {{ background: var({GRADE_VARS[g]}, {light}); }}' for i, (g, (light, _)) in enumerate(GRADE_COLORS.items())
) + "\n@media (prefers-color-scheme: dark) {\n" + "\n".join(
    f'  .g-{i} {{ background: var({GRADE_VARS[g]}, {dark}); }}' for i, (g, (_, dark)) in enumerate(GRADE_COLORS.items())
) + "\n}"
GRADE_CLASS = {g: f"g-{i}" for i, g in enumerate(GRADE_COLORS)}

# Who the request is signed in as (set per request by the app), for the bar at the top of every page.
CURRENT_PILOT: ContextVar[str | None] = ContextVar("current_pilot", default=None)
CURRENT_ADMIN: ContextVar[bool] = ContextVar("current_admin", default=False)
# For an admin: how many pilots are waiting for approval (shown on the Admin link).
CURRENT_PENDING: ContextVar[int] = ContextVar("current_pending", default=0)
# The signed-in pilot is waiting for an admin's approval (no Live link for them yet).
CURRENT_WAITING: ContextVar[bool] = ContextVar("current_waiting", default=False)
# The admins' custom CSS (set per request by the app), added after the built-in styles on every page.
CURRENT_CSS: ContextVar[str] = ContextVar("current_css", default="")


def _nav() -> str:
    me = CURRENT_PILOT.get()
    if me:
        quoted = quote(me, safe="")
        live = "" if CURRENT_WAITING.get() else '<a href="/live">Live</a>'  # not until an admin approves them
        right = (f'<span class="me">{live}Signed in as <strong><a href="/pilots/{quoted}">{escape(me)}</a></strong>'
                 f'<a href="/pilots/{quoted}/settings">Settings</a>'
                 + (f'<a href="/admin">Admin{f" ({n})" if (n := CURRENT_PENDING.get()) else ""}</a>'
                    if CURRENT_ADMIN.get() else "") +
                 '<form method="post" action="/signout"><button type="submit">Sign out</button></form></span>')
    else:
        right = '<span class="me"><a href="/signin">Sign in</a><a href="/join">Join</a></span>'
    return f'<nav class="top"><a href="/">Greenie Board</a>{right}</nav>'


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{STYLE}\n{GRADE_CSS}</style>"
            + (f"<style>{safe_css(css)}</style>" if (css := CURRENT_CSS.get()) else "") + "</head>"
            f"<body><main>{_nav()}{body}</main></body></html>")


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown time"




def board_page(passes: list[Pass], pilots: list[str], sources: list[str], days: int, pilot: str | None,
               source: str | None, columns: int, empty: bool = False, servers: str = "all",
               aircraft: str | None = None, airframes: list[str] | None = None) -> str:
    """A table per airframe (geometry.airframe: the F-14A and B are one), each pilot's landings in it.
    `empty`: also list pilots with no passes in the filter (e.g. joined, not flown here yet). `servers`: "ours"
    leaves out landings flown on other servers; `aircraft`: one airframe only (the passes are already
    filtered; these set the form). `airframes`: the choices for the Aircraft filter."""
    sections: dict[str, dict[str, list[Pass]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted(passes, key=lambda p: (p.occurred_at or p.created_at)):
        sections[airframe(p.aircraft_type)][p.pilot.name].append(p)

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
        + '</select></label><label>Aircraft <select name="aircraft">' + option("", "All", aircraft)
        + "".join(option(n, n, aircraft) for n in (airframes or []))
        + '</select></label><label>Source <select name="source">' + option("", "All", source)
        + "".join(option(n, n, source) for n in sources)
        + '</select></label><label>Servers <select name="servers">'
        + option("ours", "This hub's servers", servers) + option("all", "All servers", servers)
        + '</select></label><label class="check"><input type="checkbox" name="empty" value="1"'
        + (" checked" if empty else "") + "> Show pilots with no passes</label>"
        + '<button type="submit">Apply</button></form>'
    )

    def no_passes_row(name: str) -> str:
        return (f'<tr><td class="pilot"><a href="/pilots/{quote(name, safe="")}">{escape(name)}</a></td>'
                '<td class="num">0</td><td class="num">–</td><td class="num">–</td>'
                + '<td><span class="cell empty"></span></td>' * columns + "</tr>")

    def pilot_row(name: str, items: list[Pass]) -> str:
        graded = [p for p in items if p.grade]
        traps = sum(p.outcome == "trap" for p in items)
        avg = sum(p.grade.points for p in graded) / len(graded) if graded else 0.0
        shown = items[-columns:]
        cells = []
        for p in shown:
            g = p.grade.grade if p.grade else "?"
            night = " · night" if p.night else ""
            title = f"{grade_name(g)}: {p.grade.text if p.grade else ''}{night} · {_when(p.occurred_at)} · {p.mission or ''}"
            cells.append(f'<td><a class="cell {GRADE_CLASS.get(g, "")}{" night" if p.night else ""}" href="/passes/{p.id}" '
                         f'title="{escape(title)}">{escape(grade_short(g))}</a></td>')
        cells += ['<td><span class="cell empty"></span></td>'] * (columns - len(shown))
        return (f'<tr><td class="pilot"><a href="/pilots/{quote(name, safe="")}" title="Themes across recent passes">'
                f'{escape(name)}</a></td><td class="num">{len(items)}</td>'
                f'<td class="num">{avg:.2f}</td><td class="num">{100 * traps / len(items):.0f}%</td>'
                + "".join(cells) + "</tr>")

    def table(rows: list[str]) -> str:
        return (f'<div class="scroll"><table class="board"><thead><tr><th>Pilot</th><th>Passes</th><th>Avg</th>'
                f'<th>Traps</th><th colspan="{columns}">Passes (oldest → newest)</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>')

    # The busiest airframe first.
    order = sorted(sections, key=lambda a: (-sum(len(v) for v in sections[a].values()), a.lower()))
    parts = [f'<h2 class="airframe">{escape(a)}</h2>'
             + table([pilot_row(n, sections[a][n]) for n in sorted(sections[a], key=str.lower)]) for a in order]
    if empty:
        flown = {n for by_pilot in sections.values() for n in by_pilot}
        idle = sorted((n for n in pilots if n not in flown and (pilot is None or n == pilot)), key=str.lower)
        if idle:
            parts.append('<h2 class="airframe">No passes yet</h2>' + table([no_passes_row(n) for n in idle]))
    content = "".join(parts) if parts else '<p class="empty-state">No passes yet for this filter.</p>'

    legend = '<div class="legend">' + "".join(
        f'<span><span class="swatch {GRADE_CLASS[g]}"></span>{escape(grade_short(g))} {grade_name(g)}'
        f'{f" ({escape(g)})" if grade_short(g) != g else ""}</span>' for g in GRADE_COLORS
    ) + '<span><span class="night-dot"></span>Night pass</span></div>'
    period = f"last {days} days" if days else "all time"
    body = (f"<h1>Greenie Board</h1><p class=\"sub\">{len(passes)} passes · {period} · "
            '<a href="/upload">Upload a Tacview recording</a> · <a href="/join">Join this board</a></p>'
            f'{filters}<div class="panel">{content}{legend}</div>')
    return _page("Greenie Board", body)


def card_title(p: Pass) -> str:
    """The trap card's title line: the mission, the pilot's side number, and the livery, when known."""
    modex = p.pilot.modex if p.pilot else None
    return " · ".join(x for x in (p.mission, f"#{modex}" if modex else None, p.livery) if x)


def _wire(p: Pass) -> str:
    """DCS's own wire when known; otherwise the estimate from the stop point, marked as one."""
    if p.wire is not None:
        return f"#{p.wire} (DCS)"
    estimate = ((p.grade.detail or {}) if p.grade else {}).get("wire_estimate")
    return f"#{estimate} (estimated from where the jet stopped)" if estimate is not None else "–"


def _report(r: Pass, used: bool) -> str:
    info = (r.slice.sidecar or {}).get("pass") or {}
    rate = info.get("sample_rate_hz")
    detail = ", ".join(x for x in (f"{rate:g} Hz" if rate else "", "AOA" if info.get("aoa_recorded") else "",
                                   "own track only" if r.is_track else "") if x)
    text = f"{r.source.name} ({r.source.kind}{', ' + detail if detail else ''})"
    return (f'<a href="/passes/{r.id}/acmi">{escape(text)}</a>'
            + (" <strong>track used</strong>" if used else ""))


def _accuracy(a: Accuracy) -> str:
    """The landing's row of the accuracy table (docs/accuracy-scores.png): which parts reported it, and the scores."""
    def level(s) -> str:
        cls = "na" if s.level == "n/a" else s.level
        note = f'<span class="note">{escape(s.note)}</span>' if s.note else ""
        return f'<span class="level level-{cls}">{escape(s.level)}</span>{note}'

    heads = [p.name for p in a.parts] + ["Overall", "Approach", "Wire", "Comms"]
    cells = [f'<span class="{"yes" if p.present else "no"}">{"✓" if p.present else "✗"}</span>'
             + (f'<span class="note">{escape(p.note)}</span>' if p.note else "") for p in a.parts]
    cells += [level(s) for s in a.scores().values()]
    split = len(a.parts)
    th = "".join(f'<th{" class=split" if i == split else ""}>{escape(h)}</th>' for i, h in enumerate(heads))
    td = "".join(f'<td{" class=split" if i == split else ""}>{c}</td>' for i, c in enumerate(cells))
    return (f'<h2>Accuracy</h2><div class="scroll-x"><table class="accuracy"><tr>{th}</tr><tr>{td}</tr></table></div>')


def weather_text(weather: dict | None) -> str | None:
    """The mission's weather settings (the server hook's `weather` event) in a line: clouds, visibility, fog,
    temperature and pressure."""
    if not weather:
        return None
    parts = []
    if weather.get("dynamic"):
        parts.append("dynamic weather")
    clouds = weather.get("clouds") or {}
    if clouds.get("preset") or clouds.get("density"):
        base = clouds.get("base_m")
        parts.append("clouds" + (f" from {base / 0.3048:,.0f} ft" if base is not None else ""))
    else:
        parts.append("clear skies")
    precipitation = {1: "rain", 2: "thunderstorms", 3: "snow", 4: "snowstorms"}.get(clouds.get("precipitation") or 0)
    if precipitation:
        parts.append(precipitation)
    if (fog := weather.get("fog")) and fog.get("visibility_m"):
        parts.append(f"fog (visibility {fog['visibility_m'] / 1852:.1f} nm)")
    if (vis := weather.get("visibility_m")) is not None:
        parts.append(f"visibility {vis / 1852:.0f} nm")
    if weather.get("dust_m"):
        parts.append(f"dust (visibility {weather['dust_m'] / 1852:.1f} nm)")
    if (temp := weather.get("temperature")) is not None:
        parts.append(f"{temp:g} °C")
    if (qnh := weather.get("qnh_mmhg")) is not None:
        parts.append(f"QNH {qnh / 25.4:.2f} inHg")
    return ", ".join(parts)


def pass_page(p: Pass, card_svg: str | None, error: str | None = None, reports: list[Pass] | None = None,
              track_source: str | None = None, accuracy: Accuracy | None = None, wind: str | None = None,
              weather: dict | None = None, radio: list[dict] | None = None) -> str:
    g = p.grade
    facts = [
        ("Pilot", p.pilot.name),
        ("Side number", p.pilot.modex or "–"),
        ("When", _when(p.occurred_at)),
        ("Mission", p.mission or "–"),
        ("Carrier", f"{p.carrier_unit or ''} ({p.carrier_type})"),
        ("Aircraft", p.aircraft_type),
        ("Livery", p.livery or "–"),
        ("Outcome", p.outcome),
        ("Grade", f"{grade_name(g.grade)}: {g.text} ({g.points:g} pts, grading v{g.version})" if g else "not graded"),
        ("DCS LSO", p.dcs_grade or "–"),
        ("Wire", _wire(p)),
    ]
    if wind:
        facts.append(("Wind", wind.removeprefix("Wind ")))
    if (settings := weather_text(weather)) is not None:
        facts.append(("Weather", settings))
    if accuracy is not None:
        facts.append(("Flown on", accuracy.flown_label + (f" ({accuracy.flown.note})" if accuracy.flown.note else "")))
    if relayed := (p.slice.sidecar or {}).get("calls_from"):
        facts.append(("LSO calls", f"made on {relayed}'s server, relayed by the pilot hook"))
    dl = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)
    if radio:
        dl += f"<dt>Radio</dt><dd>{_radio(p, radio)}</dd>"
    reports = reports or [p]
    used = track_source or p.source.name
    listed = "<br>".join(_report(r, len(reports) > 1 and r.source.name == used) for r in reports)
    dl += f"<dt>{'Reports' if len(reports) > 1 else 'Source'}</dt><dd>{listed}</dd>"
    if card_svg:
        card = (f'<p class="sub"><button id="card-zoom-reset" class="zoom-reset" hidden>Reset zoom</button></p>'
                f'<div class="panel card zoomable" id="trap-card" data-src="/passes/{p.id}/card.svg?zoom=1" '
                f'data-reset="card-zoom-reset">{card_svg}</div><script>{ZOOM_SCRIPT}</script>')
    else:
        card = f'<p class="empty-state">{escape(error or "")}</p>'
    replay = f' · <a href="/passes/{p.id}/replay">Replay in 3D</a>' if card_svg else ""
    body = (f'<p class="sub"><a href="/">← Greenie board</a>{replay} · <a href="/passes/{p.id}/acmi">Download ACMI</a></p>'
            f"<h1>{escape(p.pilot.name)} · {escape(grade_name(g.grade) if g else '?')}</h1>"
            f'<dl class="facts">{dl}</dl>{_accuracy(accuracy) if accuracy else ""}{card}')
    return _page(f"{p.pilot.name} {g.grade if g else ''}", body)


def _radio(p: Pass, radio: list[dict]) -> str:
    """The pass's radio recording (PLAN #52): what's in it, and for its pilot or an admin, a way to delete it."""
    seconds = sum(t["seconds"] for t in radio)
    pilot = sum(t["by"] == "pilot" for t in radio)
    text = escape(f"recorded on the carrier frequency: {len(radio)} transmission{'s' if len(radio) != 1 else ''} "
                  f"({seconds:.0f} s, {pilot} from the pilot), played in the replay")
    if CURRENT_ADMIN.get() or CURRENT_PILOT.get() == p.pilot.name:
        ask = _js_text("Delete this pass's radio recording? It can't be undone.")
        text += (f' <form method="post" action="/passes/{p.id}/radio/delete" style="display:inline" '
                 f'onsubmit="return confirm(\'{ask}\')"><button type="submit" class="danger">Delete recording</button>'
                 f'</form>')
    return text


# three.js for the replay, served by the hub (static/; MIT licence alongside it).
THREE_DIR = "/static/three-0.169.0"

REPLAY_STYLE = """
[hidden] { display: none !important; }
.replay { position: relative; background: #000; border-radius: 12px; overflow: hidden; }
.replay canvas { display: block; width: 100%; height: min(70vh, 640px); min-height: 320px; touch-action: none; }
.replay .loading { position: absolute; inset: 0; display: grid; place-items: center; color: #ccc; }
.replay.ready .loading { display: none; }
.replay .hud { position: absolute; top: 10px; left: 10px; display: grid; grid-template-columns: max-content max-content;
  gap: 0 10px; padding: 6px 10px; border-radius: 8px; background: rgba(0, 0, 0, 0.55); color: #f0f3f6;
  font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; pointer-events: none; }
.replay .hud dt { color: #aab4be; }
.replay .hud dd { margin: 0; }
.replay .ballbox { position: absolute; top: 10px; right: 10px; width: 22px; height: 140px; border-radius: 6px;
  background: rgba(0, 0, 0, 0.55); pointer-events: none; }
.replay .ballbox::before, .replay .ballbox::after { content: ""; position: absolute; top: calc(50% - 2px);
  width: 14px; height: 4px; background: #3fdc5a; border-radius: 2px; }
.replay .ballbox::before { right: 100%; margin-right: 2px; }
.replay .ballbox::after { left: 100%; margin-left: 2px; }
.replay .ball { position: absolute; left: 3px; width: 16px; height: 16px; margin-top: -8px; border-radius: 50%;
  background: #ffa020; box-shadow: 0 0 8px #ffa020; }
.replay .ball.low { background: #ff3030; box-shadow: 0 0 8px #ff3030; }
.replay .call { position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%); max-width: calc(100% - 32px);
  padding: 4px 12px; border-radius: 6px; background: rgba(0, 0, 0, 0.65); color: #fff; font-size: 16px;
  text-align: center; pointer-events: none; }
/* The PLAT view, as the carrier's deck camera monitor shows it (and DCS's Supercarrier draws it): a grey 4:3 picture,
   a black crosshair with a gap around the jet, and the camera's data in big typewriter digits. */
.replay .platframe { display: none; position: absolute; top: 0; bottom: 0; left: 50%; transform: translateX(-50%);
  aspect-ratio: 1274 / 928; max-width: 100%; container-type: size; pointer-events: none;
  box-shadow: 0 0 0 100vmax #0b0d0c;
  background: radial-gradient(ellipse at center, transparent 55%, rgba(0, 0, 0, 0.35) 100%),
    repeating-linear-gradient(0deg, rgba(0, 0, 0, 0.04) 0 1px, transparent 1px 3px); }
.replay.plat .platframe { display: block; }
.replay.plat canvas { filter: grayscale(1) contrast(0.8) brightness(1.12) blur(0.4px); }
.replay.plat .hud, .replay.plat .ballbox { display: none; }
.replay .platframe span { position: absolute; color: #141414; font: 7.6cqh/1 "Courier New", Courier, monospace;
  white-space: pre; text-shadow: 0 0 1px rgba(0, 0, 0, 0.4); }
.replay .platframe .line { position: absolute; background: #161616; }
.replay-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 4px; }
.replay-bar button, .replay-bar select { font: inherit; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
.replay-bar button[aria-pressed="true"] { border-color: var(--text); font-weight: 600; }
.replay-bar input[type=range] { flex: 1 1 200px; }
.replay-bar .time { font-variant-numeric: tabular-nums; color: var(--muted); min-width: 9em; }
.replay-bar .group { display: flex; flex-wrap: wrap; gap: 4px; }
.replay-bar label { display: flex; gap: 4px; align-items: center; color: var(--muted); }
.lineup { margin-top: 10px; }
"""


def replay_page(p: Pass, lineup_svg: str | None = None) -> str:
    """The 3D replay of a landing (static/replay.js draws it from /passes/{id}/replay.json), with the trap card's
    lineup plot (`lineup_svg`, cards.render_lineup) below it, which follows the jet."""
    g = p.grade
    body = (f'<p class="sub"><a href="/passes/{p.id}">← The pass</a> · <a href="/">Greenie board</a></p>'
            f"<h1>{escape(p.pilot.name)} · {escape(grade_name(g.grade) if g else '?')}"
            f'{" · " + escape(g.text) if g and g.text else ""}</h1>'
            f'<p class="sub">{escape(_when(p.occurred_at))} · {escape(p.aircraft_type)} · '
            f'{escape(p.carrier_unit or p.carrier_type)}{" · wire " + escape(_wire(p)) if p.wire else ""}</p>'
            f'<div class="replay" id="replay" data-src="/passes/{p.id}/replay.json">'
            f'<canvas aria-label="3D replay of the pass"></canvas>'
            + _scene_overlays("Loading the replay…") + '</div>'
            f'<div class="replay-bar"><button type="button" id="replay-play">Pause</button>'
            f'<input type="range" id="replay-scrub" min="0" value="0" aria-label="Time">'
            f'<span class="time" id="replay-time"></span>'
            f'<select id="replay-speed" aria-label="Speed">'
            + "".join(f'<option value="{v}"{" selected" if v == "1" else ""}>{v}×</option>'
                      for v in ("0.25", "0.5", "1", "2", "4"))
            + '</select><button type="button" id="replay-sound" aria-pressed="true" hidden>Sound on</button>'
            '<button type="button" id="replay-restart">From the start</button>'
            f'<button type="button" id="replay-groove">From the groove</button></div>'
            f'<div class="replay-bar"><span class="group" id="replay-views"></span>'
            f'<label><input type="checkbox" data-guide="glidepath" checked> Glide path</label>'
            f'<label><input type="checkbox" data-guide="path" checked> Path flown</label></div>'
            + (f'<div class="panel card lineup" id="replay-lineup">{lineup_svg}</div>' if lineup_svg else "") +
            f'<p class="sub" id="replay-hint"></p>'
            f'<p class="sub">Space plays and pauses, the arrow keys step a second (with Shift, five), 1–6 pick the '
            f"camera, and the mouse wheel zooms. The jet and ship are simple stand-in models; positions are from "
            f"the landing's best track, smoothed between samples.</p>"
            f'<script type="importmap">{_imports()}</script>'
            f'<script type="module" src="/static/replay.js"></script>')
    return _page(f"Replay: {p.pilot.name} {g.grade if g else ''}", f"<style>{REPLAY_STYLE}</style>{body}")


LIVE_STYLE = """
.replay .labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
.replay .labels .label { position: absolute; transform: translate(-50%, -100%); padding: 1px 6px; border-radius: 4px;
  background: rgba(0, 0, 0, 0.5); color: #e8edf2; font-size: 12px; white-space: nowrap; }
.replay .labels .label.followed { background: rgba(255, 208, 64, 0.85); color: #111; font-weight: 600; }
.replay-bar button[aria-pressed="mixed"] { border-style: dashed; border-color: var(--text); }
.live-status { color: var(--muted); }
.live-graded { margin: 8px 0; padding-left: 1.2em; }
tr.muted td, .muted { color: var(--muted); }
"""


def _imports() -> str:
    return json.dumps({"imports": {"three": f"{THREE_DIR}/three.module.min.js",
                                   "three/addons/controls/OrbitControls.js": f"{THREE_DIR}/OrbitControls.js"}})


def _scene_overlays(loading: str) -> str:
    """What's drawn over the 3D view: the PLAT monitor's frame, the HUD, the ball, the subtitle, and a loading line."""
    hud = "".join(f'<dt>{label}</dt><dd id="hud-{key}">–</dd>'
                  for key, label in (("range", "Range"), ("gs", "Glideslope"), ("lineup", "Lineup"), ("aoa", "AOA")))
    return ('<div class="platframe" aria-hidden="true">'
            # Laid out as on the deck camera's monitor (positions in a 1274 x 928 picture, as DCS's Supercarrier has them).
            + "".join(f'<span id="plat-{k}" style="left:{x}%;top:{y}%"></span>'
                      for k, x, y in (("date", 15.5, 1.1), ("time", 15.5, 8.6), ("status", 63.4, 1.1), ("hull", 76.8, 1.1),
                                      ("wind", 69.1, 8.6), ("speed", 15.5, 85.3), ("waveoff", 37.9, 85.3),
                                      ("distance", 50.0, 85.3), ("sink", 76.8, 85.3)))
            + "".join(f'<div class="line" id="plat-{k}"></div>' for k in ("left", "right", "top", "bottom"))
            + '</div>'
            f'<dl class="hud">{hud}</dl><div class="ballbox" id="hud-ballbox" hidden><div class="ball" id="hud-ball"></div></div>'
            f'<div class="call" id="hud-call" hidden></div><div class="loading">{escape(loading)}</div>')


LIVE_STATUS = {
    "live": "Live",
    "running": "Mission running, no carrier",
    "paused": "Mission paused (a dedicated server pauses with no players)",
    "silent": "No live data",
}


def _live_url(agent: str, unit: str | None = None) -> str:
    return f"/live/{quote(agent, safe='')}" + (f"/{quote(unit, safe='')}" if unit else "")


def _ago(when: datetime) -> str:
    seconds = (datetime.now(when.tzinfo) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return _when(when)


def _live_status(a) -> str:
    """A server agent's status (hub.live.AgentStatus) in words."""
    text = escape(LIVE_STATUS[a.status])
    if a.status == "silent":
        text += (" since the hub started" if a.heard is None
                 else f' <span class="muted">· last heard {escape(_ago(a.heard))}</span>')
    return text


def _live_carriers(a) -> str:
    """Links to a server's live carriers, with who's around each."""
    return "<br>".join(
        f'<a href="{_live_url(c.agent, c.unit)}">{escape(c.unit)}</a> · {len(c.pilots)} aircraft'
        + (f'<span class="muted"> ({", ".join(escape(p) for p in sorted(c.pilots.values(), key=str.lower))})</span>'
           if c.pilots else "")
        for c in a.carriers) or "–"


def live_index_page(agents) -> str:
    """Every server agent on the hub and its live view (hub.live.LiveBroker.status)."""
    if agents:
        rows = "".join(
            f'<tr class="{"" if a.status == "live" else "muted"}"><td><a href="{_live_url(a.agent)}">'
            f"{escape(a.agent)}</a></td><td>{_live_status(a)}</td><td>{_live_carriers(a)}</td></tr>"
            for a in agents)
        table = (f'<div class="scroll-x"><table class="board"><thead><tr><th>Server</th><th>Status</th>'
                 f"<th>Carriers</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        table = '<p class="sub">This hub has no server agents, so there\'s nothing to watch live.</p>'
    body = (f'<p class="sub"><a href="/">← Greenie board</a></p><h1>Live</h1>'
            f'<p class="sub">Watch the carriers on this hub\'s servers as it happens, from the LSO\'s view. A server '
            f"shows its carriers while its server agent is connected to a mission with one.</p>{table}"
            '<script>setTimeout(() => location.reload(), 15000);</script>')
    return _page("Live", f"<style>{LIVE_STYLE}</style>{body}")


def live_server_page(a) -> str:
    """One server agent's live view (hub.live.AgentStatus) when it isn't exactly one carrier to go straight to."""
    if a.carriers:
        what = (f'<div class="scroll-x"><table class="board"><thead><tr><th>Carriers</th></tr></thead>'
                f"<tbody><tr><td>{_live_carriers(a)}</td></tr></tbody></table></div>")
    else:
        what = f'<p class="sub">{_live_status(a)}. This page refreshes itself; a carrier shows here once one is live.</p>'
    body = (f'<p class="sub"><a href="/live">← Live</a> · <a href="/">Greenie board</a></p>'
            f"<h1>{escape(a.agent)}</h1>{what}"
            '<script>setTimeout(() => location.reload(), 15000);</script>')
    return _page(f"Live: {a.agent}", f"<style>{LIVE_STYLE}</style>{body}")


def live_page(agent: str, unit: str, voice: bool = False) -> str:
    """One carrier's live view (static/live.js draws it from /live/{agent}/{unit}/events)."""
    src = _live_url(agent, unit) + "/events"
    body = (f'<p class="sub"><a href="/live">← Live</a> · <a href="/">Greenie board</a></p>'
            f'<h1>{escape(unit)}</h1><p class="sub">Live from <a href="{_live_url(agent)}">{escape(agent)}</a> · '
            f'<span class="live-status" id="live-status">Connecting…</span></p>'
            f'<div class="replay" id="live" data-src="{escape(src)}" data-voice="{1 if voice else 0}">'
            f'<canvas aria-label="Live 3D view of the carrier"></canvas><div class="labels"></div>'
            + _scene_overlays("Waiting for the carrier…") + '</div>'
            '<div class="replay-bar"><span class="group" id="live-follow" aria-label="Follow"></span>'
            '<button type="button" id="live-sound" aria-pressed="true" hidden>Sound on</button></div>'
            '<div class="replay-bar"><span class="group" id="live-views"></span></div>'
            '<ul class="live-graded" id="live-graded" hidden></ul>'
            '<p class="sub" id="live-hint"></p>'
            '<p class="sub">1–7 pick the camera and the mouse wheel zooms. Auto follows the jet in the groove, else the '
            "one nearest the ship. About a second behind the server; the jets and ship are simple stand-in models.</p>"
            f'<script type="importmap">{_imports()}</script>'
            '<script type="module" src="/static/live.js"></script>')
    return _page(f"Live: {unit}", f"<style>{REPLAY_STYLE}{LIVE_STYLE}</style>{body}")


UPLOAD_SCRIPT = """
const form = document.getElementById('upload');
const tokenInput = form.elements.token;
try { tokenInput.value = localStorage.getItem('burble-token') || ''; } catch (e) {}
form.addEventListener('submit', (event) => {
  event.preventDefault();
  const status = document.getElementById('status');
  const bar = document.getElementById('bar');
  const data = new FormData();
  data.append('recording', form.elements.recording.files[0]);
  if (form.elements.debrief.files.length) data.append('debrief', form.elements.debrief.files[0]);
  if (form.elements.password && form.elements.password.value) data.append('password', form.elements.password.value);
  try { localStorage.setItem('burble-token', tokenInput.value); } catch (e) {}
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/v1/recordings');
  if (tokenInput.value.trim()) xhr.setRequestHeader('Authorization', 'Bearer ' + tokenInput.value.trim());
  bar.hidden = false;
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = e.loaded / e.total; };
  xhr.onload = () => {
    if (xhr.status === 202) { window.location = JSON.parse(xhr.responseText).page; return; }
    bar.hidden = true;
    let detail = xhr.responseText;
    try { detail = JSON.parse(xhr.responseText).detail; } catch (e) {}
    status.textContent = 'Upload failed (' + xhr.status + '): ' + detail;
  };
  xhr.onerror = () => { bar.hidden = true; status.textContent = 'Upload failed: network error'; };
  status.textContent = 'Uploading…';
  xhr.send(data);
});
"""


def upload_page(token_required: bool) -> str:
    me = CURRENT_PILOT.get()
    if token_required:
        token = ('<label>Upload token <input name="token" type="password" required autocomplete="off">'
                 '<span class="hint">The token of a source on this service (kept in this browser).</span></label>')
        intro = "Every carrier pass in it is graded and added to the board."
    else:
        if me:
            password = (f'<p class="sub">Signed in as {escape(me)}: your passes are imported without your '
                        "password.</p>")
        else:
            password = ('<label>Your pilot password (if you\'ve set one) <input name="password" type="password" '
                        'autocomplete="current-password"><span class="hint">Needed only if you set a password for '
                        'your pilot name; you can also give it after uploading, or <a href="/signin?next=/upload">'
                        "sign in</a> first.</span></label>")
        token = (password +
                 '<label>Upload token (optional) <input name="token" type="password" autocomplete="off">'
                 '<span class="hint">For a source on this service: imports every pilot\'s passes (kept in this '
                 "browser). Without one, only your own passes are imported: those of the pilot flying on the "
                 "PC that made the recording (a server's recording needs that server's token).</span></label>")
        intro = "Your carrier passes in it are graded and added to the board."
    body = (
        '<p class="sub"><a href="/">← Greenie board</a></p><h1>Upload a Tacview recording</h1>'
        f'<p class="sub">{intro} They\'re merged with the passes already here (a server\'s and a pilot\'s '
        "report of the same landing become one). From a multiplayer client's recording, which often has only "
        "your own jet, your approaches are matched with the server's report of each landing.</p>"
        f'<div class="panel"><form class="upload" id="upload">{token}'
        '<label>Tacview recording <input name="recording" type="file" accept=".acmi" required>'
        '<span class="hint">A .zip.acmi or .txt.acmi file.</span></label>'
        '<label>debrief.log (optional) <input name="debrief" type="file" accept=".log">'
        "<span class=\"hint\">DCS's Logs/debrief.log from the same session, for DCS's own grades and wires "
        "(DCS overwrites it at the next mission).</span></label>"
        '<button type="submit">Upload</button><progress id="bar" hidden value="0"></progress>'
        '<p id="status" class="error"></p></form></div>'
        f"<script>{UPLOAD_SCRIPT}</script>"
    )
    return _page("Upload a recording", body)


def upload_status_page(upload: Upload, key: str | None) -> str:
    """`key`: the uploader's own view (they may pick whose passes to import)."""
    running = upload.status in ("inspecting", "queued", "processing")
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    rows = []
    for r in upload.results or []:
        kind = "own track" if r.get("kind") == "track" else (r.get("outcome") or "")
        if r.get("error"):
            result = f'<span class="error">{escape(r["error"])}</span>'
        elif r.get("grade"):
            state = "added" if r.get("created") else "already here"
            result = (f'<a href="/passes/{r["pass_id"]}">{escape(grade_name(r["grade"]))}: {escape(r.get("text") or "")}</a>'
                      f" ({state})")
        else:
            result = escape(r.get("text") or "kept until a report of this landing with the carrier arrives")
        start = r.get("start_time")
        rows.append(f"<tr><td>{escape(r.get('pilot') or '?')}</td><td>{escape(kind)}</td>"
                    f"<td>{'' if start is None else f'{start:.0f}s'}</td><td>{result}</td></tr>")
    if upload.status == "choose_pilot" and key:
        options = "".join(f'<option value="{escape(n)}">{escape(n)}</option>' for n in upload.pilots or [])
        detail = (f'<form class="upload" method="post" action="/uploads/{upload.id}/pilot">'
                  f'<input type="hidden" name="key" value="{escape(key)}">'
                  '<label>This recording has more than one own pilot. Which are you? <select name="pilot" required>'
                  f'<option value="" disabled selected>Choose…</option>{options}</select>'
                  '<span class="hint">Only that pilot\'s passes are imported.</span></label>'
                  + ('<label>Password (if that pilot has set one, and isn\'t you) <input name="password" '
                     'type="password" autocomplete="current-password"><span class="hint">Not needed for your own '
                     f'name: you\'re signed in as {escape(CURRENT_PILOT.get())}.</span></label>' if CURRENT_PILOT.get() else
                     '<label>Password (if that pilot has set one) <input name="password" type="password" '
                     'autocomplete="current-password"></label>')
                  + '<button type="submit">Import my passes</button></form>')
    elif upload.status == "needs_password" and key:
        wrong = f'<p class="error">{escape(upload.message)}</p>' if upload.message else ""
        detail = (f'<form class="upload" method="post" action="/uploads/{upload.id}/password">{wrong}'
                  f'<input type="hidden" name="key" value="{escape(key)}">'
                  f'<label>{escape(upload.pilot or "This pilot")} has set a password. Enter it to import their passes '
                  '<input name="password" type="password" required autocomplete="current-password"></label>'
                  '<button type="submit">Import</button></form>')
    elif upload.status == "needs_password":
        detail = '<p class="empty-state">Waiting for the pilot\'s password.</p>'
    elif upload.status == "choose_pilot":
        detail = '<p class="empty-state">Waiting for the uploader to choose their pilot.</p>'
    elif upload.status == "failed":
        detail = f'<p class="error">{escape(upload.message or "failed")}</p>'
    elif running:
        doing = {"inspecting": "Reading the recording", "queued": "Waiting to be processed"}.get(upload.status, "Processing")
        detail = f'<p class="empty-state">{doing}… (this page refreshes)</p>'
    elif rows:
        detail = ('<div class="scroll"><table class="results"><thead><tr><th>Pilot</th><th>Pass</th><th>At</th>'
                  f'<th>Result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    else:
        detail = f'<p class="empty-state">{escape(upload.message or "No carrier passes or approaches found in this recording.")}</p>'
    body = (f'<p class="sub"><a href="/">← Greenie board</a> · <a href="/upload">Upload another</a></p>'
            f"<h1>{escape(upload.filename)}</h1>"
            f'<p class="sub">{upload.size / 1e6:.1f} MB · '
            f'{escape(f"passes flown by {upload.pilot}" if upload.pilot else f"from {upload.source.name}")} · '
            f'{escape(upload.status)}</p>'
            f'<div class="panel">{detail}</div>')
    return _page(f"Upload: {upload.filename}", body).replace("<head>", "<head>" + refresh, 1)


# Hovering a pass's grade square previews its trap card next to the pointer (not the overlay's lines).
CARD_PREVIEW_SCRIPT = """
(() => {
  const pop = document.getElementById('tc-pop');
  const cards = new Map();
  let current = null;
  const place = (e) => {
    const w = pop.offsetWidth, h = pop.offsetHeight, m = 16;
    let x = e.clientX + m, y = e.clientY + m;
    if (x + w > window.innerWidth - 8) x = Math.max(8, e.clientX - w - m);
    if (y + h > window.innerHeight - 8) y = Math.max(8, window.innerHeight - h - 8);
    pop.style.left = x + 'px'; pop.style.top = y + 'px';
  };
  const show = async (id, e) => {
    current = id;
    pop.hidden = false;
    if (!cards.has(id)) {
      pop.innerHTML = '<div class="loading">Loading trap card…</div>';
      place(e);
      cards.set(id, fetch('/passes/' + id + '/card.svg').then(r => r.ok ? r.text() : '<div class="loading">No trap card</div>'));
    }
    const svg = await cards.get(id);
    if (current !== id) return;
    pop.innerHTML = svg;
    place(e);
  };
  const hide = () => { current = null; pop.hidden = true; };
  document.querySelectorAll('[data-pass]').forEach((el) => {
    const id = el.getAttribute('data-pass');
    el.addEventListener('mouseenter', (e) => show(id, e));
    el.addEventListener('mousemove', (e) => { if (current === id) place(e); });
    el.addEventListener('mouseleave', hide);
  });
})();
"""

# Drag across the overlay to zoom into that stretch of the approach (redrawn by the server, so the scales
# and labels adapt); "Reset zoom" or a double-click goes back to the whole approach.
# Drag across a chart (the trap card, or the pilot's traps overlaid) to zoom into that stretch of the approach;
# double-click or the reset button to see it all again. Each `.zoomable` box has `data-src` (its SVG's URL,
# which takes `near`/`far`) and `data-reset` (its reset button's id); its SVG carries its current view.
ZOOM_SCRIPT = """
(() => {
  for (const box of document.querySelectorAll('.zoomable')) {
    const reset = document.getElementById(box.dataset.reset);
    const sel = document.createElement('div');
    sel.className = 'zoom-sel';
    sel.hidden = true;
    box.appendChild(sel);
    let drag = null, dragged = false;
    const along = (svg, clientX) => {
      const r = svg.getBoundingClientRect();
      const px = (clientX - r.left) * svg.viewBox.baseVal.width / r.width;
      const near = +svg.dataset.near, far = +svg.dataset.far;
      const f = Math.min(1, Math.max(0, (px - +svg.dataset.padL) / +svg.dataset.plotW));
      return far - f * (far - near);
    };
    const load = async (near, far) => {
      const url = new URL(box.dataset.src, location.href);
      if (near === null) {
        url.searchParams.delete('near');
        url.searchParams.delete('far');
      } else {
        url.searchParams.set('near', near.toFixed(1));
        url.searchParams.set('far', far.toFixed(1));
      }
      box.classList.add('loading');
      try {
        const r = await fetch(url);
        if (r.ok) {
          box.querySelector('svg').outerHTML = await r.text();
          if (reset) reset.hidden = near === null;
        }
      } finally {
        box.classList.remove('loading');
      }
    };
    box.addEventListener('mousedown', (e) => {
      const svg = box.querySelector('svg');
      if (e.button !== 0 || !svg) return;
      drag = { x: e.clientX, svg };
      dragged = false;
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!drag) return;
      if (Math.abs(e.clientX - drag.x) > 4) dragged = true;
      if (!dragged) return;
      const r = box.getBoundingClientRect();
      sel.hidden = false;
      sel.style.left = (Math.min(drag.x, e.clientX) - r.left) + 'px';
      sel.style.width = Math.abs(e.clientX - drag.x) + 'px';
    });
    window.addEventListener('mouseup', (e) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      sel.hidden = true;
      if (!dragged) return;
      const a = along(d.svg, d.x), b = along(d.svg, e.clientX);
      load(Math.min(a, b), Math.max(a, b));
    });
    // A drag that ends on a line isn't a click on it.
    box.addEventListener('click', (e) => { if (dragged) { e.preventDefault(); e.stopPropagation(); dragged = false; } }, true);
    box.addEventListener('dblclick', () => load(null, null));
    if (reset) reset.addEventListener('click', () => load(null, null));
  }
})();
"""

KIND_LABELS = {"fault": "Fault", "bias": "Leaning", "speed": "Speed", "outcome": "Outcome", "wires": "Wires",
               "trend": "Trend"}


def pilot_page(name: str, summary, passes: int, overlay_svg: str | None = None, overlay_src: str = "",
               servers: str = "all") -> str:
    """Meta grading: themes across the pilot's recent passes (`summary`: the hub's PilotSummary)."""
    result, rows = summary.trends, summary.rows
    first = summary.first_seen.strftime("%Y-%m-%d") if summary.first_seen else "?"
    last = summary.last_seen.strftime("%Y-%m-%d") if summary.last_seen else "?"
    heading = (f'<h1>{escape(name)}{f" <span class=\"modex\">#{escape(summary.modex)}</span>" if summary.modex else ""}'
               "</h1>")
    settings = f'<a href="/pilots/{quote(name, safe="")}/settings">Settings</a>'
    seen = (f'<p class="sub">{f"Last livery: {escape(summary.last_livery)} · " if summary.last_livery else ""}'
            f'{summary.landings} landing{"s" if summary.landings != 1 else ""} · '
            f"first seen {first} · last seen {last} · "
            f"{'uploads password-protected' if summary.protected else 'no upload password'} · {settings}</p>")
    options = "".join(f'<option value="{n}"{" selected" if n == passes else ""}>last {n}</option>'
                      for n in sorted({8, 12, 15, 20, passes}))
    chosen = summary.airframe or ""
    tabs = ""
    if summary.airframes:  # trends don't mix airframes: one at a time (a single tab says which it is)
        def tab(a: str, n: int) -> str:
            href = f"?passes={passes}&servers={servers}&aircraft={quote(a, safe='')}"
            current = ' class="current" aria-current="page"' if a == chosen else ""
            return f'<a href="{escape(href)}"{current}>{escape(a)} <span class="n">{n}</span></a>'
        tabs = '<nav class="airframes">' + "".join(tab(a, n) for a, n in summary.airframes) + "</nav>"
    picker = (f'<form class="filters" method="get"><input type="hidden" name="aircraft" value="{escape(chosen)}">'
              f'<label>Look at <select name="passes" onchange="this.form.submit()">'
              f"{options}</select> passes</label><label>Servers <select name=\"servers\" onchange=\"this.form.submit()\">"
              + "".join(f'<option value="{v}"{" selected" if v == servers else ""}>{label}</option>'
                        for v, label in (("ours", "This hub&#x27;s servers"), ("all", "All servers")))
              + "</select></label></form>")
    if not result.passes:
        body = (f'<p class="sub"><a href="/">← Greenie board</a></p>{heading}{seen}{tabs}{picker}'
                f'<p class="empty-state">No graded {escape(chosen + " " if chosen else "")}passes yet.</p>')
        return _page(name, body)
    grades = " · ".join(f"{escape(grade_short(g))} ×{n}" for g, n in sorted(result.grades.items(), key=lambda kv: -kv[1]))
    def items(themes, empty: str) -> str:
        return "".join(
            f'<li><span class="kind">{KIND_LABELS.get(t.kind, t.kind)}</span>'
            f'<span class="bar" title="{t.count} of {t.of}"><span style="width:{100 * t.share:.0f}%"></span></span>'
            f"<span>{escape(t.text)}</span></li>" for t in themes) or f"<li>{empty}</li>"

    analysis = items(result.analysis, "Nothing stands out: no fault or leaning repeats across these passes.")
    results = items(result.results, "No pattern in outcomes or wires across these passes.")
    strengths = ("<h2>Consistently good</h2><ul class=\"themes\">"
                 + "".join(f"<li>{escape(s)}</li>" for s in result.strengths) + "</ul>") if result.strengths else ""
    cells = "".join(
        f'<a class="cell {GRADE_CLASS.get(p.grade.grade, "")}" href="/passes/{p.id}" data-pass="{p.id}" '
        f'title="{escape(grade_name(p.grade.grade))}: {escape(p.grade.text)} · {_when(p.occurred_at)}">'
        f"{escape(grade_short(p.grade.grade))}</a>" for p in reversed(rows))
    body = (f'<p class="sub"><a href="/">← Greenie board</a></p>{heading}{seen}{tabs}'
            f'<p class="sub">Last {result.passes} {escape(chosen + " " if chosen else "")}pass{"es" if result.passes != 1 else ""} · '
            f'{result.average_points:.2f} points on average · {grades}</p>'
            f"{picker}<div class=\"panel\"><h2 style=\"margin-top:0\">Analysis</h2>"
            f'<ul class="themes">{analysis}</ul>{strengths}'
            f'<h2>Results</h2><ul class="themes">{results}</ul>'
            f'<h2>These passes (oldest → newest)</h2><div class="recent">{cells}</div></div>'
            + (f'<h2>Traps overlaid <button id="zoom-reset" class="zoom-reset" hidden>Reset zoom</button></h2>'
               f'<div class="panel card zoomable" id="overlay" data-src="{escape(overlay_src)}" data-reset="zoom-reset">'
               f'{overlay_svg}</div>'
               f"<script>{ZOOM_SCRIPT}</script>" if overlay_svg else "")
            + f'<div id="tc-pop" hidden></div><script>{CARD_PREVIEW_SCRIPT}</script>')
    return _page(f"{name}: recent passes", body)


def waiting_page(name: str) -> str:
    """For a signed-in pilot an admin hasn't approved yet, where only approved pilots may go."""
    body = (f'<p class="sub"><a href="/">← Greenie board</a></p><h1>Waiting for approval</h1>'
            f'<div class="panel"><p>An admin approves new pilots on this board, and hasn\'t approved {escape(name)} yet. '
            f'This opens once they do. <a href="/pilots/{quote(name, safe="")}/settings">Your settings</a></p></div>')
    return _page("Waiting for approval", body)


def join_page(error: str | None = None, name: str = "", existing: str | None = None, approval: bool = False) -> str:
    """Join the board before flying here: choose a name and a password (`approval`: an admin approves new pilots)."""
    note = f'<p class="error">{escape(error)}</p>' if error else ""
    if existing:
        note = (f'<p class="error">{escape(existing)} is already on this board. If that\'s you, '
                f'<a href="/pilots/{quote(existing, safe="")}/settings">set your password on your settings page</a>.</p>')
    form = (
        '<form class="upload" method="post" action="/join">'
        '<p class="sub">Choose the name you fly under in DCS and a password. With it you can create pilot tokens '
        "(to send your passes here from your own PC) and claim other names you fly under. Passes reported under "
        "this name count as yours.</p>"
        + ('<p class="sub">An admin approves new pilots on this board: until they do, you can sign in and see that '
           "you're waiting, nothing more.</p>" if approval else "") +
        f'<label>Pilot name <input name="name" required maxlength="100" value="{escape(name)}" autocomplete="username"></label>'
        '<label>Password <input name="password" type="password" required minlength="8" autocomplete="new-password">'
        '<span class="hint">At least 8 characters.</span></label>'
        '<label>Password again <input name="confirm" type="password" required minlength="8" autocomplete="new-password">'
        "</label><button type=\"submit\">Join</button></form>")
    body = ('<p class="sub"><a href="/">← Greenie board</a></p><h1>Join this board</h1>'
            f'{note}<div class="panel">{form}</div>')
    return _page("Join this board", body)


def signin_page(error: str | None = None, name: str = "", next_url: str = "", members_only: bool = False) -> str:
    note = f'<p class="error">{escape(error)}</p>' if error else ""
    if members_only:
        note = ('<p class="sub" style="color:var(--text)">This board is for its members: sign in to see it, or '
                '<a href="/join">join</a>.</p>' + note)
    form = (
        '<form class="upload" method="post" action="/signin">'
        f'<input type="hidden" name="next" value="{escape(next_url)}">'
        f'<label>Pilot name <input name="name" required maxlength="100" value="{escape(name)}" autocomplete="username">'
        '<span class="hint">The name you fly under (or one of your other names).</span></label>'
        '<label>Password <input name="password" type="password" required autocomplete="current-password"></label>'
        '<button type="submit">Sign in</button></form>')
    body = ('<h1>Sign in</h1><p class="sub">Stay signed in on this browser to change your settings and upload '
            f'your recordings without giving your password each time. New here? <a href="/join">Join this board</a>.</p>'
            f'{note}<div class="panel">{form}</div>'
            '<p class="sub" style="margin-top:12px">Forgot your password? Ask the server\'s admin to reset it.</p>')
    return _page("Sign in", body)


def pilot_settings_page(pilot, done: str | None, error: str | None, tokens: list | None = None,
                        aliases: list | None = None, new_token: str | None = None, signed_in: bool = False) -> str:
    """Set or change the pilot's password; signed in as them (`signed_in`), change their side number, create
    and revoke pilot tokens, and claim other in-game names. `new_token`: a token just created, shown this once."""
    name = pilot.name
    quoted = quote(name, safe="")
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    has = pilot.password_hash is not None
    back = f'<p class="sub"><a href="/pilots/{quoted}">← {escape(name)}</a></p><h1>{escape(name)}: settings</h1>'
    if has and not signed_in:
        me = CURRENT_PILOT.get()
        other = f" You're signed in as {escape(me)}." if me else ""
        body = (f'{back}{note}<div class="panel"><p>Sign in as {escape(name)} to change these settings.{other}</p>'
                f'<p><a href="/signin?next={quote(f"/pilots/{quoted}/settings", safe="")}">Sign in</a></p></div>'
                '<p class="sub" style="margin-top:12px">Forgot your password? Ask the server\'s admin to reset it.</p>')
        return _page(f"{name}: settings", body)
    password_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/password">'
        "<h2 style=\"margin-top:0\">" + ("Change password" if has else "Set a password") + "</h2>"
        + ("" if has else '<p class="sub">Nobody has set one for this pilot yet: setting it claims the name and signs '
           "you in. Recordings uploaded without a token then need it to import this pilot's passes.</p>")
        + ('<label>Current password <input name="current" type="password" required autocomplete="current-password">'
           '<span class="hint">Changing it signs you out everywhere else.</span></label>' if has else "")
        + '<label>New password <input name="new" type="password" required minlength="8" autocomplete="new-password">'
        '<span class="hint">At least 8 characters.</span></label>'
        '<label>New password again <input name="confirm" type="password" required minlength="8" '
        'autocomplete="new-password"></label><button type="submit">Save password</button></form>')
    if not has:  # nothing else until the name is claimed
        body = f'{back}{note}<div class="panel">{password_form}</div>'
        return _page(f"{name}: settings", body)
    if pilot.pending_since is not None:  # nothing else until an admin approves them
        waiting = ('<div class="panel"><h2 style="margin-top:0">Waiting for approval</h2><p>An admin approves new '
                   f"pilots on this board, and hasn't approved {escape(name)} yet (asked {escape(_when(pilot.pending_since))}). "
                   "Until then you can't create pilot tokens, claim other names, or change your side number, and "
                   "your password doesn't protect uploads of your passes yet. Your passes still reach the board.</p></div>")
        body = f'{back}{note}{waiting}<div class="panel" style="margin-top:12px">{password_form}</div>'
        return _page(f"{name}: settings", body)
    modex_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/modex">'
        '<h2>Side number</h2>'
        f'<p class="sub">Now: {escape(pilot.modex or "not known yet")}. It\'s taken from your first pass that has one; '
        "you can change it here.</p>"
        '<label>Side number <input name="modex" required pattern="[0-9]{1,4}" inputmode="numeric"></label>'
        '<button type="submit">Save side number</button></form>')
    shown = (f'<div class="token-once"><p><strong>Your new pilot token</strong> (copy it now: it won\'t be shown again)</p>'
             f'<code>{escape(new_token)}</code></div>' if new_token else "")
    rows = "".join(
        f'<tr><td>{escape(t.label or t.name)}</td><td>{_when(t.created_at)}</td>'
        f'<td>{_when(t.last_used_at) if t.last_used_at else "never"}</td><td>'
        + ("revoked" if t.revoked_at else
           f'<form method="post" action="/pilots/{quoted}/settings/tokens/{t.id}/revoke" class="inline">'
           '<button type="submit">Revoke</button></form>')
        + "</td></tr>" for t in tokens or [])
    listed = (f'<div class="scroll"><table class="results"><thead><tr><th>Token</th><th>Created</th><th>Last used</th>'
              f'<th></th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else "")
    tokens_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/tokens"><h2>Pilot tokens</h2>'
        '<p class="sub">A pilot token lets the pilot hook or pilot uploader send your passes here. Everything sent '
        "with it is credited to you, whatever name you fly under.</p>" + shown + listed
        + '<label>What it\'s for <input name="label" maxlength="100" placeholder="e.g. my PC"></label>'
        '<button type="submit">Create a pilot token</button></form>')
    names = "".join(f'<li>{escape(a.name)}{"" if a.claimed else " (seen with your pilot token)"}</li>'
                    for a in aliases or [])
    aliases_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/aliases"><h2>Other names</h2>'
        '<p class="sub">Other in-game names you fly under (e.g. with a squadron tag). Passes reported under them '
        "count as yours.</p>"
        + (f'<ul class="themes">{names}</ul>' if names else '<p class="sub">None yet.</p>')
        + '<label>Claim a name <input name="alias" required maxlength="100"></label>'
        '<span class="hint">Only a name nobody else has claimed. Its passes on this board move to you.</span>'
        '<button type="submit">Claim name</button></form>')
    body = (f'{back}{note}<div class="panel">{password_form}</div><div class="panel" style="margin-top:12px">{modex_form}</div>'
            f'<div class="panel" style="margin-top:12px">{tokens_form}</div>'
            f'<div class="panel" style="margin-top:12px">{aliases_form}</div>')
    return _page(f"{name}: settings", body)


def admin_page(pilots: list, agents: list, q: str = "", done: str | None = None, error: str | None = None,
               new_token: tuple[str, str] | None = None, css: str = "", telemetry: dict | None = None,
               pending: list | None = None, approval: bool = False, members_only: bool = False) -> str:
    """For admins (pilots with the role, signed in): pilots (admin role, password reset, removal, other names,
    pilot tokens) and server agents (adding one, their configuration). `new_token`: (what, token), shown once."""
    me = CURRENT_PILOT.get()
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    if new_token:
        note += (f'<div class="token-once"><p><strong>The {escape(new_token[0])}</strong> (copy it now: it won\'t be '
                 f'shown again)</p><code>{escape(new_token[1])}</code></div>')
    keep = f'<input type="hidden" name="q" value="{escape(q)}">'

    def post(action: str, label: str, confirm: str = "", danger: bool = False, extra: str = "") -> str:
        ask = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
        cls = ' class="danger"' if danger else ""
        return (f'<form method="post" action="{escape(action)}"{ask}>{keep}{extra}'
                f'<button type="submit"{cls}>{label}</button></form>')

    rows = []
    for p in pilots:
        quoted = quote(p.name, safe="")
        base = f"/admin/pilots/{quoted}"
        acts = []
        if p.admin:
            if p.name != me:
                acts.append(post(f"{base}/admin", "Remove admin", extra='<input type="hidden" name="admin" value="0">'))
        elif p.has_password and not p.pending:
            acts.append(post(f"{base}/admin", "Make admin", "Make this pilot an admin?",
                             extra='<input type="hidden" name="admin" value="1">'))
        if p.has_password and not p.pending and p.name != me:
            acts.append(post(f"{base}/reset-password", "Reset password",
                             "Clear this pilot\\'s password? They (or anyone) can then set a new one.", danger=True))
        if not p.admin:
            lost = (f" and their {p.passes} landing{'s' if p.passes != 1 else ''} (gone from the board for good)"
                    if p.passes else "")
            acts.append(post(f"{base}/remove", "Delete",
                             f"Delete {_js_text(p.name)}{lost}? Their pilot tokens are revoked. "
                             "If they fly here again, they come back as a new pilot.", danger=True))
        if not p.pending:  # approve them first (Approving new pilots, above)
            acts.append(post(f"{base}/tokens", "New pilot token",
                             extra='<input class="label" name="label" maxlength="100" placeholder="for (e.g. PC)">'))
        names = "".join(
            f'<span class="alias">{escape(a)}'
            + post("/admin/aliases/remove", "×", "Remove this alias? Its passes go back to a pilot of that name.",
                   extra=f'<input type="hidden" name="alias" value="{escape(a)}">') + "</span>" for a in p.aliases)
        rows.append(
            f'<tr><td><a href="/pilots/{quoted}">{escape(p.name)}</a>'
            + (' <span class="tag">admin</span>' if p.admin else "")
            + (' <span class="tag">waiting</span>' if p.pending else "") + "</td>"
            f"<td>{p.passes}</td><td>{'set' if p.has_password else 'none'}</td>"
            f'<td>{names or "<span class=sub>none</span>"}</td><td><div class="actions">{"".join(acts)}</div></td></tr>')
    pilots_table = (
        '<div class="scroll"><table class="results"><thead><tr><th>Pilot</th><th>Passes</th><th>Password</th>'
        f'<th>Other names</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows else '<p class="sub">No pilots' + (" match." if q else " yet.") + "</p>")
    search = ('<form method="get" action="/admin" class="actions" style="margin-bottom:8px">'
              f'<input name="q" value="{escape(q)}" placeholder="Pilot or other name"><button type="submit">Find</button>'
              + ('<a href="/admin">Show all</a>' if q else "") + "</form>")
    agent_rows = "".join(
        f'<tr><td><a href="/admin/agents/{quote(a.name, safe="")}">{escape(a.name)}</a></td><td>{_when(a.created_at)}</td>'
        f'<td>{_when(a.last_used_at) if a.last_used_at else "never"}</td>'
        f'<td><a href="/admin/agents/{quote(a.name, safe="")}">Settings</a></td></tr>'
        for a in agents)
    agents_table = (
        '<div class="scroll"><table class="results"><thead><tr><th>Server agent</th><th>Added</th><th>Last used</th>'
        f'<th></th></tr></thead><tbody>{agent_rows}</tbody></table></div>' if agent_rows
        else '<p class="sub">None yet.</p>')
    add_agent = ('<form method="post" action="/admin/agents" class="actions" style="margin-top:8px">'
                 '<input name="name" required maxlength="100" placeholder="e.g. server1">'
                 '<button type="submit">Add a server agent</button></form>'
                 '<p class="sub">Its server agent token is shown once, here.</p>')
    tel = telemetry or {}
    usage_link = ' · <a href="/admin/usage">Usage</a>' if tel.get("view") else ""
    body = ('<h1>Admin</h1><p class="sub">Regrading and the wire check are run on the hub\'s machine '
            f"(<code>burble hub regrade</code>, <code>burble hub wire-check</code>).{usage_link}</p>"
            f'{note}{_telemetry_notice(tel)}<div class="admin">{_joining(pending or [], approval, post)}{_board_access(members_only, approval, post)}'
            f'<div class="panel"><h2 style="margin-top:0">Pilots</h2>{search}{pilots_table}'
            '<p class="sub">Only a pilot with a password can be an admin; resetting an admin\'s password removes the '
            "role. Deleting a pilot deletes their landings too; an admin can't be deleted (remove the role first).</p></div>"
            f'<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">Server agents</h2>{agents_table}'
            f"{add_agent}</div>{_appearance(css)}{_telemetry_panel(tel) if telemetry else ''}</div>")
    return _page("Admin", body)


def agent_settings_page(agent, done: str | None = None, error: str | None = None,
                        form: dict[str, list[str]] | None = None) -> str:
    """A server agent's settings (PLAN #43): the common ones as a form (agentconfig.SECTIONS), its carriers'
    frequencies, which calls it makes, and its call thresholds; the raw JSON below for anything else. `form`: what
    was just posted (shown again after an error, so nothing typed is lost)."""
    from .. import agentconfig as ac

    config = agent.config if isinstance(agent.config, dict) else {}
    info = agent.agent_info or {}
    typed = form is not None and "raw" not in form
    name = quote(agent.name, safe="")

    def typed_value(key: str) -> str:
        return ((form or {}).get(key) or [""])[-1]

    def shown(value: object) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value)

    def field(setting) -> str:
        key = ac.field_name(setting)
        current = ac.get(config, setting.path)
        hint = f'<span class="hint">{escape(setting.help)}</span>' if setting.help else ""
        if setting.kind == "bool":
            on = key in (form or {}) if typed else (current if isinstance(current, bool) else setting.default)
            control = f'<input type="checkbox" name="{key}" value="1"{" checked" if on else ""}>'
        elif setting.kind == "choice" or setting.path == ("callouts", "voice") and info:
            choices = list(setting.choices)
            if setting.path == ("callouts", "voice"):
                voices = info.get("voices") or []
                choices = [("", "First by name")] + [(v, v) for v in voices] + (
                    [(EACH_CARRIER, "A different one for each carrier")] if len(voices) > 1 else [])
            value = typed_value(key) if typed else shown(current if current is not None else setting.default)
            if setting.path[-1] == "case" and value.upper() in ("I", "III"):
                value = value.upper()
            if value not in [str(c) for c, _ in choices]:
                choices.append((value, f"{value} (not on the agent)" if setting.path[-1] == "voice" else value))
            control = f'<select name="{key}">' + "".join(
                f'<option value="{escape(str(c))}"{" selected" if str(c) == value else ""}>{escape(label)}</option>'
                for c, label in choices) + "</select>"
        else:
            value = typed_value(key) if typed else shown(current)
            kind = ' type="number" step="any"' if setting.kind in ("int", "number") else ""
            default = f"{setting.default:g}" if isinstance(setting.default, float) else setting.default
            placeholder = "" if default is None else f' placeholder="{escape(str(default))}"'
            control = f'<input name="{key}"{kind} value="{escape(value)}"{placeholder}>'
        return f'<label class="setting"><span>{escape(setting.label)}</span>{control}{hint}</label>'

    sections = []
    for title, group in ac.SECTIONS:
        sections.append(f"<h2>{escape(title)}</h2>" + "".join(field(st) for st in group))
        if title == "Live calls":
            sections.append(_carrier_rows(config, form if typed else None, info))
        if title == "Listening and recording":
            if (ac.get(config, ("callouts", "listen")) is True and info and not info.get("listen_model")):
                sections.append('<p class="warn">Listening is on, but this agent has no listen_model, '
                                "so it can't hear anyone.</p>")

    calls = ac.get(config, ("callouts", "calls"))
    chosen = set(form.get("call") or []) if typed else (set(calls) if isinstance(calls, list) else None)
    groups = "".join(
        f"<fieldset><legend>{escape(title)}</legend>" + "".join(
            f'<label><input type="checkbox" name="call" value="{escape(c.value)}"'
            f'{" checked" if chosen is None or c.value in chosen else ""}> {escape(ac.call_label(c))}'
            + (f' <span class="note">({escape(ac.CALL_NOTES[c])})</span>' if c in ac.CALL_NOTES else "") + "</label>"
            for c in group) + "</fieldset>" for title, group in ac.CALL_GROUPS)

    set_thresholds = ac.get(config, ("callouts", "thresholds"))
    set_thresholds = set_thresholds if isinstance(set_thresholds, dict) else {}
    rows = []
    for t in ac.thresholds():
        key = f"threshold.{t.name}"
        value = typed_value(key) if typed else shown(set_thresholds.get(t.name))
        rows.append(f'<tr><td><code>{t.name}</code></td><td><input name="{key}" type="number" step="any" '
                    f'value="{escape(value)}" placeholder="{t.default:g}" aria-label="{t.name}"></td>'
                    f'<td class="default">{escape(ac.show_number(t.default, t.unit))}</td>'
                    f'<td class="note">{escape(t.note)}</td></tr>')
    changed = any(t.name in set_thresholds for t in ac.thresholds())
    thresholds = (f'<details{" open" if changed else ""}><summary><strong>Call thresholds</strong> (advanced; '
                  f"{sum(t.name in set_thresholds for t in ac.thresholds())} changed)</summary>"
                  '<p class="sub">Leave one empty for its default. Distances are in metres (1 nm = 1852 m), angles in '
                  "degrees. The AOA band is each aircraft's own.</p>"
                  '<div class="scroll"><table class="results"><thead><tr><th>Threshold</th><th>Value</th><th>Default</th>'
                  f'<th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')

    if info:
        voices = info.get("voices") or []
        about = (f"Agent version {escape(info.get('version') or 'unknown')}; "
                 + (f"one clip set ({escape(voices[0])}), used whatever the voice says" if info.get("one_voice") and voices
                    else f"clip sets: {escape(', '.join(voices))}" if voices else "no voice clips (it can't make calls)")
                 + f"; listening model: {'yes' if info.get('listen_model') else 'no'}. "
                 f"(Reported {escape(str(info.get('at', ''))[:16].replace('T', ' '))} UTC.)")
    else:
        about = ("This agent hasn't said which voices it has yet: it does when it next fetches its settings (agents "
                 "older than this hub don't). Type the voice's folder name meanwhile.")
    raw_value = typed_value("raw") if form is not None and "raw" in form else (
        json.dumps(config, indent=2) if config else "")
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    body = (f'<p><a href="/admin">← Admin</a></p><h1>{escape(agent.name)}: settings</h1>'
            '<p class="sub">Its server agent fetches these from the hub and applies them when the next mission starts. '
            f"{about}</p>{note}"
            f'<div class="admin settings"><form class="panel" method="post" action="/admin/agents/{name}/settings">'
            + "".join(sections)
            + "<h2>Calls</h2><p class=\"sub\">Which calls the LSO makes.</p>" + groups
            + f'<h2>Thresholds</h2>{thresholds}<p><button type="submit">Save settings</button></p></form>'
            f'<div class="panel" style="margin-top:12px"><details{" open" if form is not None and "raw" in form else ""}>'
            '<summary><strong>Raw configuration (JSON)</strong></summary>'
            '<p class="sub">Everything the agent is sent, including keys the form doesn\'t show (kept when the form '
            "is saved). Checked before it's saved. Every key is listed by <code>burble hub config-reference</code>.</p>"
            f'<form method="post" action="/admin/agents/{name}/config">'
            f'<textarea name="config" spellcheck="false" rows="16">{escape(raw_value)}</textarea>'
            '<button type="submit">Save configuration</button></form></details></div></div>')
    return _page(f"{agent.name}: settings", body)


def _carrier_rows(config: dict, form: dict[str, list[str]] | None, info: dict | None = None) -> str:
    """Per-carrier frequencies and voices (callouts.carriers), with two empty rows to add more. `info`: what the agent
    said about itself (its clip sets, for the voice)."""
    from .. import agentconfig as ac

    keys = ("unit", "frequency_mhz", "modulation", "voice")
    if form is not None:
        n = int((form.get("carriers") or ["0"])[-1] or 0)
        rows = [tuple((form.get(f"carrier.{i}.{k}") or [""])[-1] for k in keys) for i in range(n)]
        rows = [r for r in rows if r[0] or r[1] or r[3]]
    else:
        carriers = ac.get(config, ("callouts", "carriers"))
        rows = [(unit, f"{radio.get('frequency_mhz', '')}", str(radio.get("modulation", "AM")), str(radio.get("voice", "")))
                for unit, radio in (carriers.items() if isinstance(carriers, dict) else []) if isinstance(radio, dict)]
    rows += [("", "", "AM", "")] * 2
    voices = (info or {}).get("voices") or []

    def voice_cell(i: int, voice: str) -> str:
        if not voices or (info or {}).get("one_voice"):
            return (f'<input name="carrier.{i}.voice" value="{escape(voice)}" placeholder="the LSO\'s" '
                    'aria-label="Voice (clip set)">')
        choices = [("", "The LSO's voice")] + [(v, v) for v in voices]
        if voice not in [c for c, _ in choices]:
            choices.append((voice, f"{voice} (not on the agent)"))
        return f'<select name="carrier.{i}.voice" aria-label="Voice">' + "".join(
            f'<option value="{escape(c)}"{" selected" if c == voice else ""}>{escape(label)}</option>'
            for c, label in choices) + "</select>"

    cells = "".join(
        f'<tr><td><input class="unit" name="carrier.{i}.unit" value="{escape(unit)}" placeholder="e.g. CVN-75 Truman" '
        f'aria-label="Carrier unit name"></td><td><input name="carrier.{i}.frequency_mhz" type="number" step="any" '
        f'value="{escape(mhz)}" placeholder="MHz" aria-label="Frequency (MHz)"></td>'
        f'<td><select name="carrier.{i}.modulation" aria-label="Modulation">' + "".join(
            f'<option{" selected" if m == mod else ""}>{m}</option>' for m, _ in ac.MODULATIONS) + "</select></td>"
        f"<td>{voice_cell(i, voice)}</td></tr>"
        for i, (unit, mhz, mod, voice) in enumerate(rows))
    return ('<p style="margin:12px 0 4px"><strong>Carriers</strong></p><p class="sub" style="margin:0">'
            "By the carrier's unit name in the mission. A carrier listed with a frequency uses it; one without uses "
            "the frequency set for it in the mission, else the default above. A carrier with a voice speaks with it, "
            "whatever the voice above says. Clear a name to remove it.</p>"
            f'<input type="hidden" name="carriers" value="{len(rows)}">'
            '<div class="scroll carriers"><table class="results"><thead><tr><th>Carrier</th><th>MHz</th><th>Modulation'
            f"</th><th>Voice</th></tr></thead><tbody>{cells}</tbody></table></div>")


def _board_access(members_only: bool, approval: bool, post) -> str:
    """Who can see the board: anyone, or members only (signed-in, approved pilots)."""
    toggle = post("/admin/members-only", "Open it to everyone" if members_only else "Make it members-only",
                  extra=f'<input type="hidden" name="on" value="{0 if members_only else 1}">')
    if members_only:
        state = ("<strong>Members only:</strong> the board, passes, pilots' pages, replays, and the read API need a "
                 "signed-in pilot an admin has approved (the API also takes a token). Signing in, joining, uploads, and "
                 "Discord posts work as before; links in Discord posts lead to signing in.")
        if not approval:
            state += " Anyone can join, though: turn on approving new pilots to choose who gets in."
    else:
        state = "<strong>Open:</strong> anyone can see the board, passes, and pilots' pages."
    return (f'<div class="panel" style="margin-bottom:12px"><h2 style="margin-top:0">Who can see the board</h2>'
            f'<div class="actions"><p class="sub" style="margin:0">{state}</p>{toggle}</div></div>')


def _js_text(text: str) -> str:
    """Text for inside a single-quoted JavaScript string in an HTML attribute (e.g. a confirm() prompt)."""
    js = text.replace("\\", "\\\\").replace("'", "\\'")
    return escape(js, quote=False).replace('"', "&quot;")


def _joining(pending: list, approval: bool, post) -> str:
    """Approving new pilots: on or off, and those waiting (hub.service.PendingPilot) with Approve and Refuse."""
    toggle = post("/admin/join-approval", "Turn off" if approval else "Turn on",
                  extra=f'<input type="hidden" name="on" value="{0 if approval else 1}">')
    state = ("<strong>On:</strong> a pilot who joins, or sets a password on a name already on the board, waits for an "
             "admin here. Until then they can sign in and see that they're waiting, nothing more."
             if approval else "<strong>Off:</strong> anyone can join and use their account straight away.")
    rows = "".join(
        f'<tr><td><a href="/pilots/{quote(p.name, safe="")}">{escape(p.name)}</a></td><td>{escape(_when(p.since))}</td>'
        f"<td>{p.passes}</td><td><div class=\"actions\">"
        + post(f"/admin/pilots/{quote(p.name, safe='')}/approve", "Approve")
        + post(f"/admin/pilots/{quote(p.name, safe='')}/refuse", "Refuse",
               "Refuse this pilot? The name is free again: removed if it has no passes, else back to unclaimed.",
               danger=True)
        + "</div></td></tr>" for p in pending)
    waiting = ('<div class="scroll"><table class="results"><thead><tr><th>Waiting</th><th>Asked</th><th>Passes</th>'
               f"<th></th></tr></thead><tbody>{rows}</tbody></table></div>" if rows else "")
    after = ('<p class="sub">Turning it off doesn\'t approve those waiting.</p>' if rows and approval else "")
    return (f'<div class="panel" style="margin-bottom:12px"><h2 style="margin-top:0">Approving new pilots</h2>'
            f'{waiting}<div class="actions"><p class="sub" style="margin:0">{state}</p>{toggle}</div>{after}</div>')


_OPEN_SENT = """onclick="document.getElementById('whats-sent').showModal()\""""


def _telemetry_notice(tel: dict) -> str:
    """Until an admin has seen it: that usage reports are on, with what's sent and how to turn them off."""
    if not tel or not tel.get("enabled") or tel.get("acknowledged"):
        return ""
    return ('<div class="notice admin"><strong>This hub sends anonymous usage counts to the burble project once a '
            'day.</strong> Counts only: no pilot names, UCIDs, IP addresses, or tracks. '
            f'<button type="button" class="link" {_OPEN_SENT}>What\'s sent</button><div class="actions">'
            '<form method="post" action="/admin/telemetry"><input type="hidden" name="on" value="1">'
            '<button type="submit">Keep sending</button></form>'
            '<form method="post" action="/admin/telemetry"><input type="hidden" name="on" value="0">'
            '<button type="submit">Turn off</button></form></div></div>')


def _telemetry_panel(tel: dict) -> str:
    """Usage reports: on/off, where they go (and how each last went), the report as sent now, and what is and isn't
    in it; and connecting a receiver for the Usage page."""
    from .telemetry import NEVER_SENT, WHAT_IS_SENT

    on = tel["enabled"]
    toggle = ('<form method="post" action="/admin/telemetry">'
              f'<input type="hidden" name="on" value="{"0" if on else "1"}">'
              f'<button type="submit"{" class=danger" if on else ""}>{"Turn off" if on else "Turn on"}</button></form>')
    rows = []
    for url in tel["urls"]:
        st = tel["status"].get(url)
        when = (_when(datetime.fromisoformat(st["at"])) + (": sent" if st["ok"] else f": {st['message']}")
                if st else "not sent yet")
        rows.append(f"<li><code>{escape(url)}</code> <span class=\"sub\">({escape(when)})</span></li>")
    where = f"<ul>{''.join(rows)}</ul>" if rows else '<p class="sub">No addresses: nothing is sent.</p>'
    is_default = tel["urls"] == tel["default"]
    urls_form = ('<details><summary>Change where reports go</summary>'
                 '<form method="post" action="/admin/telemetry/urls">'
                 f'<textarea name="urls" spellcheck="false" style="min-height:4em">{escape(chr(10).join(tel["urls"]))}'
                 '</textarea><p class="sub">One address per line: a usage receiver (<code>burble telemetry serve</code>), '
                 'e.g. your community\'s own beside the project\'s. The report goes to each.</p>'
                 '<div class="actions"><button type="submit">Save addresses</button>'
                 + ("" if is_default else '<button type="submit" name="default" value="1">Back to the project\'s</button>')
                 + "</div></form></details>")
    view = tel.get("view")
    view_form = (f'<p>Showing stats from <code>{escape(view)}</code> on the <a href="/admin/usage">Usage page</a>.</p>'
                 '<form method="post" action="/admin/telemetry/view"><button type="submit" name="clear" value="1">'
                 "Disconnect</button></form>" if view else
                 '<p class="sub">Connect a usage receiver to see the stats it has gathered on a Usage page here. '
                 "Its API key comes from <code>burble telemetry new-key NAME</code> on the receiver's machine "
                 "(or use its password).</p>"
                 '<form method="post" action="/admin/telemetry/view" class="actions">'
                 '<input name="url" required placeholder="https://telemetry.example.com" style="width:18em">'
                 '<input name="key" type="password" required placeholder="API key or password" autocomplete="off">'
                 '<button type="submit">Connect</button></form>')
    sent = "".join(f"<li>{escape(x)}</li>" for x in WHAT_IS_SENT)
    never = "".join(f"<li>{escape(x)}</li>" for x in NEVER_SENT)
    state = ("<strong>On</strong>: once a day, to the addresses below." if on else
             "<strong>Off</strong>: nothing is sent.")
    return ('<div class="panel" id="usage-reports" style="margin-top:12px"><h2 style="margin-top:0">Usage reports</h2>'
            f'<p>{state} They help the burble project see how many hubs run it and how they\'re used.</p>'
            f'<div class="actions">{toggle}<button type="button" {_OPEN_SENT}>What\'s sent</button></div>'
            '<dialog class="modal" id="whats-sent"><h2 style="margin-top:0">What\'s sent</h2>'
            f"<p class=\"sub\" style=\"margin:0\">All of it about the last 30 days.</p><ul>{sent}</ul>"
            f"<h3>What's never sent</h3><ul>{never}</ul>"
            f'<details><summary>The report as it would be sent now</summary><pre>{escape(tel["report"])}</pre></details>'
            '<form method="dialog" class="actions" style="margin-top:8px"><button>Close</button></form></dialog>'
            f"<h3>Where it goes</h3>{where}{urls_form}"
            f"<h3>Usage page</h3>{view_form}</div>")


def usage_page(stats: dict | None, view: str | None, error: str | None) -> str:
    """Admins' Usage page: the stats of the receiver they connected (fetched from its /api/v1/usage)."""
    if view is None:
        content = ('<div class="panel"><p>No usage receiver connected. Connect one under "Usage reports" on the '
                   '<a href="/admin#usage-reports">admin page</a>.</p></div>')
    elif error:
        content = (f'<div class="panel"><p class="error">{escape(error)}</p><p>Check the address and key under '
                   '"Usage reports" on the <a href="/admin#usage-reports">admin page</a>.</p></div>')
    else:
        content = f'<div class="panel">{usage_body(stats)}</div>'
    source = f'<p class="sub">From <code>{escape(view)}</code> · <a href="/admin">Admin</a></p>' if view else ""
    return _page("Usage", f"<style>{USAGE_STYLE}</style><h1>Usage</h1>{source}{content}")


def _appearance(css: str) -> str:
    """The admins' custom CSS box, with the theme variables it can set."""
    names = ", ".join(f"<code>{v}</code>" for v in [*COLOR_VARS.values(), FONT_VAR, *GRADE_VARS.values()])
    return ('<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">Appearance</h2>'
            '<p class="sub">CSS added to every page of this board. The Discord images (the greenie board, and the '
            "colour beside each landing's post) can't use CSS rules, so they take only the theme variables set on "
            f"<code>:root</code>: {names}. Discord's images are dark, so values set for dark mode "
            "(<code>@media (prefers-color-scheme: dark)</code>) win there. Colours as #hex, rgb(), or names.</p>"
            '<form method="post" action="/admin/appearance">'
            f'<textarea name="css" spellcheck="false" maxlength="{MAX_CSS}" '
            'placeholder=":root { --bg: #101820; --card: #18242f; --grade-ok: #00c853; }">'
            f'{escape(css)}</textarea><button type="submit">Save CSS</button></form></div>')


def not_admin_page(pilot: str | None) -> str:
    """/admin for someone who isn't an admin (or isn't signed in)."""
    if pilot:
        text = (f"You're signed in as <strong>{escape(pilot)}</strong>, who isn't an admin on this board. "
                "If you should be, ask one of its admins to give you the role.")
        action = f'<a href="/pilots/{quote(pilot, safe="")}">Your passes</a> · <a href="/">Greenie board</a>'
    else:
        text = "This page is for the board's admins. Sign in as a pilot who's an admin to use it."
        action = f'<a href="/signin?next={quote("/admin", safe="")}">Sign in</a> · <a href="/">Greenie board</a>'
    body = f'<h1>Admins only</h1><div class="panel"><p>{text}</p><p>{action}</p></div>'
    return _page("Admins only", body)
