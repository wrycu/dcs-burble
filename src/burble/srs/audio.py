"""Load audio for transmission as 16 kHz mono 16-bit PCM."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path

from .opus import SAMPLE_RATE


def load_wav(path: str | Path) -> array:
    """Read a 16-bit PCM WAV, downmix to mono and resample (linear) to 16 kHz."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV files are supported")
        channels, rate = wav.getnchannels(), wav.getframerate()
        raw = array("h", wav.readframes(wav.getnframes()))
    if channels > 1:
        raw = array("h", (sum(raw[i:i + channels]) // channels for i in range(0, len(raw), channels)))
    return resample(raw, rate, SAMPLE_RATE)


def resample(pcm: array, src_rate: int, dst_rate: int) -> array:
    if src_rate == dst_rate or not pcm:
        return array("h", pcm)
    n = int(len(pcm) * dst_rate / src_rate)
    step = src_rate / dst_rate
    out = array("h", bytes(2 * n))
    last = len(pcm) - 1
    for i in range(n):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = pcm[min(j, last)]
        b = pcm[min(j + 1, last)]
        out[i] = int(a + (b - a) * frac)
    return out


def speak(text: str, voice: str | None = None) -> array:
    """Synthesize speech with espeak-ng (test-quality voice) as 16 kHz mono PCM."""
    exe = shutil.which("espeak-ng")
    if exe is None:
        raise RuntimeError("espeak-ng is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "speech.wav"
        cmd = [exe, "-w", str(out)]
        if voice:
            cmd += ["-v", voice]
        subprocess.run(cmd + [text], check=True, capture_output=True)
        return load_wav(out)
