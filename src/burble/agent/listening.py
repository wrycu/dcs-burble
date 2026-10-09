"""Listening to pilots on the LSO frequencies (PLAN #20): what the server agent's SRS connection hears, made into
transmissions, recognised (callouts/heard.py), and handed to the live callouts to answer ("Roger ball").

Switched on per server in the hub's configuration (`callouts.listen`), with a Vosk model on the agent's machine
(`--listen-model`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..callouts.heard import PilotCall, Recogniser
from ..srs.listen import Transmissions
from ..srs.packet import VoicePacket

log = logging.getLogger(__name__)

POLL_S = 0.1


@dataclass(frozen=True, slots=True)
class Heard:
    call: PilotCall
    speaker: str  # the SRS client's name (a DCS player's), or ""
    unit_id: int  # the speaker's DCS unit
    frequency_hz: float
    at: float  # time.monotonic() when the transmission ended


class Listener:
    def __init__(self, recogniser: Recogniser, on_call: Callable[[Heard], None]) -> None:
        self.recogniser = recogniser
        self.on_call = on_call
        self.transmissions = Transmissions()
        self._sink = None

    def attach(self, sink) -> None:
        """Hear what `sink` (an SrsSink) receives."""
        self._sink = sink
        sink.on_voice = self._on_voice

    def _on_voice(self, packet: VoicePacket, at: float) -> None:
        client = self._sink.client if self._sink is not None else None
        if client is not None:  # (after a reconnect: the new connection's client list and our new id)
            self.transmissions.clients, self.transmissions.own_guid = client.clients, client.guid
        self.transmissions.add(packet, at)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_S)
            for t in self.transmissions.finished(time.perf_counter()):
                try:
                    call = await loop.run_in_executor(None, self.recogniser.hear, t.pcm)
                except Exception:  # never let one transmission stop the listening
                    log.exception("recognising a transmission failed")
                    continue
                log.info("HEARD %s on %.3f MHz (%.1f s): %r -> %s", t.name or t.client_guid, t.frequency_hz / 1e6,
                         t.seconds, call.text, call.call or "not a call")
                if call.call is not None:
                    self.on_call(Heard(call, t.name, t.unit_id, t.frequency_hz, time.monotonic()))
