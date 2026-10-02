"""Approaches found from an aircraft's own track alone, with no carrier in the data.

A multiplayer client's Tacview often records only the player's own jet (servers commonly don't
let clients export other objects), so passes can't be detected against a carrier. The pilot's
track is still the best one available (higher rate, real AOA), so the pilot-mode collector uploads
it around each approach and central grades it against the carrier from the server's report of the
same landing (see `central.service`).

An approach: the jet comes down from pattern altitude to near deck height, and ends when it climbs
away again (bolter, wave-off, touch-and-go) or stops (trap).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from ..acmi import Recording, Sample

ARMED_ABOVE_M = 170.0  # back above this (the 600 ft pattern is 183 m): ready for the next approach
APPROACH_BELOW_M = 120.0  # an approach starts below this (~3/4 nm on glideslope)
DECK_BELOW_M = 60.0  # ...and only counts if the jet gets this low (deck height is ~20 m)
STOPPED_SPEED_MS = 3.0
STOPPED_DURATION_S = 5.0


@dataclass(frozen=True, slots=True)
class Approach:
    aircraft_id: int
    start_time: float  # first sample below APPROACH_BELOW_M
    end_time: float  # climbed away, stopped, or the track ended
    min_alt: float


class ApproachSegmenter:
    """Feed one aircraft's samples in order; returns each approach as it ends."""

    def __init__(self, aircraft_id: int) -> None:
        self.aircraft_id = aircraft_id
        self._armed = False
        self._start: float | None = None
        self._min_alt = math.inf
        self._last: Sample | None = None
        self._stopped_since: float | None = None

    def feed(self, sample: Sample) -> Approach | None:
        t = sample.transform
        alt = t.alt if t.alt is not None else math.inf
        last, self._last = self._last, sample
        if self._start is None:
            if alt > ARMED_ABOVE_M:
                self._armed = True
            elif self._armed and alt < APPROACH_BELOW_M:
                self._start, self._min_alt, self._stopped_since = sample.time, alt, None
            return None
        self._min_alt = min(self._min_alt, alt)
        if alt > ARMED_ABOVE_M:
            return self._end(sample.time, armed=True)  # climbed away: ready for the next one
        if last is not None and sample.time > last.time:
            speed = math.hypot((t.u or 0.0) - (last.transform.u or 0.0),
                               (t.v or 0.0) - (last.transform.v or 0.0)) / (sample.time - last.time)
            if speed < STOPPED_SPEED_MS:
                if self._stopped_since is None:
                    self._stopped_since = sample.time
                if sample.time - self._stopped_since >= STOPPED_DURATION_S:
                    return self._end(sample.time, armed=False)  # on deck: must take off again first
            else:
                self._stopped_since = None
        return None

    def flush(self) -> Approach | None:
        """The track ended (despawned, or the stream closed)."""
        return self._end(self._last.time, armed=False) if self._start is not None and self._last else None

    def _end(self, time: float, armed: bool) -> Approach | None:
        start, low = self._start, self._min_alt
        self._start, self._min_alt, self._stopped_since, self._armed = None, math.inf, None, armed
        if start is None or low > DECK_BELOW_M:
            return None
        return Approach(self.aircraft_id, start, time, low)


def find_approaches(recording: Recording, aircraft_id: int) -> Iterator[Approach]:
    track = recording.objects[aircraft_id]
    segmenter = ApproachSegmenter(aircraft_id)
    for sample in track.samples:
        if (found := segmenter.feed(sample)) is not None:
            yield found
    if (found := segmenter.flush()) is not None:
        yield found
