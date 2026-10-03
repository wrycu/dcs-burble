"""Persistent native SRS client: joins an SRS server, stays connected, transmits on demand.

Protocol per SRS 2.3.8.2 (github.com/ciribob/DCS-SimpleRadioStandalone):
- TCP: newline-delimited JSON `NetworkMessage`s. The client sends SYNC (with its radio
  state) and RADIO_UPDATE; the server replies with SYNC (clients, settings) and multicasts
  other clients' updates.
- UDP (same port): the client sends its 22-byte GUID as a ping every 15 s; the server
  echoes it and records the client's UDP address. Voice packets (see `packet.py`) carry
  40 ms Opus frames and are relayed to clients whose radios match.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .opus import FRAME_MS
from .packet import GUID_LENGTH, Modulation, VoicePacket, short_guid

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2.3.8.2"
DEFAULT_PORT = 5002
MAX_RADIOS = 11  # radio 0 is intercom
_MSG_UPDATE, _MSG_PING, _MSG_SYNC, _MSG_RADIO_UPDATE = 0, 1, 2, 3
_MSG_CLIENT_DISCONNECT, _MSG_VERSION_MISMATCH = 5, 6
UDP_PING_INTERVAL_S = 15.0
RADIO_UPDATE_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class Radio:
    frequency_mhz: float
    modulation: Modulation = Modulation.AM

    @property
    def frequency_hz(self) -> float:
        return self.frequency_mhz * 1e6


VoiceCallback = Callable[[VoicePacket, float], None]


class SrsError(ConnectionError):
    pass


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: SrsClient) -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr: object) -> None:
        now = time.perf_counter()
        if len(data) == GUID_LENGTH:
            self.client._udp_ready.set()
        elif len(data) > GUID_LENGTH and self.client.on_voice is not None:
            try:
                self.client.on_voice(VoicePacket.decode(data), now)
            except (ValueError, UnicodeDecodeError):
                log.debug("dropping malformed voice packet (%d bytes)", len(data))

    def error_received(self, exc: Exception) -> None:
        log.warning("SRS UDP error: %s", exc)


@dataclass
class SrsClient:
    host: str
    port: int = DEFAULT_PORT
    name: str = "DCS-LSO"
    coalition: int = 2  # 0 spectator, 1 red, 2 blue
    radios: Sequence[Radio] = (Radio(251.0),)
    unit_id: int = 100000
    on_voice: VoiceCallback | None = None
    guid: str = field(default_factory=short_guid)

    server_version: str | None = field(default=None, init=False)
    server_settings: dict[str, str] = field(default_factory=dict, init=False)
    clients: dict[str, dict] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.radios) < MAX_RADIOS:
            raise ValueError(f"between 1 and {MAX_RADIOS - 1} radios")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._udp_ready = asyncio.Event()
        self._synced = asyncio.Event()
        self._closed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._tx_lock = asyncio.Lock()
        self._packet_number = 1

    async def set_radios(self, radios: Sequence[Radio]) -> None:
        """Change the radios this client announces (sent to the server straight away when connected)."""
        if not 1 <= len(radios) < MAX_RADIOS:
            raise ValueError(f"between 1 and {MAX_RADIOS - 1} radios")
        self.radios = tuple(radios)
        if self._writer is not None:
            await self._send(_MSG_RADIO_UPDATE)

    # -- connection -------------------------------------------------------------------------

    async def connect(self, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout)
        self._tasks.append(asyncio.create_task(self._read_tcp(), name="srs-tcp"))
        await self._send(_MSG_SYNC)
        try:
            await asyncio.wait_for(self._synced.wait(), timeout)
        except TimeoutError as exc:
            raise SrsError("no SYNC reply from the SRS server") from exc
        await self._send(_MSG_RADIO_UPDATE)

        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self), remote_addr=(self.host, self.port))
        self._tasks.append(asyncio.create_task(self._ping_udp(), name="srs-udp-ping"))
        try:
            await asyncio.wait_for(self._udp_ready.wait(), timeout)
        except TimeoutError as exc:
            raise SrsError("SRS server did not answer the UDP ping (UDP blocked?)") from exc
        self._tasks.append(asyncio.create_task(self._keep_radio_state(), name="srs-radio-update"))
        log.info("connected to SRS %s:%d (server %s) as %s", self.host, self.port, self.server_version, self.guid)

    async def close(self) -> None:
        self._closed.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._udp is not None:
            self._udp.close()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except ConnectionError:
                pass

    async def __aenter__(self) -> SrsClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- transmitting ---------------------------------------------------------------------

    async def transmit(self, frames: Sequence[bytes], radios: Sequence[Radio] | None = None,
                       on_sent: Callable[[int, float], None] | None = None) -> None:
        """Send Opus frames in real time (one per 40 ms). Transmissions queue behind each other."""
        if self._udp is None or not self._udp_ready.is_set():
            raise SrsError("not connected")
        radios = tuple(radios or self.radios)
        async with self._tx_lock:
            loop = asyncio.get_running_loop()
            start = loop.time()
            for i, frame in enumerate(frames):
                delay = start + i * FRAME_MS / 1000 - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                number = self._packet_number
                self._packet_number += 1
                packet = VoicePacket(
                    audio=frame,
                    frequencies=tuple(r.frequency_hz for r in radios),
                    modulations=tuple(int(r.modulation) for r in radios),
                    encryptions=tuple(0 for _ in radios),
                    unit_id=self.unit_id,
                    packet_number=number,
                    client_guid=self.guid,
                )
                self._udp.sendto(packet.encode())
                if on_sent is not None:
                    on_sent(number, time.perf_counter())

    # -- internals ------------------------------------------------------------------------

    def _client_state(self) -> dict:
        disabled = {"enc": False, "encKey": 0, "freq": 1.0, "modulation": int(Modulation.DISABLED),
                    "retransmit": False, "secFreq": 1.0}
        radios = [dict(disabled) for _ in range(MAX_RADIOS)]
        for i, radio in enumerate(self.radios, start=1):
            radios[i].update(freq=radio.frequency_hz, modulation=int(radio.modulation))
        return {
            "ClientGuid": self.guid,
            "Name": self.name,
            "Coalition": self.coalition,
            "AllowRecord": True,
            "Seat": 0,
            "RadioInfo": {"radios": radios, "unit": self.name, "unitId": self.unit_id,
                          "ambient": {"abType": "", "vol": 0.0}},
            "LatLngPosition": {"lat": 0.0, "lng": 0.0, "alt": 0.0},
        }

    async def _send(self, msg_type: int) -> None:
        assert self._writer is not None
        message = {"Client": self._client_state(), "MsgType": msg_type, "Version": PROTOCOL_VERSION}
        self._writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def _read_tcp(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("unparseable SRS message: %r", line[:200])
                    continue
                self._handle(message)
        finally:
            if not self._closed.is_set():
                log.warning("SRS server closed the TCP connection")
            self._closed.set()

    def _handle(self, message: dict) -> None:
        kind = message.get("MsgType")
        if kind == _MSG_SYNC:
            self.server_version = message.get("Version")
            self.server_settings = message.get("ServerSettings") or {}
            self.clients = {c["ClientGuid"]: c for c in message.get("Clients") or [] if c.get("ClientGuid")}
            self._synced.set()
        elif kind in (_MSG_UPDATE, _MSG_RADIO_UPDATE):
            client = message.get("Client") or {}
            if guid := client.get("ClientGuid"):
                self.clients.setdefault(guid, {}).update(client)
        elif kind == _MSG_CLIENT_DISCONNECT:
            client = message.get("Client") or {}
            self.clients.pop(client.get("ClientGuid", ""), None)
        elif kind == _MSG_VERSION_MISMATCH:
            log.error("SRS server rejected protocol version %s (server %s)", PROTOCOL_VERSION, message.get("Version"))

    async def _ping_udp(self) -> None:
        guid = self.guid.encode("ascii")
        while True:
            assert self._udp is not None
            self._udp.sendto(guid)
            # Retry quickly until the first echo, then settle into the normal interval.
            await asyncio.sleep(UDP_PING_INTERVAL_S if self._udp_ready.is_set() else 0.5)

    async def _keep_radio_state(self) -> None:
        while True:
            await asyncio.sleep(RADIO_UPDATE_INTERVAL_S)
            await self._send(_MSG_RADIO_UPDATE)

    @property
    def connected(self) -> bool:
        return self._udp_ready.is_set() and not self._closed.is_set()
