"""Live pass detection on the Tacview stream.

Feeds parser records into per-object tracks and runs a `PassTracker` per
(carrier, aircraft) pair, one sample at a time. Live detection only decides *when* a
pass happened and who flew it; the pass is then sliced from the session archive and
analysed with the same offline code as everything else.
"""

from __future__ import annotations

import math

from typing import Protocol

from ..acmi import ObjectRemoved, ObjectTrack, ObjectUpdate, Record, Recording, Sample
from ..detect import PassResult, PassTracker, is_recovery_attempt
from ..detect.passes import _finish
from ..geometry import AIRCRAFT, CARRIERS, CarrierPose, DeckFrame

def _extrapolate(a: tuple[float, CarrierPose], b: tuple[float, CarrierPose], t: float) -> CarrierPose:
    """Carrier pose at `t` from its last two samples (ships move steadily between updates)."""
    (ta, pa), (tb, pb) = a, b
    if tb <= ta:
        return pb
    f = (t - tb) / (tb - ta)
    dh = (pb.heading - pa.heading + 180.0) % 360.0 - 180.0
    return CarrierPose(u=pb.u + (pb.u - pa.u) * f, v=pb.v + (pb.v - pa.v) * f, alt=pb.alt,
                       heading=(pb.heading + dh * f) % 360.0)


# The landing area for "foul deck": from the ramp to the forward end of the angled deck (per carrier,
# CarrierInfo), with an aircraft's hook within this height of the deck; samples older than
# FOUL_DECK_STALE_S are ignored (the aircraft left).
ON_DECK_HEIGHT_M = 3.0
FOUL_DECK_STALE_S = 3.0

# Seen sitting on a carrier's deck (inside the ship's footprint, at deck height, slower over the map than any
# aircraft flies: on deck the jet moves with the ship): it departed from that carrier, so a later trap there is
# "welcome home". Only counts this long before the trap (not the rollout of the trap being welcomed).
DECK_HALF_WIDTH_M, DECK_HALF_LENGTH_M = 45.0, 170.0
PARKED_SPEED_MS = 25.0
DEPARTED_BEFORE_S = 120.0

# Keep this much history per object (slices reach back 30 s before detection, and
# nearby-aircraft checks need the whole window).
HISTORY_S = 600.0


class PassListener(Protocol):
    """Told about every sample of an aircraft in a pass (e.g. live callouts)."""

    def on_sample(self, carrier: ObjectTrack, plane: ObjectTrack, pose: CarrierPose, sample: Sample,
                  frame: DeckFrame) -> object: ...

    def pass_ended(self, carrier_id: int, aircraft_id: int) -> None: ...


class LivePassDetector:
    def __init__(self, listener: PassListener | None = None) -> None:
        self.listener = listener
        self._on_deck: dict[tuple[int, int], float] = {}  # (carrier, aircraft) -> first seen sitting on its deck
        self.tracks: dict[int, ObjectTrack] = {}
        self._trackers: dict[tuple[int, int], tuple[PassTracker, DeckFrame]] = {}
        # Last two (time, pose) samples per carrier, for extrapolating to aircraft sample times.
        self._carrier_pose: dict[int, list[tuple[float, CarrierPose]]] = {}
        self.time = 0.0

    def recording(self, globals_: dict[str, str]) -> Recording:
        """A `Recording` view of the recent history (for nearby-object checks)."""
        return Recording(globals_, self.tracks)

    def feed(self, record: Record) -> list[PassResult]:
        finished: list[PassResult] = []
        if isinstance(record, ObjectUpdate):
            self.time = record.time
            track = self.tracks.get(record.id)
            if track is None:
                track = self.tracks[record.id] = ObjectTrack(record.id)
            track.props = record.props
            if record.moved:
                aoa = record.props.get("AOA")
                sample = Sample(record.time, record.transform, float(aoa) if aoa else None)
                track.samples.append(sample)
                if len(track.samples) > 64 and record.time - track.samples[0].time > HISTORY_S * 1.5:
                    track.samples = [s for s in track.samples if record.time - s.time <= HISTORY_S]
                if track.name in CARRIERS:
                    poses = self._carrier_pose.setdefault(track.id, [])
                    poses.append((record.time, CarrierPose.from_transform(record.transform)))
                    del poses[:-2]
                elif track.name in AIRCRAFT:
                    finished.extend(self._aircraft_sample(track, sample))
        elif isinstance(record, ObjectRemoved):
            for key in [k for k in self._trackers if record.id in k]:
                finished.extend(self._finish(key))
            self._carrier_pose.pop(record.id, None)
        return finished

    def flush(self) -> list[PassResult]:
        """Finish every pass in progress (the stream ended)."""
        out: list[PassResult] = []
        for key in list(self._trackers):
            out.extend(self._finish(key))
        return out

    def _aircraft_sample(self, plane: ObjectTrack, sample: Sample) -> list[PassResult]:
        finished: list[PassResult] = []
        for carrier_id, poses in self._carrier_pose.items():
            pose = _extrapolate(poses[0], poses[-1], sample.time) if len(poses) == 2 else poses[-1][1]
            key = (carrier_id, plane.id)
            if key not in self._on_deck and self._sitting_on_deck(carrier_id, pose, plane, sample):
                self._on_deck[key] = sample.time
            entry = self._trackers.get(key)
            if entry is None:
                if not is_recovery_attempt(pose, sample.transform):
                    continue
                carrier = self.tracks[carrier_id]
                frame = DeckFrame(CARRIERS[carrier.name], AIRCRAFT[plane.name])
                entry = self._trackers[key] = (PassTracker(frame), frame)
            tracker, frame = entry
            if not tracker.feed(sample.time, pose, sample.transform, sample.aoa):
                finished.extend(self._finish(key))
            elif self.listener is not None:
                self.listener.on_sample(self.tracks[carrier_id], plane, pose, sample, frame)
        return finished

    def _sitting_on_deck(self, carrier_id: int, pose: CarrierPose, plane: ObjectTrack, sample: Sample) -> bool:
        t = sample.transform
        if t.u is None or t.v is None or t.alt is None or len(plane.samples) < 2:
            return False
        x, z = pose.to_local(t.u, t.v)
        deck = pose.alt + CARRIERS[self.tracks[carrier_id].name].deck_altitude
        if abs(x) > DECK_HALF_WIDTH_M or abs(z) > DECK_HALF_LENGTH_M or abs(t.alt - deck) > ON_DECK_HEIGHT_M:
            return False
        earlier = next((s for s in reversed(plane.samples[:-1]) if sample.time - s.time >= 0.5), None)
        if earlier is None or earlier.transform.u is None or earlier.transform.v is None:
            return False
        speed = math.hypot(t.u - earlier.transform.u, t.v - earlier.transform.v) / (sample.time - earlier.time)
        return speed < PARKED_SPEED_MS

    def departed_from(self, carrier_id: int, aircraft_id: int, before: float) -> bool:
        """Was this aircraft sitting on this carrier's deck earlier in the mission (it launched from it)?"""
        seen = self._on_deck.get((carrier_id, aircraft_id))
        return seen is not None and seen <= before - DEPARTED_BEFORE_S

    def landing_area_foul(self, carrier_id: int, pose: CarrierPose, frame: DeckFrame, exclude: int, now: float) -> bool:
        """Is another aircraft on deck in the landing area right now (from the ramp to the forward end
        of the angled deck, within the wires' width)?"""
        half_width = max(abs(lateral) for ends in frame.wire_ends for _, lateral in ends)
        for track in self.tracks.values():
            if track.id in (exclude, carrier_id) or "Air" not in track.tags or not track.samples:
                continue
            last = track.samples[-1]
            if now - last.time > FOUL_DECK_STALE_S:
                continue
            pos = frame.position(pose, last.transform)
            if (frame.carrier.landing_area_forward_m <= pos.along <= frame.carrier.ramp_along_m and abs(pos.lateral) <= half_width
                    and pos.hook_height <= ON_DECK_HEIGHT_M):
                return True
        return False

    def provisional(self, carrier_id: int, aircraft_id: int) -> PassResult | None:
        """The pass in progress as it stands now (e.g. graded for the welcome, before it has ended)."""
        entry = self._trackers.get((carrier_id, aircraft_id))
        carrier, plane = self.tracks.get(carrier_id), self.tracks.get(aircraft_id)
        if entry is None or carrier is None or plane is None:
            return None
        tracker, frame = entry
        return _finish(carrier, plane, frame, tracker)

    def _finish(self, key: tuple[int, int]) -> list[PassResult]:
        tracker, frame = self._trackers.pop(key)
        if self.listener is not None:
            self.listener.pass_ended(*key)
        carrier, plane = self.tracks.get(key[0]), self.tracks.get(key[1])
        if carrier is None or plane is None:
            return []
        result = _finish(carrier, plane, frame, tracker)
        return [result] if result else []
