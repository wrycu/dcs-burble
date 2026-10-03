"""HTML pages for the central service: the greenie board and per-pass pages."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape

from ..grading import grade_name, grade_short
from .db import Pass, Upload

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
            title = f"{grade_name(g)}: {p.grade.text if p.grade else ''} · {_when(p.occurred_at)} · {p.mission or ''}"
            cells.append(f'<td><a class="cell {GRADE_CLASS.get(g, "")}" href="/passes/{p.id}" '
                         f'title="{escape(title)}">{escape(grade_short(g))}</a></td>')
        cells += ['<td><span class="cell empty"></span></td>'] * (columns - len(shown))
        rows.append(f'<tr><td class="pilot">{escape(name)}</td><td class="num">{len(items)}</td>'
                    f'<td class="num">{avg:.2f}</td><td class="num">{100 * traps / len(items):.0f}%</td>'
                    + "".join(cells) + "</tr>")

    legend = '<div class="legend">' + "".join(
        f'<span><span class="swatch {GRADE_CLASS[g]}"></span>{escape(grade_short(g))} {grade_name(g)}'
        f'{f" ({escape(g)})" if grade_short(g) != g else ""}</span>' for g in GRADE_COLORS
    ) + "</div>"
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
        ("When", _when(p.occurred_at)),
        ("Mission", p.mission or "–"),
        ("Carrier", f"{p.carrier_unit or ''} ({p.carrier_type})"),
        ("Aircraft", p.aircraft_type),
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
        token = ('<label>Upload token (optional) <input name="token" type="password" autocomplete="off">'
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
                  '<button type="submit">Import my passes</button></form>')
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
