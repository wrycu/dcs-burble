"""SRS UDP voice packet, as in SRS 2.3.8.2 `Common/Models/UDPVoicePacket.cs` (little-endian).

    u16 packet length | u16 audio length | u16 frequency-part length
    audio bytes
    per frequency: f64 frequency (Hz) | u8 modulation | u8 encryption
    u32 unit id | u64 packet number | u8 retransmission count
    22-byte transmission GUID | 22-byte client GUID
"""

from __future__ import annotations

import base64
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum

GUID_LENGTH = 22
_HEADER = struct.Struct("<HHH")
_FREQ = struct.Struct("<dBB")
_FIXED = struct.Struct("<IQB")


class Modulation(IntEnum):
    AM = 0
    FM = 1
    INTERCOM = 2
    DISABLED = 3
    HAVEQUICK = 4
    SATCOM = 5
    MIDS = 6
    SINCGARS = 7


def short_guid() -> str:
    """SRS client GUID: a random GUID as 22 characters of URL-safe base64."""
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).decode("ascii")[:GUID_LENGTH]


@dataclass(frozen=True, slots=True)
class VoicePacket:
    audio: bytes
    frequencies: tuple[float, ...]
    modulations: tuple[int, ...]
    encryptions: tuple[int, ...]
    unit_id: int
    packet_number: int
    client_guid: str
    transmission_guid: str | None = None  # defaults to the client GUID
    retransmission_count: int = 0

    def encode(self) -> bytes:
        freq_part = b"".join(
            _FREQ.pack(f, m, e) for f, m, e in zip(self.frequencies, self.modulations, self.encryptions)
        )
        body = (
            self.audio
            + freq_part
            + _FIXED.pack(self.unit_id, self.packet_number, self.retransmission_count)
            + (self.transmission_guid or self.client_guid).encode("ascii")
            + self.client_guid.encode("ascii")
        )
        total = _HEADER.size + len(body)
        return _HEADER.pack(total, len(self.audio), len(freq_part)) + body

    @classmethod
    def decode(cls, data: bytes) -> VoicePacket:
        total, audio_len, freq_len = _HEADER.unpack_from(data, 0)
        if total != len(data):
            raise ValueError(f"length mismatch: header says {total}, got {len(data)}")
        pos = _HEADER.size
        audio = data[pos:pos + audio_len]
        pos += audio_len
        freqs, mods, encs = [], [], []
        for _ in range(freq_len // _FREQ.size):
            f, m, e = _FREQ.unpack_from(data, pos)
            freqs.append(f)
            mods.append(m)
            encs.append(e)
            pos += _FREQ.size
        unit_id, number, retrans = _FIXED.unpack_from(data, pos)
        return cls(
            audio=audio,
            frequencies=tuple(freqs),
            modulations=tuple(mods),
            encryptions=tuple(encs),
            unit_id=unit_id,
            packet_number=number,
            retransmission_count=retrans,
            transmission_guid=data[-2 * GUID_LENGTH:-GUID_LENGTH].decode("ascii"),
            client_guid=data[-GUID_LENGTH:].decode("ascii"),
        )
