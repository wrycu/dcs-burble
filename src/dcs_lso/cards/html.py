"""A basic HTML page listing trap cards (each card is its own SVG file)."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True, slots=True)
class CardEntry:
    svg_file: str
    svg: str  # inlined, so the page is one self-contained file
    pilot: str
    grade: str
    grade_text: str
    points: float
    outcome: str
    source: str
    start_time: float
    dcs_grade: str | None


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trap Cards</title>
<style>
:root {{ --bg: #f6f8fa; --card: #ffffff; --text: #1f2328; --muted: #656d76; --border: #d0d7de; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #010409; --card: #0d1117; --text: #e6edf3; --muted: #8d96a0; --border: #30363d; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
.sub {{ color: var(--muted); margin: 0 0 20px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 28px; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }}
th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
td.grade {{ font-weight: 700; }}
a {{ color: inherit; }}
figure {{ margin: 0 0 24px; background: var(--card); border: 1px solid var(--border); border-radius: 12px;
         overflow: hidden; }}
figure svg {{ display: block; width: 100%; height: auto; }}
figure figcaption {{ padding: 6px 12px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); }}
.scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<main>
<h1>Trap Cards</h1>
<p class="sub">{count} passes · grading v{version}</p>
<div class="scroll"><table>
<thead><tr><th>Pilot</th><th>Grade</th><th>Pts</th><th>Outcome</th><th>DCS LSO</th><th>Recording</th></tr></thead>
<tbody>
{rows}
</tbody>
</table></div>
{cards}
</main>
</body>
</html>
"""


def render_index(entries: list[CardEntry], version: str) -> str:
    rows = []
    cards = []
    for i, e in enumerate(entries):
        anchor = f"card-{i}"
        rows.append(
            f'<tr><td><a href="#{anchor}">{escape(e.pilot)}</a></td><td class="grade">{escape(e.grade_text)}</td>'
            f"<td>{e.points:g}</td><td>{escape(e.outcome)}</td><td>{escape(e.dcs_grade or '–')}</td>"
            f"<td>{escape(e.source)} @ {e.start_time:.0f}s</td></tr>"
        )
        cards.append(f'<figure id="{anchor}">{e.svg}<figcaption><a href="{escape(e.svg_file)}">'
                     f'{escape(e.svg_file)}</a></figcaption></figure>')
    return PAGE.format(count=len(entries), version=escape(version), rows="\n".join(rows), cards="\n".join(cards))
