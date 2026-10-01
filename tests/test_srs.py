import asyncio
import os
import socket
import subprocess
import time
from array import array
from pathlib import Path

import pytest

from dcs_lso.srs import Decoder, Modulation, Radio, SrsClient, VoicePacket, encode_pcm, short_guid, tone
from dcs_lso.srs.audio import resample
from dcs_lso.srs.opus import FRAME_SAMPLES

GUID = "ufYS_WlLVkmFPjqCgxz6GA"


def test_packet_matches_srs_test_vector():
    # From SRS 2.3.8.2 DCS-SR-CommonTests/Network/UDPVoicePacketTests.cs::EncodeInitialVoicePacket.
    packet = VoicePacket(audio=bytes([0, 1, 2, 3, 4, 5]), frequencies=(100.0,), modulations=(4,),
                         encryptions=(0,), unit_id=1, packet_number=1, client_guid=GUID,
                         retransmission_count=4)
    expected = bytes(
        [79, 0, 6, 0, 10, 0, 0, 1, 2, 3, 4, 5, 0, 0, 0, 0, 0, 0, 89, 64, 4, 0, 1, 0, 0, 0,
         1, 0, 0, 0, 0, 0, 0, 0, 4]
    ) + GUID.encode() * 2
    assert packet.encode() == expected
    decoded = VoicePacket.decode(expected)
    assert (decoded.audio, decoded.frequencies, decoded.modulations, decoded.unit_id, decoded.packet_number,
            decoded.retransmission_count, decoded.client_guid, decoded.transmission_guid) == (
        bytes([0, 1, 2, 3, 4, 5]), (100.0,), (4,), 1, 1, 4, GUID, GUID)


def test_packet_round_trip_multiple_frequencies():
    packet = VoicePacket(audio=b"\x01" * 60, frequencies=(251e6, 127.5e6), modulations=(0, 0), encryptions=(0, 0),
                         unit_id=100000, packet_number=2**40, client_guid=short_guid())
    decoded = VoicePacket.decode(packet.encode())
    assert decoded.frequencies == (251e6, 127.5e6)
    assert decoded.packet_number == 2**40
    assert decoded.client_guid == decoded.transmission_guid == packet.client_guid


def test_short_guid_shape():
    guid = short_guid()
    assert len(guid) == 22 and guid.isascii() and "=" not in guid


def test_opus_round_trip():
    frames = encode_pcm(tone(0.2))
    assert len(frames) == 5
    pcm = Decoder().decode(frames[2])
    assert len(pcm) == FRAME_SAMPLES and max(abs(x) for x in pcm) > 3000


def test_resample_length():
    assert len(resample(array("h", [0] * 22050), 22050, 16000)) == 16000


# --- against a real SRS server (set SRS_SERVER_BIN to SRS-Server-Commandline) ------------------

@pytest.fixture(scope="module")
def srs_server(tmp_path_factory):
    binary = os.environ.get("SRS_SERVER_BIN")
    if not binary or not Path(binary).is_file():
        pytest.skip("SRS_SERVER_BIN not set")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    workdir = tmp_path_factory.mktemp("srs")
    proc = subprocess.Popen([binary, "--port", str(port)], cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("SRS server did not start")
    yield port
    proc.terminate()
    proc.wait(timeout=10)


def test_transmission_reaches_only_matching_radios(srs_server):
    async def run():
        heard = {"match": [], "other_freq": [], "fm": []}
        clients = [
            SrsClient("127.0.0.1", srs_server, radios=(Radio(251.0),), on_voice=lambda p, t: heard["match"].append(p)),
            SrsClient("127.0.0.1", srs_server, radios=(Radio(252.0),), on_voice=lambda p, t: heard["other_freq"].append(p)),
            SrsClient("127.0.0.1", srs_server, radios=(Radio(251.0, Modulation.FM),),
                      on_voice=lambda p, t: heard["fm"].append(p)),
        ]
        tx = SrsClient("127.0.0.1", srs_server, radios=(Radio(251.0),))
        for c in clients + [tx]:
            await c.connect()
        await asyncio.sleep(0.3)
        frames = encode_pcm(tone(0.4))
        await tx.transmit(frames)
        await asyncio.sleep(0.3)
        for c in clients + [tx]:
            await c.close()
        return heard, frames

    heard, frames = asyncio.run(run())
    assert [p.audio for p in heard["match"]] == frames
    assert heard["other_freq"] == [] and heard["fm"] == []
