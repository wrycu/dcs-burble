"""Discord: the greenie board as one message kept up to date by editing it, and a post per landing with its
trap card. Both through webhooks (Server Settings > Integrations > Webhooks in Discord), each optional and
each to its own webhook (they may be the same one).

Runs in a background thread: the hub tells it which landing changed (`Hub.listeners`), and it posts or edits
the landing's message and then the board, at most every BOARD_EVERY_S. Discord's rate limits are respected
(429 responses say how long to wait).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from ..cards import render_card
from ..cards.board import BoardRow, render_board
from ..grading import grade_name, grade_pass, grade_short
from ..grading.grade import Grade as GradeValue
from .db import Pass, Setting
from .pages import card_title

log = logging.getLogger(__name__)

BOARD_EVERY_S = 10.0  # edit the board at most this often
BOARD_DAYS = 30  # the board covers this many days, as the website's default
BOARD_PASSES = 15  # squares per pilot
# Landings older than this when they arrive (e.g. a whole old recording uploaded) aren't posted one by one;
# they still count on the board.
POST_MAX_AGE = timedelta(hours=6)
BOARD_SETTING = "discord_board_message"
CHANGES_EVERY_S = 5.0  # look for landings changed outside this process (`hub regrade`) this often

COLORS = {GradeValue.PERFECT: 0x2DA44E, GradeValue.OK: 0x3FB950, GradeValue.FAIR: 0xD4A72C, GradeValue.NO_GRADE: 0x9A6700,
          GradeValue.CUT: 0xCF222E, GradeValue.BOLTER: 0x0969DA, GradeValue.WAVE_OFF: 0x8C959F}


def png(svg: str, zoom: float = 1.5) -> bytes:
    """An SVG (a trap card, the board) as a PNG, which Discord shows inline."""
    import resvg_py
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))


class Discord:
    def __init__(self, hub, board_webhook: str | None = None, traps_webhook: str | None = None,
                 public_url: str = "", client: httpx.Client | None = None, start: bool = True) -> None:
        self.hub = hub
        self.board_webhook = board_webhook or None
        self.traps_webhook = traps_webhook or None
        self.public_url = public_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._board_due = True  # bring the board up to date at start-up
        self._board_at = -BOARD_EVERY_S
        self._changes_at = -CHANGES_EVERY_S
        hub.listeners.append(self._queue.put)
        if start:
            threading.Thread(target=self._run, name="discord", daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self.board_webhook or self.traps_webhook)

    # -- the worker ------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                landing_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                landing_id = False
            try:
                if time.monotonic() - self._changes_at >= CHANGES_EVERY_S:
                    self._changes_at = time.monotonic()
                    self.pick_up_changes()
                self.step(landing_id)
            except Exception:  # keep going whatever Discord does
                log.exception("Discord update failed")

    def step(self, landing_id: int | None | bool = False, now: float | None = None) -> None:
        """Handle one change (`landing_id`: a landing; None: many; False: nothing new), and edit the board if due."""
        if landing_id is not False:
            self._board_due = True
            if landing_id is not None and self.traps_webhook:
                self.post_landing(landing_id)
        now = time.monotonic() if now is None else now
        if self._board_due and self.board_webhook and now - self._board_at >= BOARD_EVERY_S:
            self._board_due, self._board_at = False, now
            self.update_board()

    def pick_up_changes(self) -> list[int]:
        """Landings changed by another process (e.g. `hub regrade` from the command line, which can't tell this
        one): queue each, so its post is edited and the board updated, and clear the mark."""
        with self.hub.sessions.begin() as s:
            changed = list(s.scalars(select(Pass).where(Pass.discord_stale.is_(True))))
            for p in changed:
                p.discord_stale = None
            ids = [p.id for p in changed]
        for landing_id in ids:
            self._queue.put(landing_id)
        return ids

    # -- HTTP --------------------------------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        for _ in range(5):
            r = self.client.request(method, url, **kwargs)
            if r.status_code != 429:
                return r
            wait = float((r.json() if r.content else {}).get("retry_after", 1.0))
            time.sleep(min(max(wait, 0.1), 30.0))
        return r

    def _upsert(self, webhook: str, message_id: str | None, payload: dict, files: dict | None = None) -> str | None:
        """Edit the message, or post a new one if there's none (or it was deleted). Returns its id."""
        def send(method: str, url: str) -> httpx.Response:
            if files:
                return self._send(method, url, data={"payload_json": json.dumps(payload)}, files=files)
            return self._send(method, url, json=payload)

        if message_id:
            r = send("PATCH", f"{webhook}/messages/{message_id}")
            if r.status_code == 200:
                return message_id
            if r.status_code != 404:
                log.warning("Discord refused an edit (%s %s)", r.status_code, r.text[:200])
                return message_id
        r = send("POST", f"{webhook}?wait=true")
        if r.status_code not in (200, 201):
            log.warning("Discord refused a post (%s %s)", r.status_code, r.text[:200])
            return None
        return str(r.json()["id"])

    def _link(self, path: str) -> str | None:
        return f"{self.public_url}{path}" if self.public_url else None

    # -- a post per landing ---------------------------------------------------------------------

    def post_landing(self, landing_id: int) -> None:
        """Post the landing with its trap card, or edit its post if it has one (e.g. a better report merged in)."""
        hub = self.hub
        with hub.sessions() as s:
            p = s.scalar(select(Pass).where(Pass.id == landing_id).options(
                selectinload(Pass.grades), selectinload(Pass.pilot), selectinload(Pass.slice), selectinload(Pass.source)))
            if p is None or p.merged_into_id is not None or (p.is_track and not p.is_dcs_only) or p.grade is None:
                return
            when = p.occurred_at or p.created_at
            if p.discord_message_id is None and when is not None and \
                    datetime.now(UTC) - when.replace(tzinfo=when.tzinfo or UTC) > POST_MAX_AGE:
                return  # backfilled: the board shows it, no post of its own
            svg = None
            if not p.is_dcs_only:  # graded by DCS alone: no carrier, so no trap card
                result = hub.load_pass(p)
                svg = render_card(result, grade_pass(result), card_title(p), uid="discord", calls=p.calls,
                                  night=bool(p.night), dark=True)
            g = GradeValue(p.grade.grade)
            estimate = (p.grade.detail or {}).get("wire_estimate")
            wire = f"wire #{p.wire}" if p.wire is not None else (f"wire #{estimate} (est.)" if estimate else None)
            facts = [p.outcome, wire if p.outcome == "trap" else None, p.carrier_unit or p.carrier_type,
                     p.aircraft_type.replace("_hornet", ""), "🌙 night" if p.night else None,
                     "graded by DCS's LSO" if p.is_dcs_only else None]
            payload = {"embeds": [{
                "title": f"{p.pilot.name} · {grade_name(g.value)} ({grade_short(g.value)})",
                "url": self._link(f"/passes/{p.id}"),
                "description": f"`{p.grade.text}`\n" + " · ".join(x for x in facts if x),
                "color": COLORS.get(g, 0x8C959F),
                "timestamp": when.replace(tzinfo=when.tzinfo or UTC).isoformat() if when else None,
                "footer": {"text": p.mission or "dcs-lso"},
            }]}
            if svg is not None:
                payload["embeds"][0]["image"] = {"url": "attachment://card.png"}
                payload["attachments"] = [{"id": 0, "filename": "card.png"}]
            message_id = p.discord_message_id
        files = {"files[0]": ("card.png", png(svg), "image/png")} if svg is not None else None
        new_id = self._upsert(self.traps_webhook, message_id, payload, files=files)
        if new_id and new_id != message_id:
            with hub.sessions.begin() as s:
                s.get(Pass, landing_id).discord_message_id = new_id

    # -- the greenie board ------------------------------------------------------------------------

    def board_rows(self) -> list[BoardRow]:
        """Each pilot with landings in the last BOARD_DAYS: their latest BOARD_PASSES (oldest first), average and
        trap rate."""
        since = datetime.now(UTC) - timedelta(days=BOARD_DAYS)
        with self.hub.sessions() as s:
            q = (select(Pass).where(Pass.merged_into_id.is_(None), or_(Pass.kind.is_(None), Pass.kind != "track"),
                                    func.coalesce(Pass.occurred_at, Pass.created_at) >= since)
                 .order_by(func.coalesce(Pass.occurred_at, Pass.created_at))
                 .options(selectinload(Pass.grades), selectinload(Pass.pilot)))
            by_pilot: dict[str, list[Pass]] = defaultdict(list)
            for p in s.scalars(q):
                by_pilot[p.pilot.name].append(p)
            rows = []
            for name in sorted(by_pilot, key=str.lower):
                items = by_pilot[name]
                graded = [p for p in items if p.grade]
                rows.append(BoardRow(
                    name=name, passes=len(items),
                    average=sum(p.grade.points for p in graded) / len(graded) if graded else None,
                    trap_rate=sum(p.outcome == "trap" for p in items) / len(items),
                    cells=[(p.grade.grade if p.grade else "?", bool(p.night)) for p in items[-BOARD_PASSES:]]))
        return rows

    def update_board(self) -> None:
        with self.hub.sessions() as s:
            row = s.get(Setting, BOARD_SETTING)
            message_id = row.value if row else None
        rows = self.board_rows()
        now = datetime.now(UTC)
        subtitle = (f"last {BOARD_DAYS} days · {sum(r.passes for r in rows)} landings · "
                    f"updated {now:%Y-%m-%d %H:%M} UTC")
        image = png(render_board(rows, BOARD_PASSES, title="", subtitle=subtitle), zoom=1.0)  # the message names it
        # A plain message with the image attached (no embed, so no coloured bar beside it); `embeds: []` clears
        # the embed an older board message had.
        link = self._link("/")
        payload = {"content": f"**[Greenie Board](<{link}>)**" if link else "**Greenie Board**", "embeds": [],
                   "attachments": [{"id": 0, "filename": "board.png"}]}
        new_id = self._upsert(self.board_webhook, message_id, payload,
                              files={"files[0]": ("board.png", image, "image/png")})
        if new_id and new_id != message_id:
            with self.hub.sessions.begin() as s:
                row = s.get(Setting, BOARD_SETTING)
                if row is None:
                    s.add(Setting(key=BOARD_SETTING, value=new_id))
                else:
                    row.value = new_id
