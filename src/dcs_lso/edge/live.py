"""Live pass detection on the Tacview stream.

Feeds parser records into per-object tracks and runs a `PassTracker` per
(carrier, aircraft) pair, one sample at a time. Live detection only decides *when* a
pass happened and who flew it; the pass is then sliced from the session archive and
analysed with the same offline code as everything else.
"""

from __future__ import annotations

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


# Keep this much history per object (slices reach back 30 s before detection, and
# nearby-aircraft checks need the whole window).
HISTORY_S = 600.0


class LivePassDetector:
    def __init__(self) -> None:
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
            entry = self._trackers.get(key)
            if entry is None:
                if not is_recovery_attempt(pose, sample.transform):
                    continue
                carrier = self.tracks[carrier_id]
                frame = DeckFrame(CARRIERS[carrier.name], AIRCRAFT[plane.name])
                entry = self._trackers[key] = (PassTracker(frame), frame)
            tracker, _ = entry
            if not tracker.feed(sample.time, pose, sample.transform, sample.aoa):
                finished.extend(self._finish(key))
        return finished

    def _finish(self, key: tuple[int, int]) -> list[PassResult]:
        tracker, frame = self._trackers.pop(key)
        carrier, plane = self.tracks.get(key[0]), self.tracks.get(key[1])
        if carrier is None or plane is None:
            return []
        result = _finish(carrier, plane, frame, tracker)
        return [result] if result else []
