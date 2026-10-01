import asyncio
from pathlib import Path

import pytest

from dcs_lso.acmi import AcmiParser, ObjectUpdate, load_recording
from dcs_lso.acmi.stream import HandshakeError, TelemetryClient, crc64_we, password_hash, serve_recording

FIXTURE = Path(__file__).parent / "fixtures" / "ai_hornet_trap_cvn75.zip.acmi"


def test_crc64_we_check_value():
    # reveng catalogue check value for CRC-64/WE.
    assert crc64_we(b"123456789") == 0x62EC59E3F1A4F00A


def test_password_hash():
    assert password_hash(None) == "0"
    assert password_hash("") == "0"
    assert password_hash("secret") == format(crc64_we("secret".encode("utf-16-le")), "x")


async def _stream_all(password_host, password_client):
    server = await serve_recording(FIXTURE, port=0, password=password_host, speed=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = TelemetryClient(port=port, password=password_client)
        info = await client.connect()
        parser = AcmiParser()
        moved = 0
        async for line in client.lines():
            moved += sum(1 for r in parser.feed(line) if isinstance(r, ObjectUpdate) and r.moved)
        await client.close()
    return info, moved


def test_stream_round_trip_matches_file():
    info, moved = asyncio.run(_stream_all(None, None))
    assert info.name == "dcs-lso-replay"
    recording = load_recording(FIXTURE)
    assert moved == sum(len(t.samples) for t in recording.objects.values())


def test_wrong_password_is_rejected():
    with pytest.raises(HandshakeError):
        asyncio.run(_stream_all("right", "wrong"))
