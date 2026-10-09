"""Hearing an SRS frequency (PLAN #20): incoming voice packets put together into whole transmissions, one per
speaker, each decoded to 16 kHz PCM with who sent it (their SRS client: name and DCS unit).

A transmission ends when its speaker's packets stop for END_GAP_S (the push-to-talk released).
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field

from .opus import Decoder, SAMPLE_RATE
from .packet import VoicePacket

END_GAP_S = 0.4
MAX_TRANSMISSION_S = 20.0  # a stuck microphone: cut into pieces this long


@dataclass
class Transmission:
    client_guid: str
    name: str  # the SRS client's name (a DCS player's name), or "" if the server hasn't told us yet
    unit_id: int  # the DCS unit the speaker is in (0: none)
    started: float  # perf_counter time of the first packet
    ended: float  # ... and the last
    frequency_hz: float
    pcm: array = field(default_factory=lambda: array("h"))

    @property
    def seconds(self) -> float:
        return len(self.pcm) / SAMPLE_RATE


class Transmissions:
    """Feed it every voice packet (`add`, from `SrsClient.on_voice`); `finished(now)` returns the transmissions
    that have ended. `clients`: the SRS client list (`SrsClient.clients`), for speakers' names. Our own
    transmissions (`own_guid`) are left out."""

    def __init__(self, clients: dict[str, dict] | None = None, own_guid: str | None = None) -> None:
        self.clients = clients if clients is not None else {}
        self.own_guid = own_guid
        self._open: dict[str, tuple[Transmission, Decoder]] = {}

    def add(self, packet: VoicePacket, at: float) -> None:
        if packet.client_guid == self.own_guid or not packet.audio:
            return
        entry = self._open.get(packet.client_guid)
        if entry is None:
            client = self.clients.get(packet.client_guid) or {}
            t = Transmission(packet.client_guid, str(client.get("Name") or ""), packet.unit_id, at, at,
                             packet.frequencies[0] if packet.frequencies else 0.0)
            entry = self._open[packet.client_guid] = (t, Decoder())
        t, decoder = entry
        try:
            t.pcm.extend(decoder.decode(packet.audio))
        except Exception:  # a damaged packet: skip it, keep the rest
            return
        t.ended = at
        if not t.name:
            t.name = str((self.clients.get(packet.client_guid) or {}).get("Name") or "")

    def finished(self, now: float) -> list[Transmission]:
        done = []
        for guid, (t, _) in list(self._open.items()):
            if now - t.ended >= END_GAP_S or t.ended - t.started >= MAX_TRANSMISSION_S:
                done.append(t)
                del self._open[guid]
        return sorted(done, key=lambda t: t.started)
