"""HTML pages for the central service: the greenie board and per-pass pages."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape
from urllib.parse import quote

from ..cards.overlay import GRADE_COLORS
from ..grading import grade_name, grade_short
from .db import Pass, Upload

# Greenie board colours per grade (light, dark), shared with the overlay card.

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
.cell.night { position: relative; }
.cell.night::after, .legend span.night-dot { content: ""; position: absolute; top: 4px; right: 4px; width: 7px; height: 7px;
        border-radius: 50%; background: #111; box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.6); }
.legend span.night-dot { position: relative; display: inline-block; top: 0; right: 0; vertical-align: 0; margin-right: 6px; }
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
h1 .modex { color: var(--muted); font-weight: 600; margin-left: 6px; }
#tc-pop { position: fixed; z-index: 10; width: min(640px, 92vw); pointer-events: none; background: var(--card);
  border: 1px solid var(--border); border-radius: 12px; padding: 6px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25); }
#tc-pop svg { display: block; width: 100%; height: auto; }
#tc-pop .loading { color: var(--muted); padding: 24px; text-align: center; }
#overlay { position: relative; cursor: crosshair; user-select: none; }
#overlay.loading svg { opacity: 0.5; }
.zoom-sel { position: absolute; top: 0; bottom: 0; background: rgba(9, 105, 218, 0.12);
  border-left: 1px solid #0969da; border-right: 1px solid #0969da; pointer-events: none; }
.zoom-reset { font: inherit; font-size: 12px; font-weight: 400; margin-left: 8px; padding: 2px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; vertical-align: 2px; }
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
            night = " · night" if p.night else ""
            title = f"{grade_name(g)}: {p.grade.text if p.grade else ''}{night} · {_when(p.occurred_at)} · {p.mission or ''}"
            cells.append(f'<td><a class="cell {GRADE_CLASS.get(g, "")}{" night" if p.night else ""}" href="/passes/{p.id}" '
                         f'title="{escape(title)}">{escape(grade_short(g))}</a></td>')
        cells += ['<td><span class="cell empty"></span></td>'] * (columns - len(shown))
        rows.append(f'<tr><td class="pilot"><a href="/pilots/{quote(name, safe="")}" title="Themes across recent passes">'
                    f'{escape(name)}</a></td><td class="num">{len(items)}</td>'
                    f'<td class="num">{avg:.2f}</td><td class="num">{100 * traps / len(items):.0f}%</td>'
                    + "".join(cells) + "</tr>")

    legend = '<div class="legend">' + "".join(
        f'<span><span class="swatch {GRADE_CLASS[g]}"></span>{escape(grade_short(g))} {grade_name(g)}'
        f'{f" ({escape(g)})" if grade_short(g) != g else ""}</span>' for g in GRADE_COLORS
    ) + '<span><span class="night-dot"></span>Night pass</span></div>'
    period = f"last {days} days" if days else "all time"
    if rows:
        table = (f'<div class="scroll"><table class="board"><thead><tr><th>Pilot</th><th>Passes</th><th>Avg</th>'
                 f'<th>Traps</th><th colspan="{columns}">Passes (oldest → newest)</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        table = '<p class="empty-state">No passes yet for this filter.</p>'
    body = (f"<h1>Greenie Board</h1><p class=\"sub\">{len(passes)} passes · {period} · "
            '<a href="/upload">Upload a Tacview recording</a></p>'
            f'{filters}<div class="panel">{table}{legend}</div>')
    return _page("Greenie Board", body)


def card_title(p: Pass) -> str:
    """The trap card's title line: the mission, the pilot's side number and the livery, when known."""
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


def pass_page(p: Pass, card_svg: str | None, error: str | None = None, reports: list[Pass] | None = None,
              track_source: str | None = None) -> str:
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
    dl = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)
    reports = reports or [p]
    used = track_source or p.source.name
    listed = "<br>".join(_report(r, len(reports) > 1 and r.source.name == used) for r in reports)
    dl += f"<dt>{'Reports' if len(reports) > 1 else 'Source'}</dt><dd>{listed}</dd>"
    card = f'<div class="panel card">{card_svg}</div>' if card_svg else f'<p class="empty-state">{escape(error or "")}</p>'
    body = (f'<p class="sub"><a href="/">← Greenie board</a> · <a href="/passes/{p.id}/acmi">Download ACMI</a></p>'
            f"<h1>{escape(p.pilot.name)} · {escape(grade_name(g.grade) if g else '?')}</h1>"
            f'<dl class="facts">{dl}</dl>{card}')
    return _page(f"{p.pilot.name} {g.grade if g else ''}", body)



UPLOAD_SCRIPT = """
const form = document.getElementById('upload');
const tokenInput = form.elements.token;
try { tokenInput.value = localStorage.getItem('dcs-lso-token') || ''; } catch (e) {}
form.addEventListener('submit', (event) => {
  event.preventDefault();
  const status = document.getElementById('status');
  const bar = document.getElementById('bar');
  const data = new FormData();
  data.append('recording', form.elements.recording.files[0]);
  if (form.elements.debrief.files.length) data.append('debrief', form.elements.debrief.files[0]);
  if (form.elements.password && form.elements.password.value) data.append('password', form.elements.password.value);
  try { localStorage.setItem('dcs-lso-token', tokenInput.value); } catch (e) {}
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
    if token_required:
        token = ('<label>Upload token <input name="token" type="password" required autocomplete="off">'
                 '<span class="hint">The token of a source on this service (kept in this browser).</span></label>')
        intro = "Every carrier pass in it is graded and added to the board."
    else:
        token = ('<label>Your pilot password (if you\'ve set one) <input name="password" type="password" '
                 'autocomplete="current-password"><span class="hint">Needed only if you set a password for your '
                 "pilot name; you can also give it after uploading.</span></label>"
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
                  '<label>Password (if that pilot has set one) <input name="password" type="password" '
                  'autocomplete="current-password"></label>'
                  '<button type="submit">Import my passes</button></form>')
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
OVERLAY_ZOOM_SCRIPT = """
(() => {
  const box = document.getElementById('overlay');
  const reset = document.getElementById('zoom-reset');
  if (!box) return;
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
    const url = near === null ? box.dataset.src : box.dataset.src + '&near=' + near.toFixed(1) + '&far=' + far.toFixed(1);
    box.classList.add('loading');
    try {
      const r = await fetch(url);
      if (r.ok) {
        box.querySelector('svg').outerHTML = await r.text();
        reset.hidden = near === null;
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
  reset.addEventListener('click', () => load(null, null));
})();
"""

KIND_LABELS = {"fault": "Fault", "bias": "Leaning", "speed": "Speed", "outcome": "Outcome", "wires": "Wires",
               "trend": "Trend"}


def pilot_page(name: str, summary, passes: int, overlay_svg: str | None = None, overlay_src: str = "") -> str:
    """Meta grading: themes across the pilot's recent passes (`summary`: central's PilotSummary)."""
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
    picker = (f'<form class="filters" method="get"><label>Look at <select name="passes" onchange="this.form.submit()">'
              f"{options}</select> passes</label></form>")
    if not result.passes:
        body = (f'<p class="sub"><a href="/">← Greenie board</a></p>{heading}{seen}'
                '<p class="empty-state">No graded passes yet.</p>')
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
    body = (f'<p class="sub"><a href="/">← Greenie board</a></p>{heading}{seen}'
            f'<p class="sub">Last {result.passes} passes · {result.average_points:.2f} points on average · {grades}</p>'
            f"{picker}<div class=\"panel\"><h2 style=\"margin-top:0\">Analysis</h2>"
            f'<ul class="themes">{analysis}</ul>{strengths}'
            f'<h2>Results</h2><ul class="themes">{results}</ul>'
            f'<h2>These passes (oldest → newest)</h2><div class="recent">{cells}</div></div>'
            + (f'<h2>Traps overlaid <button id="zoom-reset" class="zoom-reset" hidden>Reset zoom</button></h2>'
               f'<div class="panel card" id="overlay" data-src="{escape(overlay_src)}">{overlay_svg}</div>'
               f"<script>{OVERLAY_ZOOM_SCRIPT}</script>" if overlay_svg else "")
            + f'<div id="tc-pop" hidden></div><script>{CARD_PREVIEW_SCRIPT}</script>')
    return _page(f"{name}: recent passes", body)


def pilot_settings_page(pilot, done: str | None, error: str | None) -> str:
    """Set or change the pilot's password; with it, change their side number."""
    name = pilot.name
    quoted = quote(name, safe="")
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    has = pilot.password_hash is not None
    password_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/password">'
        "<h2 style=\"margin-top:0\">" + ("Change password" if has else "Set a password") + "</h2>"
        + ("" if has else '<p class="sub">Nobody has set one for this pilot yet: setting it claims the name. '
           "Recordings uploaded without a token then need it to import this pilot's passes.</p>")
        + ('<label>Current password <input name="current" type="password" required autocomplete="current-password">'
           "</label>" if has else "")
        + '<label>New password <input name="new" type="password" required minlength="8" autocomplete="new-password">'
        '<span class="hint">At least 8 characters.</span></label>'
        '<label>New password again <input name="confirm" type="password" required minlength="8" '
        'autocomplete="new-password"></label><button type="submit">Save password</button></form>')
    modex_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/modex">'
        '<h2>Side number</h2>'
        f'<p class="sub">Now: {escape(pilot.modex or "not known yet")}. It\'s taken from your first pass that has one; '
        "you can change it here.</p>"
        + ('<label>Password <input name="password" type="password" required autocomplete="current-password"></label>'
           '<label>Side number <input name="modex" required pattern="[0-9]{1,4}" inputmode="numeric"></label>'
           '<button type="submit">Save side number</button>' if has else
           '<p class="sub">Set a password first to change it.</p>')
        + "</form>")
    body = (f'<p class="sub"><a href="/pilots/{quoted}">← {escape(name)}</a></p><h1>{escape(name)}: settings</h1>'
            f'{note}<div class="panel">{password_form}</div><div class="panel" style="margin-top:12px">{modex_form}</div>'
            '<p class="sub" style="margin-top:12px">Forgot your password? Ask the server\'s admin to reset it.</p>')
    return _page(f"{name}: settings", body)
