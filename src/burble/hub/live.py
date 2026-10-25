"""The live LSO view (PLAN #50): what each server agent sends a few times a second (`agent.livefeed`), kept in
memory per carrier and passed on to the browsers watching it (server-sent events, `/live/{agent}/{carrier}/events`).

Nothing is stored: a viewer who opens the page gets the last KEEP_S of the carrier's traffic, then each update as
it comes. Everything here runs on the app's event loop (the hub is one process).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..callouts.voice import spoken_text
from ..cards.replay import aircraft_data, carrier_data
from ..cards.svg import call_label
from ..geometry import CARRIERS

KEEP_S = 90.0  # mission seconds of traffic kept for a viewer who opens the page
STALE_S = 60.0  # a carrier not heard of in this long (wall clock) is off the list of live carriers
# An agent's mission clock that hasn't moved in this long (wall clock) is paused (a dedicated server with no
# players pauses); the agent's heartbeat comes every 5 s.
PAUSED_AFTER_S = 15.0
# Mission time going back more than this is a new mission (DCS can reuse the carrier's object id).
RESTART_S = 5.0
QUEUE_MAX = 400  # updates waiting for a slow viewer (about 100 s of them); past this, it's disconnected
# The texts the LSO said recently, which the hub's voice may render for viewers (not just any text).
SPOKEN_MAX = 500


@dataclass(slots=True)
class AgentState:
    """What the hub has heard from one server agent's live view since it started."""
    heard_at: float  # monotonic
    heard_wall: datetime
    now: float = 0.0  # the newest mission time
    moved_at: float = 0.0  # monotonic, when the mission time last moved on


@dataclass(slots=True)
class AgentStatus:
    """One server agent on the /live overview."""
    agent: str
    status: str  # live | running | paused | silent
    carriers: list[Channel]  # live now
    heard: datetime | None  # last heard, since the hub started


@dataclass(slots=True)
class Channel:
    """One carrier on one server agent."""
    agent: str
    unit: str
    carrier_type: str
    carrier_id: int  # its object in the current mission (a new one means a new mission)
    seen_at: float  # monotonic
    now: float = 0.0  # the newest mission time
    state: dict = field(default_factory=dict)  # the ship's speed, night, wind over the deck
    types: dict[str, dict] = field(default_factory=dict)  # aircraft types seen, as the page draws them
    pilots: dict[int, str] = field(default_factory=dict)  # aircraft id -> pilot, for the list of carriers
    updates: deque[tuple[float, dict]] = field(default_factory=deque)  # (mission time, update)
    viewers: set[asyncio.Queue] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str]:
        return self.agent, self.unit

    def opening(self) -> dict:
        """The first event a viewer gets: the carrier as the page draws it, and the types to draw."""
        return {"agent": self.agent, "unit": self.unit, "carrier": carrier_data(self.carrier_type),
                "state": self.state, "types": self.types}


class LiveBroker:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.channels: dict[tuple[str, str], Channel] = {}
        self.agents: dict[str, AgentState] = {}
        self.spoken: dict[str, None] = {}  # insertion-ordered set

    def publish(self, agent: str, batch: dict) -> int:
        """A server agent's batch (`agent.livefeed.LiveFeed.batch`); returns how many carriers it updated."""
        now = float(batch.get("now") or 0.0)
        self._heard(agent, now)
        count = 0
        for entry in batch.get("carriers") or []:
            unit, carrier_type = str(entry.get("unit") or ""), str(entry.get("type") or "")
            if not unit or carrier_type not in CARRIERS:
                continue
            channel = self.channels.get((agent, unit))
            if (channel is None or channel.carrier_id != entry.get("id") or channel.carrier_type != carrier_type
                    or now < channel.now - RESTART_S):
                channel = self._reset(agent, unit, carrier_type, entry.get("id"), channel)
            channel.seen_at = self.clock()
            channel.now = max(channel.now, now)
            state = {k: entry[k] for k in ("speed", "night", "deck_wind_kt") if entry.get(k) is not None}
            update: dict = {"now": now, "aircraft": [], "calls": [], "gone": list(entry.get("gone") or [])}
            if batch.get("clock"):
                update["clock"] = batch["clock"]
            if state != channel.state:
                channel.state = state
                update["state"] = state
            new_types = {}
            for plane in entry.get("aircraft") or []:
                kind = str(plane.get("type") or "")
                if kind not in channel.types:
                    channel.types[kind] = new_types[kind] = aircraft_data(kind)
                channel.pilots[plane["id"]] = str(plane.get("pilot") or kind)
                update["aircraft"].append({k: plane.get(k) for k in ("id", "type", "pilot", "track", "groove")})
            for gone in update["gone"]:
                channel.pilots.pop(gone, None)
            if new_types:
                update["types"] = new_types
            for call in entry.get("calls") or []:
                shown = {"aircraft": call.get("aircraft"), "t": call.get("time"), "text": call_label(call),
                         "by": call.get("by", "lso")}
                if call.get("by") != "pilot":
                    shown["say"] = spoken_text(call)
                    self._spoke(shown["say"])
                update["calls"].append(shown)
            channel.updates.append((now, update))
            while channel.updates and channel.updates[0][0] < now - KEEP_S:
                channel.updates.popleft()
            self._send(channel, "update", update)
            count += 1
        return count

    def announce(self, agent: str, unit: str | None, aircraft_id: int | None, graded: dict) -> None:
        """A pass from this agent was graded (its upload came in): shown on the page with a link."""
        channel = self.channels.get((agent, unit or ""))
        if channel is not None:
            self._send(channel, "graded", {"aircraft": aircraft_id, **graded})

    def live(self) -> list[Channel]:
        """The carriers heard from lately, busiest first."""
        now = self.clock()
        for key in [k for k, c in self.channels.items() if now - c.seen_at > STALE_S and not c.viewers]:
            del self.channels[key]
        fresh = [c for c in self.channels.values() if now - c.seen_at <= STALE_S]
        return sorted(fresh, key=lambda c: (-len(c.pilots), c.agent, c.unit))

    def status(self, agents: list[str]) -> list[AgentStatus]:
        """Each of these server agents (the hub's, by name) as the /live overview shows it: live ones first, then
        running, paused, and the silent ones."""
        now = self.clock()
        live: dict[str, list[Channel]] = {}
        for channel in self.live():
            live.setdefault(channel.agent, []).append(channel)
        out = []
        for name in agents:
            state = self.agents.get(name)
            heard = state is not None and now - state.heard_at <= STALE_S
            if live.get(name):
                status = "live"
            elif heard and now - state.moved_at <= PAUSED_AFTER_S:
                status = "running"
            elif heard:
                status = "paused"
            else:
                status = "silent"
            out.append(AgentStatus(name, status, live.get(name, []), state.heard_wall if state else None))
        order = {"live": 0, "running": 1, "paused": 2, "silent": 3}
        return sorted(out, key=lambda a: (order[a.status], a.agent.lower()))

    def get(self, agent: str, unit: str) -> Channel | None:
        return self.channels.get((agent, unit))

    def may_say(self, text: str) -> bool:
        """Was this said on a live view lately (so the hub's voice may render it for viewers)?"""
        return text in self.spoken

    @asynccontextmanager
    async def watch(self, channel: Channel) -> AsyncIterator[asyncio.Queue]:
        """A viewer's queue of (event, data): the opening event and the buffered updates first, then each new one;
        (None, None) when the viewer fell too far behind."""
        queue: asyncio.Queue = asyncio.Queue(QUEUE_MAX)
        queue.put_nowait(("carrier", channel.opening()))
        for _, update in list(channel.updates)[-(QUEUE_MAX - 2):]:
            queue.put_nowait(("update", update))
        channel.viewers.add(queue)
        try:
            yield queue
        finally:
            channel.viewers.discard(queue)

    def _heard(self, agent: str, now: float) -> None:
        state = self.agents.get(agent)
        clock = self.clock()
        if state is None:
            state = self.agents[agent] = AgentState(clock, datetime.now(UTC), now, clock)
        elif now != state.now:  # moving on, or back (a new mission)
            state.now, state.moved_at = now, clock
        state.heard_at, state.heard_wall = clock, datetime.now(UTC)

    def _reset(self, agent: str, unit: str, carrier_type: str, carrier_id, old: Channel | None) -> Channel:
        channel = Channel(agent, unit, carrier_type, carrier_id, self.clock())
        self.channels[channel.key] = channel
        if old is not None:  # a new mission (or ship): viewers start over with it
            channel.viewers = old.viewers
            self._send(channel, "carrier", channel.opening())
        return channel

    def _send(self, channel: Channel, event: str, data: dict) -> None:
        for queue in list(channel.viewers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                channel.viewers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait((None, None))

    def _spoke(self, text: str) -> None:
        self.spoken.pop(text, None)
        self.spoken[text] = None
        while len(self.spoken) > SPOKEN_MAX:
            del self.spoken[next(iter(self.spoken))]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
