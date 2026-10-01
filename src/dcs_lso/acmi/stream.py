"""Tacview real-time telemetry: client for the stream DCS's Tacview plugin serves
(default port 42674), plus a fake host that replays a recording for testing.

Protocol: https://raia-software-inc.gitbook.io/tacview/technical-documentation/real-time-telemetry-public-protocol
Both peers send a handshake terminated by `\\0`; afterwards the host sends plain
ACMI 2.x text, the same as a `.txt.acmi` file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .reader import iter_lines

DEFAULT_PORT = 42674
_STREAM_ID = "XtraLib.Stream.0"
_TELEMETRY_ID = "Tacview.RealTimeTelemetry.0"

# CRC-64/WE (reveng catalogue): poly 0x42F0E1EBA9EA3693, init and xorout all ones, no reflection.
_CRC64_POLY = 0x42F0E1EBA9EA3693
_CRC64_MASK = 0xFFFFFFFFFFFFFFFF


def crc64_we(data: bytes) -> int:
    crc = _CRC64_MASK
    for byte in data:
        crc ^= byte << 56
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC64_POLY) if crc & (1 << 63) else (crc << 1)
            crc &= _CRC64_MASK
    return crc ^ _CRC64_MASK


def password_hash(password: str | None) -> str:
    """Handshake password field: `0` without a password, else the hex CRC-64/WE of its UTF-16LE text."""
    if not password:
        return "0"
    return format(crc64_we(password.encode("utf-16-le")), "x")


class HandshakeError(ConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class HostInfo:
    name: str


class TelemetryClient:
    """Connects to a Tacview real-time telemetry host and yields ACMI lines."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 client_name: str = "dcs-lso", password: str | None = None) -> None:
        self.host = host
        self.port = port
        self.client_name = client_name
        self.password = password
        self.host_info: HostInfo | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._first_line: bytes | None = None

    async def connect(self, timeout: float = 10.0) -> HostInfo:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout)
        handshake = f"{_STREAM_ID}\n{_TELEMETRY_ID}\n{self.client_name}\n{password_hash(self.password)}\0"
        self._writer.write(handshake.encode("utf-8"))
        await self._writer.drain()
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(b"\0"), timeout)
        except asyncio.IncompleteReadError as exc:
            raise HandshakeError("host closed the connection during the handshake "
                                 "(wrong password, or real-time telemetry disabled?)") from exc
        lines = raw[:-1].decode("utf-8", errors="replace").split("\n")
        if len(lines) < 3 or lines[0] != _STREAM_ID or lines[1] != _TELEMETRY_ID:
            raise HandshakeError(f"unexpected host handshake: {raw!r}")
        self.host_info = HostInfo(name=lines[2])
        # The host sends its handshake before checking ours, and rejects a client by
        # closing the socket. Wait briefly for data so a rejection surfaces here.
        try:
            self._first_line = await asyncio.wait_for(self._reader.readline(), timeout)
        except TimeoutError:
            self._first_line = None  # connected, but nothing to send yet
        else:
            if not self._first_line:
                raise HandshakeError("host closed the connection right after the handshake "
                                     "(wrong password?)")
        return self.host_info

    async def lines(self) -> AsyncIterator[str]:
        """ACMI lines until the host closes the connection."""
        if self._reader is None:
            raise RuntimeError("call connect() first")
        if self._first_line:
            yield self._first_line.decode("utf-8", errors="replace")
            self._first_line = None
        while line := await self._reader.readline():
            yield line.decode("utf-8", errors="replace")

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except ConnectionError:
                pass
            self._writer = None

    async def __aenter__(self) -> TelemetryClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


async def serve_recording(path: str | Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                          host_name: str = "dcs-lso-replay", password: str | None = None,
                          speed: float = 1.0) -> asyncio.Server:
    """Serve a recording as a fake Tacview host, pacing frames by their timestamps.

    `speed` scales playback (0 = as fast as possible). `RecordingTime` is rewritten to
    the connection time, as a live host would send it.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(f"{_STREAM_ID}\n{_TELEMETRY_ID}\n{host_name}\n\0".encode("utf-8"))
            await writer.drain()
            client = (await reader.readuntil(b"\0"))[:-1].decode("utf-8", errors="replace").split("\n")
            if len(client) < 4 or client[3] != password_hash(password):
                return
            loop = asyncio.get_running_loop()
            start = loop.time()
            first_frame: float | None = None
            for line in iter_lines(path):
                if line.startswith("0,RecordingTime="):
                    line = f"0,RecordingTime={datetime.now(UTC).isoformat().replace('+00:00', 'Z')}\n"
                if speed > 0 and line.startswith("#"):
                    t = float(line[1:])
                    first_frame = t if first_frame is None else first_frame
                    delay = start + (t - first_frame) / speed - loop.time()
                    if delay > 0:
                        await writer.drain()
                        await asyncio.sleep(delay)
                writer.write(line.encode("utf-8") if line.endswith("\n") else (line + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
