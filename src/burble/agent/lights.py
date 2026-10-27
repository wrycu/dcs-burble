"""The carrier's landing lights for a Case III recovery (PLAN #54).

At night DCS's Supercarrier keeps its deck dark unless a pilot goes through its own carrier ATC. The agent decides,
per carrier, when its landing lights should be on: it's Case III there (the recovery case as the live calls decide
it: forced in the config, or auto by night or the mission's weather) and an aircraft is recovering to it (within
`radius_nm` and 5,000 ft above its deck, astern of it, nose towards it). They stay on for `hold_s` after the last
one, for a bolter or the next jet in. Only the carrier that aircraft is recovering to (the nearest) is lit.

The agent writes what it wants to a file next to dcs.log (`LIGHTS_FILE`, every few seconds while it runs); the
server hook reads it and sets those carriers to AC RECOVERY, and back as the mission had them (the last mode its own
triggers set, else AUTO) when they're no longer wanted, or when the file goes stale (the agent stopped). Configured in the agent's config on the hub (`lights`), on by default.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from pathlib import Path

from ..acmi import ObjectTrack
from ..geometry import AIRCRAFT, CARRIERS, CarrierPose

log = logging.getLogger(__name__)

NM = 1852.0
FT = 0.3048
LIGHTS_FILE = "burble-lights.json"  # in DCS's Logs folder (the hook reads it from there)
DEFAULT_RADIUS_NM = 10.0
DEFAULT_HOLD_S = 120.0
MAX_HEIGHT_M = 5000 * FT  # above the deck
AIRBORNE_M = 15.0  # above the deck: not on it
ASTERN_COS = math.cos(math.radians(45.0))  # within this of dead astern
NOSE_TOWARDS_COS = math.cos(math.radians(60.0))  # heading within this of the carrier's bearing
# The file is rewritten this often (wall clock) even when nothing changed: the hook takes a file older than its
# LIGHTS_STALE_S (30 s) as the agent having stopped.
WRITE_EVERY_S = 5.0


def lights_settings(config: dict) -> tuple[bool, float, float]:
    """(enabled, radius in meters, hold in seconds) from the agent's config on the hub (`lights`)."""
    lights = config.get("lights") or {}
    return (bool(lights.get("enabled", True)), float(lights.get("radius_nm", DEFAULT_RADIUS_NM)) * NM,
            float(lights.get("hold_s", DEFAULT_HOLD_S)))


def recovering(pose: CarrierPose, deck_altitude: float, t) -> bool:
    """Is an aircraft here (a Tacview transform) recovering to this carrier: airborne, below 5,000 ft above its
    deck, astern of it, and pointed at it? (The distance is checked by the caller.)"""
    if t.u is None or t.v is None or t.alt is None:
        return False
    height = t.alt - pose.alt - deck_altitude
    if not AIRBORNE_M <= height <= MAX_HEIGHT_M:
        return False
    du, dv = pose.u - t.u, pose.v - t.v  # towards the carrier
    distance = math.hypot(du, dv)
    if distance < 1.0:
        return False
    h = math.radians(pose.heading)
    if (du * math.sin(h) + dv * math.cos(h)) / distance < ASTERN_COS:
        return False  # not astern (astern, the way to the carrier is along its forward axis)
    nose = math.radians(t.heading or 0.0)
    return (du * math.sin(nose) + dv * math.cos(nose)) / distance >= NOSE_TOWARDS_COS


class CarrierLights:
    """Fed the agent's stream records (`update`, `removed`) like the live view; `write` keeps the hook's file up to
    date (call it often: it writes only on a change or every WRITE_EVERY_S)."""

    def __init__(self, path: Path, case_iii: Callable[[ObjectTrack, float], bool],
                 radius_m: float = DEFAULT_RADIUS_NM * NM, hold_s: float = DEFAULT_HOLD_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.case_iii = case_iii
        self.radius_m = radius_m
        self.hold_s = hold_s
        self.clock = clock
        self._carriers: dict[int, ObjectTrack] = {}
        # Carrier id -> (mission time an aircraft was last seen recovering to it, who).
        self._recovering: dict[int, tuple[float, str]] = {}
        self._lit: set[str] = set()  # carriers (unit names) last written as wanted
        self._written_at: float | None = None
        self.now = 0.0

    def update(self, track: ObjectTrack) -> None:
        """An object moved."""
        if not track.samples:
            return
        sample = track.samples[-1]
        self.now = max(self.now, sample.time)
        if track.name in CARRIERS:
            self._carriers[track.id] = track
            return
        if track.name not in AIRCRAFT or not self._carriers:
            return
        t = sample.transform
        if t.u is None or t.v is None:
            return
        nearest: tuple[float, ObjectTrack, CarrierPose] | None = None
        for carrier in self._carriers.values():
            pose = CarrierPose.from_transform(carrier.samples[-1].transform)
            d = math.hypot(t.u - pose.u, t.v - pose.v)
            if nearest is None or d < nearest[0]:
                nearest = (d, carrier, pose)
        assert nearest is not None
        distance, carrier, pose = nearest
        if distance <= self.radius_m and recovering(pose, CARRIERS[carrier.name].deck_altitude, t):
            self._recovering[carrier.id] = (sample.time, track.pilot or hex(track.id))

    def removed(self, object_id: int) -> None:
        self._carriers.pop(object_id, None)
        self._recovering.pop(object_id, None)

    def wanted(self) -> dict[str, str]:
        """The carriers (by unit name) whose landing lights should be on now: {"CVN-75 ...": "recovery"}."""
        out: dict[str, str] = {}
        for carrier_id, (at, _) in self._recovering.items():
            carrier = self._carriers.get(carrier_id)
            if carrier is not None and carrier.pilot and self.now - at <= self.hold_s \
                    and self.case_iii(carrier, self.now):
                out[carrier.pilot] = "recovery"
        return out

    def write(self) -> None:
        wanted = self.wanted()
        now = self.clock()
        if set(wanted) == self._lit and self._written_at is not None and now - self._written_at < WRITE_EVERY_S:
            return
        for name in sorted(set(wanted) - self._lit):
            who = next((w for cid, (_, w) in self._recovering.items()
                        if (c := self._carriers.get(cid)) is not None and c.pilot == name), "?")
            log.info("carrier lights: landing lights on for %s (Case III, %s recovering)", name, who)
        for name in sorted(self._lit - set(wanted)):
            log.info("carrier lights: back as the mission had them for %s (no aircraft recovering, or not Case III)",
                     name)
        self._save(wanted, now)

    def close(self) -> None:
        """The session ended: nothing is wanted (the hook puts any lit carrier back as the mission had it)."""
        if self._written_at is not None:
            for name in sorted(self._lit):
                log.info("carrier lights: back as the mission had them for %s (session ended)", name)
            self._save({}, self.clock())

    def _save(self, wanted: dict[str, str], now: float) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"at": int(now), "carriers": wanted}) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("carrier lights: could not write %s: %s", self.path, exc)
            return
        self._lit, self._written_at = set(wanted), now
