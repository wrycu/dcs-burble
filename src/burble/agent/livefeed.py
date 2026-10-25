"""What's flying around each carrier, sent to the hub a few times a second for its live LSO view (PLAN #50).

Every aircraft within `radius_m` of a carrier (the nearest, if several) is sent in that carrier's frame, as the
replay has it (cards.replay: x starboard, y above the deck, z forward, heading relative to the ship's), with its
groove numbers while it's in a pass, and the calls as they're made. Nothing is kept: a batch the hub doesn't
take is dropped, since live data is no use late.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ..acmi import ObjectTrack, Sample
from ..cards.replay import mission_clock
from ..geometry import CARRIERS, CarrierPose, DeckFrame
from .callouts import KT, LiveCallouts, MadeCall
from .live import _extrapolate

log = logging.getLogger(__name__)

NM = 1852.0
FT = 0.3048
SEND_INTERVAL_S = 0.25
DEFAULT_RADIUS_NM = 10.0
MAX_ALT_M = 10000 * FT  # above the deck
# A carrier with nothing around it is still sent this often, so the hub knows it's there.
CARRIER_EVERY_S = 5.0
# With nothing else to send, an update with no carriers this often (wall clock), so the hub knows the agent is
# connected to its mission (and from its mission time, whether that's running or paused).
HEARTBEAT_S = 5.0
# The ship's speed is measured over this long (shorter spans are noisy).
SHIP_SPEED_S = 10.0
# The hub has said no (e.g. an older hub without the live view): don't ask again for this long.
REFUSED_BACKOFF_S = 300.0


def live_settings(config: dict) -> tuple[bool, float]:
    """(enabled, radius in meters) from the agent's config on the hub (`live`); on unless switched off."""
    live = config.get("live") or {}
    return bool(live.get("enabled", True)), float(live.get("radius_nm", DEFAULT_RADIUS_NM)) * NM


@dataclass(slots=True)
class _Plane:
    carrier: int | None = None  # the carrier it's sent with, from its latest sample
    track: list[list[float]] = field(default_factory=list)  # [t, x, y, z, heading, pitch, roll]
    groove: list[list[float | None]] = field(default_factory=list)  # [t, along, lateral, glideslope, aoa]
    last_heading: float | None = None  # relative to the ship's, unwrapped (the page interpolates it)


class LiveFeed:
    """Fed the agent's stream records (`update`, `removed`) and, as a pass listener, samples in a pass
    (`on_sample`); `batch` takes what has built up since the last one."""

    def __init__(self, radius_m: float = DEFAULT_RADIUS_NM * NM, callouts: LiveCallouts | None = None,
                 night_at: Callable[[ObjectTrack, float], bool | None] | None = None) -> None:
        self.radius_m = radius_m
        self.callouts = callouts
        self.night_at = night_at
        self._carriers: dict[int, ObjectTrack] = {}
        self._poses: dict[int, list[tuple[float, CarrierPose]]] = {}  # the last two, as the detector keeps them
        self._planes: dict[int, _Plane] = {}
        self._planes_info: dict[int, ObjectTrack] = {}
        self._calls: list[MadeCall] = []
        self._gone: list[tuple[int, int]] = []  # (carrier, aircraft) no longer around it
        self._carrier_sent: dict[int, float] = {}
        self.now = 0.0
        self.globals: dict[str, str] = {}  # the stream's (ReferenceTime gives the mission's clock); set by the agent
        if callouts is not None:
            callouts.on_made = self._made

    def _made(self, call: MadeCall) -> None:
        self._calls.append(call)

    def _pose(self, carrier_id: int, t: float) -> CarrierPose:
        poses = self._poses[carrier_id]
        return _extrapolate(poses[0], poses[-1], t) if len(poses) == 2 else poses[-1][1]

    def update(self, track: ObjectTrack) -> None:
        """An object moved (its newest sample is the one to send)."""
        if not track.samples:
            return
        sample = track.samples[-1]
        self.now = max(self.now, sample.time)
        if track.name in CARRIERS:
            self._carriers[track.id] = track
            poses = self._poses.setdefault(track.id, [])
            poses.append((sample.time, CarrierPose.from_transform(sample.transform)))
            del poses[:-2]
            return
        if "Air" not in track.tags or not self._carriers:
            return
        t = sample.transform
        if t.u is None or t.v is None or t.alt is None:
            return
        nearest: tuple[float, int, CarrierPose] | None = None
        for carrier_id in self._carriers:
            pose = self._pose(carrier_id, sample.time)
            d = math.hypot(t.u - pose.u, t.v - pose.v)
            if nearest is None or d < nearest[0]:
                nearest = (d, carrier_id, pose)
        assert nearest is not None
        distance, carrier_id, pose = nearest
        info = CARRIERS[self._carriers[carrier_id].name]
        height = t.alt - pose.alt - info.deck_altitude
        plane = self._planes.get(track.id)
        if distance > self.radius_m or height > MAX_ALT_M:
            if plane is not None and plane.carrier is not None:
                self._gone.append((plane.carrier, track.id))  # flew out of the picture
                plane.carrier, plane.last_heading = None, None
            return
        if plane is None:
            plane = self._planes[track.id] = _Plane()
        if plane.carrier != carrier_id:
            if plane.carrier is not None:
                self._gone.append((plane.carrier, track.id))  # now nearer another carrier
            plane.carrier, plane.last_heading = carrier_id, None
        self._planes_info[track.id] = track
        x, z = pose.to_local(t.u, t.v)
        heading = ((t.heading or 0.0) - pose.heading + 180.0) % 360.0 - 180.0
        if plane.last_heading is not None:
            heading = plane.last_heading + (heading - plane.last_heading + 180.0) % 360.0 - 180.0
        plane.last_heading = heading
        plane.track.append([round(sample.time, 3), round(x, 2), round(height, 2), round(z, 2), round(heading, 2),
                            round(t.pitch or 0.0, 2), round(t.roll or 0.0, 2)])

    def removed(self, object_id: int) -> None:
        self._carriers.pop(object_id, None)
        self._poses.pop(object_id, None)
        if (plane := self._planes.pop(object_id, None)) is not None and plane.carrier is not None:
            self._gone.append((plane.carrier, object_id))
        self._planes_info.pop(object_id, None)

    # As a PassListener (beside the callouts): the groove numbers of a jet in a pass.
    def on_sample(self, carrier: ObjectTrack, plane: ObjectTrack, pose: CarrierPose, sample: Sample,
                  frame: DeckFrame) -> None:
        entry = self._planes.get(plane.id)
        if entry is None or entry.carrier != carrier.id:
            return
        pos = frame.position(pose, sample.transform)
        aoa = sample.aoa
        if aoa is None and self.callouts is not None and (state := self.callouts.state_for(carrier.id, plane.id)):
            aoa = state.aoa  # from motion, with the wind
        entry.groove.append([round(sample.time, 3), round(pos.along, 1), round(pos.lateral, 2),
                             round(pos.hook_height - frame.glideslope_height(pos.along), 2),
                             None if aoa is None else round(aoa, 2)])

    def pass_ended(self, carrier_id: int, aircraft_id: int) -> None:
        pass

    def batch(self) -> dict | None:
        """Everything since the last batch, by carrier; None when there's nothing to send."""
        now = self.now
        by_carrier: dict[int, dict] = {}

        def carrier_entry(carrier_id: int) -> dict:
            if carrier_id not in by_carrier:
                carrier = self._carriers[carrier_id]
                by_carrier[carrier_id] = {"id": carrier_id, "unit": carrier.pilot or carrier.name,
                                          "type": carrier.name, **self._carrier_state(carrier),
                                          "aircraft": [], "calls": [], "gone": []}
            return by_carrier[carrier_id]

        for plane_id, plane in self._planes.items():
            if plane.carrier not in self._carriers or not (plane.track or plane.groove):
                plane.track, plane.groove = [], []
                continue
            info = self._planes_info[plane_id]
            carrier_entry(plane.carrier)["aircraft"].append(
                {"id": plane_id, "type": info.name, "pilot": info.pilot or info.name, "track": plane.track,
                 "groove": plane.groove})
            plane.track, plane.groove = [], []
        for call in self._calls:
            plane = self._planes.get(call.aircraft_id)
            if plane is not None and plane.carrier in self._carriers:
                carrier_entry(plane.carrier)["calls"].append({"aircraft": call.aircraft_id, **call.to_dict()})
        self._calls = []
        for carrier_id, plane_id in self._gone:
            if carrier_id in self._carriers:
                carrier_entry(carrier_id)["gone"].append(plane_id)
        self._gone = []
        for carrier_id in self._carriers:
            if carrier_id not in by_carrier and now - self._carrier_sent.get(carrier_id, -math.inf) >= CARRIER_EVERY_S:
                carrier_entry(carrier_id)
        if not by_carrier:
            return None
        for carrier_id in by_carrier:
            self._carrier_sent[carrier_id] = now
        out = {"now": round(now, 3), "carriers": list(by_carrier.values())}
        if (clock := mission_clock(self.globals, now)) is not None:
            out["clock"] = clock
        return out

    def _carrier_state(self, carrier: ObjectTrack) -> dict:
        """The ship's speed (the page moves the sea past it), whether it's night there, and the wind over the deck."""
        out: dict = {}
        if len(carrier.samples) >= 2:
            last = carrier.samples[-1]
            first = next((s for s in reversed(carrier.samples) if last.time - s.time >= SHIP_SPEED_S), None)
            if first is not None:
                a, b = first.transform, last.transform
                out["speed"] = round(math.hypot((b.u or 0.0) - (a.u or 0.0), (b.v or 0.0) - (a.v or 0.0))
                                     / (last.time - first.time), 2)
        if self.night_at is not None and (night := self.night_at(carrier, self.now)) is not None:
            out["night"] = night
        if self.callouts is not None and (wind := self.callouts.deck_wind(carrier)) is not None:
            out["deck_wind_kt"] = round(wind.speed / KT)
        return out


class LiveSender:
    """Posts the feed's batches to the hub (`/api/v1/live`) every SEND_INTERVAL_S, one at a time; drops a batch
    the hub doesn't take."""

    def __init__(self, feed: LiveFeed, client: httpx.AsyncClient, clock: Callable[[], float]) -> None:
        self.feed = feed
        self.client = client
        self.clock = clock  # monotonic seconds
        self._refused_until = -math.inf
        self._failing: str | None = None  # why the last update failed
        self._sent_at = -math.inf  # when the hub last took an update

    async def send_once(self) -> bool:
        batch = self.feed.batch()
        if batch is None and self.clock() - self._sent_at >= HEARTBEAT_S:
            batch = {"now": round(self.feed.now, 3), "carriers": []}  # still here
        if batch is None or self.clock() < self._refused_until:
            return False
        try:
            r = await self.client.post("/api/v1/live", json=batch, timeout=2.0)
        except httpx.HTTPError as exc:
            self._failed(f"hub not taking updates ({exc or type(exc).__name__})")
            return False
        if r.status_code in (404, 405):
            log.info("live view: the hub doesn't have it (%s); trying again in %.0f min", r.status_code,
                     REFUSED_BACKOFF_S / 60)
            self._refused_until = self.clock() + REFUSED_BACKOFF_S
            return False
        if r.status_code != 200:
            self._failed(f"hub refused an update ({r.status_code} {r.text[:200]})")
            return False
        if self._failing is not None:
            log.info("live view: hub taking updates again")
        self._failing = None
        self._sent_at = self.clock()
        return True

    def _failed(self, why: str) -> None:
        """Logged once per kind of failure (not four times a second), and again when the reason changes."""
        if why != self._failing:
            log.warning("live view: %s; dropping updates meanwhile", why)
        self._failing = why
