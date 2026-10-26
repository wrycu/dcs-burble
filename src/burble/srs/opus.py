"""Minimal libopus bindings (ctypes) for SRS voice: 16 kHz mono, 40 ms frames."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import struct
from array import array

SAMPLE_RATE = 16000
FRAME_MS = 40
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 640
_APPLICATION_VOIP = 2048
_MAX_PACKET = 4000


def _load() -> ctypes.CDLL:
    path = ctypes.util.find_library("opus") or "libopus.so.0"
    lib = ctypes.CDLL(path)
    lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_strerror.argtypes = [ctypes.c_int]
    lib.opus_strerror.restype = ctypes.c_char_p
    return lib


_lib: ctypes.CDLL | None = None


def _libopus() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
    return _lib


class OpusError(RuntimeError):
    pass


def _check(code: int) -> int:
    if code < 0:
        raise OpusError(_libopus().opus_strerror(code).decode())
    return code


class Encoder:
    def __init__(self) -> None:
        err = ctypes.c_int()
        self._st = _libopus().opus_encoder_create(SAMPLE_RATE, 1, _APPLICATION_VOIP, ctypes.byref(err))
        _check(err.value)

    def encode(self, pcm: array) -> bytes:
        """One 40 ms frame of 16-bit mono PCM (640 samples) -> one Opus packet."""
        if len(pcm) != FRAME_SAMPLES:
            raise ValueError(f"expected {FRAME_SAMPLES} samples, got {len(pcm)}")
        buf = (ctypes.c_ubyte * _MAX_PACKET)()
        src = (ctypes.c_int16 * FRAME_SAMPLES).from_buffer(pcm)
        n = _check(_libopus().opus_encode(self._st, src, FRAME_SAMPLES, buf, _MAX_PACKET))
        return bytes(buf[:n])

    def __del__(self) -> None:
        if getattr(self, "_st", None):
            _libopus().opus_encoder_destroy(self._st)


class Decoder:
    def __init__(self) -> None:
        err = ctypes.c_int()
        self._st = _libopus().opus_decoder_create(SAMPLE_RATE, 1, ctypes.byref(err))
        _check(err.value)

    def decode(self, packet: bytes) -> array:
        out = (ctypes.c_int16 * FRAME_SAMPLES)()
        data = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        n = _check(_libopus().opus_decode(self._st, data, len(packet), out, FRAME_SAMPLES, 0))
        return array("h", out[:n])

    def __del__(self) -> None:
        if getattr(self, "_st", None):
            _libopus().opus_decoder_destroy(self._st)


def encode_pcm(pcm: array) -> list[bytes]:
    """Split 16 kHz mono PCM into 40 ms Opus frames (the last one padded with silence)."""
    encoder = Encoder()
    frames: list[bytes] = []
    for start in range(0, len(pcm), FRAME_SAMPLES):
        chunk = array("h", pcm[start:start + FRAME_SAMPLES])
        chunk.extend([0] * (FRAME_SAMPLES - len(chunk)))
        frames.append(encoder.encode(chunk))
    return frames


def tone(seconds: float, freq_hz: float = 1000.0, amplitude: float = 0.3) -> array:
    """A sine test tone as 16 kHz mono PCM."""
    n = int(seconds * SAMPLE_RATE)
    return array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE)) for i in range(n)))


def pack_frames(frames: list[bytes]) -> bytes:
    """Opus frames in one blob, each after its length (2 bytes, big-endian): a recorded transmission (PLAN #52)."""
    return b"".join(struct.pack(">H", len(f)) + f for f in frames)


def unpack_frames(data: bytes) -> list[bytes]:
    frames, i = [], 0
    while i + 2 <= len(data):
        (n,) = struct.unpack_from(">H", data, i)
        if n == 0 or i + 2 + n > len(data):
            raise ValueError("damaged frames")
        frames.append(data[i + 2:i + 2 + n])
        i += 2 + n
    if i != len(data):
        raise ValueError("damaged frames")
    return frames


def decode_frames(frames: list[bytes]) -> array:
    """16 kHz PCM of a transmission's frames."""
    decoder, pcm = Decoder(), array("h")
    for frame in frames:
        pcm.extend(decoder.decode(frame))
    return pcm
