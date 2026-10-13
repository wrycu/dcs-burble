"""Usage reports (PLAN #17): once a day the hub sends anonymous counts of its use to the burble project, and to
any other addresses its admins add (a community's own receiver, `burble telemetry serve`). On unless an admin
turns it off; the admin page says so until an admin has seen it.

A report covers the last 30 days, one entry per UTC day, so a receiver can store each day once (a newer report
replaces the day) and keep the whole history. What's in it is `WHAT_IS_SENT`; what never is, `NEVER_SENT`.

Runs in a background thread, like Discord's: the first report 10 minutes after the hub starts, then every
24 hours. One try per address, failures noted for the admin page and logged at debug; nothing waits on it.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from ..slices import _collector_version
from .db import Pass, Setting, Source

log = logging.getLogger(__name__)

DEFAULT_URLS = ["https://telemetry.wrycu.com"]
REPORT_PATH = "/api/v1/telemetry"
USAGE_PATH = "/api/v1/usage"
REPORT_SCHEMA = 1
DAYS = 30
FIRST_AFTER_S = 600.0
EVERY_S = 24 * 3600.0
TIMEOUT_S = 10.0

INSTALL_SETTING = "telemetry_install"
ENABLED_SETTING = "telemetry_enabled"  # "0": off; anything else (or unset): on
ACKNOWLEDGED_SETTING = "telemetry_acknowledged"  # an admin has seen the notice
URLS_SETTING = "telemetry_urls"  # JSON list; unset: DEFAULT_URLS
STATUS_SETTING = "telemetry_status"  # JSON {url: {"at", "ok", "message"}}
VIEW_SETTING = "telemetry_view"  # JSON {"url", "key"}: a receiver whose stats the admins' Usage page shows

WHAT_IS_SENT = [
    "a random id this hub made for itself, so its reports can be told apart (not based on its address or "
    "anything else)",
    "the hub's version",
    "for each of the last 30 days: how many landings, how many pilots landed, how many landings had a server "
    "report, a pilot's report (pilot hook or uploaded recording) or both, and how many got live LSO calls",
    "how many pilots landed in the last 30 days",
    "the server agents used in the last 30 days: how many make live calls over SRS and how many only collect, "
    "and their versions",
    "the pilot hooks seen in the last 30 days: how many, and their versions",
]
NEVER_SENT = [
    "pilot names, UCIDs or IP addresses",
    "the hub's address or name",
    "mission, server or carrier names",
    "the tracks, grades or calls themselves, or anything else about a landing",
]


def _get(s, key: str) -> str | None:
    row = s.get(Setting, key)
    return row.value if row else None


def _put(s, key: str, value: str | None) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{url!r} isn't an address (it starts with http:// or https://)")
    return url


class Settings:
    """The usage report settings, kept in the hub's `settings` table."""

    def __init__(self, hub) -> None:
        self.hub = hub

    def install(self) -> str:
        with self.hub.sessions.begin() as s:
            value = _get(s, INSTALL_SETTING)
            if not value:
                value = str(uuid.uuid4())
                _put(s, INSTALL_SETTING, value)
            return value

    def enabled(self) -> bool:
        with self.hub.sessions() as s:
            return _get(s, ENABLED_SETTING) != "0"

    def acknowledged(self) -> bool:
        with self.hub.sessions() as s:
            return _get(s, ACKNOWLEDGED_SETTING) == "1"

    def set_enabled(self, on: bool) -> None:
        """Also counts as having seen the notice."""
        with self.hub.sessions.begin() as s:
            _put(s, ENABLED_SETTING, "1" if on else "0")
            _put(s, ACKNOWLEDGED_SETTING, "1")

    def urls(self) -> list[str]:
        with self.hub.sessions() as s:
            value = _get(s, URLS_SETTING)
        return list(DEFAULT_URLS) if value is None else json.loads(value)

    def set_urls(self, urls: list[str] | None) -> list[str]:
        """None: back to the default (the project's receiver). An empty list sends nowhere."""
        clean = None if urls is None else list(dict.fromkeys(_base(u) for u in urls if u.strip()))
        with self.hub.sessions.begin() as s:
            _put(s, URLS_SETTING, None if clean is None else json.dumps(clean))
            _put(s, ACKNOWLEDGED_SETTING, "1")  # they've seen where reports go
        return self.urls()

    def status(self) -> dict[str, dict]:
        with self.hub.sessions() as s:
            value = _get(s, STATUS_SETTING)
        return json.loads(value) if value else {}

    def _note(self, url: str, ok: bool, message: str) -> None:
        with self.hub.sessions.begin() as s:
            status = json.loads(_get(s, STATUS_SETTING) or "{}")
            status[url] = {"at": datetime.now(UTC).isoformat(), "ok": ok, "message": message}
            _put(s, STATUS_SETTING, json.dumps(status))

    def view(self) -> tuple[str, str] | None:
        with self.hub.sessions() as s:
            value = _get(s, VIEW_SETTING)
        if not value:
            return None
        v = json.loads(value)
        return v["url"], v["key"]

    def set_view(self, url: str | None, key: str = "") -> None:
        """A receiver whose stats the admins' Usage page shows, with its API key or password; None: none."""
        if url is not None and not key.strip():
            raise ValueError("give the receiver's API key or password")
        value = None if url is None else json.dumps({"url": _base(url), "key": key.strip()})
        with self.hub.sessions.begin() as s:
            _put(s, VIEW_SETTING, value)


def build_report(hub, now: datetime | None = None) -> dict:
    """This hub's usage report: counts only (see WHAT_IS_SENT and NEVER_SENT)."""
    from .service import PILOT_HOOKS, _aware

    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    first = today - timedelta(days=DAYS - 1)
    since = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
    days: dict[date, dict] = {first + timedelta(days=i): {"landings": 0, "server_only": 0, "pilot_only": 0,
                                                           "both": 0, "live_calls": 0} for i in range(DAYS)}
    day_pilots: dict[date, set[int]] = defaultdict(set)
    pilots: set[int] = set()
    hooks: dict[tuple, int] = {}  # each pilot hook (by its pilot token, or its UCID): its newest version
    with hub.sessions() as s:
        landings = list(s.scalars(
            select(Pass).where(Pass.merged_into_id.is_(None), or_(Pass.kind.is_(None), Pass.kind != "track"),
                               Pass.occurred_at >= since)
            .options(selectinload(Pass.source), selectinload(Pass.slice))))
        merged = defaultdict(list)
        if landings:
            for r in s.scalars(select(Pass).where(Pass.merged_into_id.in_([p.id for p in landings]))
                               .options(selectinload(Pass.source), selectinload(Pass.slice))):
                merged[r.merged_into_id].append(r)
        for landing in landings:
            day = _aware(landing.occurred_at).astimezone(UTC).date()
            if day not in days:
                continue  # flown "in the future" (a clock off): left out
            counts = days[day]
            counts["landings"] += 1
            day_pilots[day].add(landing.pilot_id)
            pilots.add(landing.pilot_id)
            server = pilot = False
            for r in [landing, *merged[landing.id]]:
                hook = (r.slice.sidecar or {}).get("pilot_hook") if r.slice else None
                if hook is not None:
                    pilot = True
                    who = (("token", r.source_id) if r.source is not None and r.source.name != PILOT_HOOKS
                           else ("ucid", hook.get("ucid") or f"report {r.id}"))
                    hooks[who] = max(hooks.get(who, 0), int(hook.get("version") or 1))
                elif r.source is not None and r.source.kind == "server":
                    server = True
                else:
                    pilot = True  # an uploaded recording, or the pilot uploader
            counts["server_only" if server and not pilot else "pilot_only" if pilot and not server else "both"] += 1
            if landing.calls:
                counts["live_calls"] += 1

        agents = []
        for a in s.scalars(select(Source).where(Source.kind == "server", Source.revoked_at.is_(None))):
            if a.last_used_at is None or _aware(a.last_used_at) < since:
                continue
            latest = s.scalar(select(Pass).where(Pass.source_id == a.id).order_by(Pass.id.desc()).limit(1)
                              .options(selectinload(Pass.slice)))
            agent_version = ((latest.slice.sidecar or {}).get("collector_version") if latest else None) or "unknown"
            live = bool(((a.config or {}).get("callouts") or {}).get("enabled"))
            agents.append((live, str(agent_version)))

    return {
        "schema": REPORT_SCHEMA,
        "install": Settings(hub).install(),
        "version": _collector_version(),
        "days": [{"day": d.isoformat(), "pilots": len(day_pilots[d]), **c} for d, c in sorted(days.items())],
        "pilots_30d": len(pilots),
        "agents": {"live_calls": sum(1 for live, _ in agents if live),
                   "collect_only": sum(1 for live, _ in agents if not live),
                   "versions": dict(Counter(v for _, v in agents))},
        "pilot_hooks": {"count": len(hooks), "versions": {str(k): n for k, n in Counter(hooks.values()).items()}},
    }


NOTICE = ("This hub sends anonymous usage counts to the burble project once a day (on unless an admin turns it "
          "off). See \"Usage reports\" on the admin page for exactly what is and isn't sent.")


class Telemetry:
    """Sends the usage report to each address, once a day, while on."""

    def __init__(self, hub, client: httpx.Client | None = None, start: bool = True) -> None:
        self.hub = hub
        self.settings = Settings(hub)
        self.client = client or httpx.Client(timeout=TIMEOUT_S)
        self._stop = threading.Event()
        if self.settings.enabled() and not self.settings.acknowledged():
            log.warning("%s Turn it off on the admin page or with `burble hub telemetry off`.", NOTICE)
        if start:
            threading.Thread(target=self._run, name="telemetry", daemon=True).start()

    def _run(self) -> None:
        wait = FIRST_AFTER_S
        while not self._stop.wait(wait):
            try:
                self.send()
            except Exception:  # noqa: BLE001 - never let a report take the hub down
                log.debug("usage report failed", exc_info=True)
            wait = EVERY_S

    def stop(self) -> None:
        self._stop.set()

    def send(self) -> dict[str, bool]:
        """Send the report to every address now (if on); which took it."""
        if not self.settings.enabled():
            return {}
        report = build_report(self.hub)
        results = {}
        for url in self.settings.urls():
            try:
                r = self.client.post(url + REPORT_PATH, json=report)
                ok = r.status_code < 300
                message = "sent" if ok else f"refused ({r.status_code}): {r.text[:200]}"
            except httpx.HTTPError as exc:
                ok, message = False, f"not reached: {exc}"
            if not ok:
                log.debug("usage report to %s: %s", url, message)
            self.settings._note(url, ok, message)
            results[url] = ok
        return results


def fetch_usage(hub, client: httpx.Client | None = None) -> dict:
    """The stats of the receiver the admins connected (Settings.set_view). Raises LookupError when none is set,
    and RuntimeError (with what to tell the admin) when it can't be had."""
    view = Settings(hub).view()
    if view is None:
        raise LookupError("no receiver connected")
    url, key = view
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        r = client.get(url + USAGE_PATH, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise RuntimeError(f"couldn't reach {url}: {exc}") from exc
    if r.status_code in (401, 403):
        raise RuntimeError(f"{url} refused the API key or password")
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered {r.status_code}")
    return r.json()
