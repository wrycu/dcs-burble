"""Recording the carrier frequency's radio for the replay (PLAN #52): what the server agent's SRS connection hears,
kept as SRS sends it (40 ms Opus frames), and what the LSO itself said, each transmission stamped with mission time.
A pass's upload carries the transmissions on its carrier's frequency from its own pilot and the LSO.

Switched on per server in the hub's configuration (`callouts.record_radio`, off by default). A player whose SRS client
doesn't allow recording ("Allow Transmissions To Be Recorded" off: `AllowRecord` in their client) is never recorded,
as SRS's own recorder leaves them out.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ..srs.client import Radio
from ..srs.listen import END_GAP_S
from ..srs.opus import FRAME_MS, SAMPLE_RATE, pack_frames
from ..srs.packet import VoicePacket

log = logging.getLogger(__name__)

KEEP_S = 300.0  # mission seconds of radio kept for passes still to be sliced (lead, pass, and tail are under 2 min)
SAME_FREQUENCY_HZ = 1000.0


@dataclass
class Recorded:
    time: float  # mission time of the first frame
    frequency_hz: float
    by: str  # "pilot" (a player's SRS client) or "lso" (ours)
    name: str  # the SRS client's name ("" for the LSO)
    frames: list[bytes] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return len(self.frames) * FRAME_MS / 1000


class RadioRecorder:
    """`clock`: the mission time now (the Tacview stream's newest frame, carried on by the wall clock)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.recorded: list[Recorded] = []
        self._open: dict[str, tuple[Recorded, float]] = {}  # SRS client -> (its transmission, perf time of its last packet)
        self._sink = None
        self._not_allowed: set[str] = set()  # logged once each

    def attach(self, sink) -> None:
        """Record what `sink` (an SrsSink) hears and says (after any listener already attached to it)."""
        self._sink = sink
        heard = sink.on_voice

        def on_voice(packet: VoicePacket, at: float) -> None:
            if heard is not None:
                heard(packet, at)
            self.heard(packet, at)

        sink.on_voice = on_voice
        sink.on_transmit = self.saying

    def heard(self, packet: VoicePacket, at: float) -> None:
        client = self._sink.client if self._sink is not None else None
        if client is None or packet.client_guid == client.guid or not packet.audio or not packet.frequencies:
            return
        info = client.clients.get(packet.client_guid) or {}
        name = str(info.get("Name") or "")
        if not info.get("AllowRecord"):  # their SRS client doesn't allow it (or we don't know them yet)
            if name not in self._not_allowed:
                self._not_allowed.add(name)
                log.info("not recording %s: their SRS client doesn't allow recording", name or packet.client_guid)
            return
        frequency = packet.frequencies[0]
        entry = self._open.get(packet.client_guid)
        if entry is None or at - entry[1] >= END_GAP_S or abs(entry[0].frequency_hz - frequency) >= SAME_FREQUENCY_HZ:
            self._keep(Recorded(self.clock(), frequency, "pilot", name))
            entry = (self.recorded[-1], at)
        entry[0].frames.append(packet.audio)
        self._open[packet.client_guid] = (entry[0], at)

    def saying(self, radio: Radio) -> list[bytes]:
        """The LSO starts a transmission: the list to put its frames in as they go out (a call cut off by a
        wave-off keeps only what was sent)."""
        recorded = Recorded(self.clock(), radio.frequency_hz, "lso", "")
        self._keep(recorded)
        return recorded.frames

    def _keep(self, recorded: Recorded) -> None:
        self.recorded.append(recorded)
        oldest = recorded.time - KEEP_S
        self.recorded = [r for r in self.recorded if r.time >= oldest]
        self._open = {g: e for g, e in self._open.items() if e[0].time >= oldest}

    def for_pass(self, pilot: str | None, frequency_hz: float, start: float, end: float) -> list[Recorded]:
        """The transmissions on this frequency between `start` and `end` (mission time) from the pass's pilot (by
        their SRS name, which is their DCS name) and the LSO; nobody else heard on the frequency."""
        who = _norm(pilot)
        return [r for r in self.recorded if r.frames and abs(r.frequency_hz - frequency_hz) < SAME_FREQUENCY_HZ
                and start <= r.time + r.seconds and r.time <= end and (r.by == "lso" or (who and _norm(r.name) == who))]


def radio_sidecar(recorded: list[Recorded]) -> dict:
    """For the pass's sidecar (`radio`): each transmission's frames, length-prefixed and base64'd."""
    return {"codec": "opus", "sample_rate": SAMPLE_RATE, "frame_ms": FRAME_MS,
            "transmissions": [{"time": round(r.time, 3), "by": r.by, **({"name": r.name} if r.name else {}),
                               "frames": base64.b64encode(pack_frames(r.frames)).decode("ascii")} for r in recorded]}


def _norm(name: str | None) -> str:
    return " ".join((name or "").lower().split())
