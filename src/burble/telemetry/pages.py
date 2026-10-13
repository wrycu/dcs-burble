"""The Usage page's content, drawn from a receiver's stats (`Store.usage`): by the receiver itself, and by a hub
whose admins connected it to a receiver (it fetches `/api/v1/usage`)."""

from __future__ import annotations

from datetime import datetime
from html import escape

STYLE = """
.usage .tiles { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.usage .tile { flex: 1 1 140px; border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; }
.usage .tile b { display: block; font-size: 22px; }
.usage .tile span { color: var(--muted); font-size: 12px; }
.usage table { border-collapse: collapse; width: 100%; font-size: 13px; }
.usage th, .usage td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.usage th { color: var(--muted); font-weight: 600; font-size: 12px; }
.usage .scroll { overflow-x: auto; }
.usage svg .bar { fill: #0969da; } .usage svg text { fill: var(--muted); font-size: 10px; }
.usage h2 { font-size: 16px; margin: 16px 0 6px; }
"""


def _n(v: int) -> str:
    return f"{v:,}"


def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return iso


def _versions(counts: dict[str, int], label: str) -> str:
    if not counts:
        return '<span class="sub">none</span>'
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{escape(label)}{escape(k)}: {_n(n)}" for k, n in items)


def _weekly_chart(weekly: list[dict]) -> str:
    w, h, pad = 640, 140, 24
    top = max([x["landings"] for x in weekly] + [1])
    bar = (w - 2 * pad) / max(len(weekly), 1)
    out = [f'<svg viewBox="0 0 {w} {h + 20}" width="100%" role="img" aria-label="Landings per week">']
    for i, x in enumerate(weekly):
        bh = (h - pad) * x["landings"] / top
        bx = pad + i * bar
        out.append(f'<rect class="bar" x="{bx + 1:.1f}" y="{h - bh:.1f}" width="{max(bar - 2, 1):.1f}" height="{bh:.1f}">'
                   f'<title>week of {escape(x["week"])}: {x["landings"]} landings</title></rect>')
        if i % 4 == 0:
            out.append(f'<text x="{bx:.1f}" y="{h + 14}">{escape(x["week"][5:])}</text>')
    out.append(f'<text x="0" y="12">{_n(top)}</text></svg>')
    return "".join(out)


def usage_body(stats: dict) -> str:
    """The page's content (no page shell): the totals, versions, landings per week, and each install."""
    inst, tot, ver = stats["installs"], stats["totals"], stats["versions"]
    tracks = tot["tracks"]
    tiles = [(inst["last_30d"], "hubs reporting (30 days)"), (inst["last_7d"], "hubs reporting (7 days)"),
             (inst["ever"], "hubs ever"), (tot["landings"], "landings (all reported)"),
             (tot["landings_30d"], "landings (30 days)"), (tot["pilots_30d"], "pilots landing (30 days)")]
    body = ['<div class="usage"><div class="tiles">'
            + "".join(f"<div class=\"tile\"><b>{_n(v)}</b><span>{escape(t)}</span></div>" for v, t in tiles)
            + "</div>"]
    body.append(
        "<h2>How landings were reported</h2>"
        f"<p>Server report only: {_n(tracks['server_only'])} · pilot's report only: {_n(tracks['pilot_only'])} · "
        f"both: {_n(tracks['both'])} · with live LSO calls: {_n(tot['live_call_landings'])}</p>"
        "<h2>Server agents and pilot hooks (hubs reporting in the last 30 days)</h2>"
        f"<p>Server agents making live calls over SRS: {_n(tot['agents']['live_calls'])} · collecting only: "
        f"{_n(tot['agents']['collect_only'])} · pilot hooks: {_n(tot['pilot_hooks'])}</p>"
        "<h2>Versions</h2>"
        f"<p>Hubs: {_versions(ver['hub'], '')}<br>Server agents: {_versions(ver['agent'], '')}<br>"
        f"Pilot hooks: {_versions(ver['pilot_hook'], 'v')}</p>"
        "<h2>Landings per week</h2>" + _weekly_chart(stats["weekly"]))
    rows = "".join(
        f"<tr><td><code>{escape(i['install'][:8])}</code></td><td>{escape(i['version'])}</td>"
        f"<td>{_date(i['first_seen'])}</td><td>{_date(i['last_seen'])}</td>"
        f"<td>{_n(i['landings'])}</td><td>{_n(i['landings_30d'])}</td><td>{_n(i['pilots_30d'])}</td>"
        f"<td>{_n(i['tracks']['server_only'])} / {_n(i['tracks']['pilot_only'])} / {_n(i['tracks']['both'])}</td>"
        f"<td>{_n(i['live_call_landings'])}</td>"
        f"<td>{_n(i['agents'].get('live_calls', 0))} / {_n(i['agents'].get('collect_only', 0))}</td>"
        f"<td>{_n(i['pilot_hooks'].get('count', 0))}</td></tr>" for i in stats["per_install"])
    body.append(
        "<h2>Each hub</h2>" + (
            '<div class="scroll"><table><thead><tr><th>Hub</th><th>Version</th><th>First report</th><th>Last report</th>'
            "<th>Landings</th><th>30 days</th><th>Pilots (30 days)</th><th>Server / pilot / both</th>"
            "<th>Live calls</th><th>Agents live / collect</th><th>Pilot hooks</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>" if rows else '<p class="sub">No reports yet.</p>'))
    body.append(f'<p class="sub">Hubs are told apart only by a random id each makes for itself. '
                f'As of {_date(stats["generated_at"])}.</p></div>')
    return "".join(body)
