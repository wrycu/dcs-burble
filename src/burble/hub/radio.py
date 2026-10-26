"""A pass's radio recording (PLAN #52): the transmissions on its carrier's frequency from its pilot and the LSO, as the
server agent recorded them (agent/radio.py: 40 ms Opus frames, each transmission stamped with mission time). Stored as
JSON in the hub's radio store; each transmission is played in the replay as a WAV made from it when first asked for.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import wave
from pathlib import Path

from ..srs.opus import FRAME_MS, SAMPLE_RATE, decode_frames, unpack_frames

MAX_TRANSMISSIONS = 100
MAX_SECONDS = 600.0  # all of a pass's transmissions together
MAX_NAME = 100


def parse_radio(value: object) -> bytes:
    """The sidecar's `radio`, checked and stored as JSON; ValueError if it isn't one."""
    if not isinstance(value, dict) or value.get("codec") != "opus" or value.get("sample_rate") != SAMPLE_RATE \
            or value.get("frame_ms") != FRAME_MS:
        raise ValueError("radio: not 16 kHz Opus in 40 ms frames")
    transmissions = value.get("transmissions")
    if not isinstance(transmissions, list) or not 0 < len(transmissions) <= MAX_TRANSMISSIONS:
        raise ValueError("radio: no transmissions, or too many")
    out, frames_total = [], 0
    for t in transmissions:
        if not isinstance(t, dict) or t.get("by") not in ("pilot", "lso") or not isinstance(t.get("time"), int | float):
            raise ValueError("radio: a transmission without its time or who said it")
        try:
            frames = unpack_frames(base64.b64decode(str(t.get("frames") or ""), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"radio: damaged frames ({exc})") from exc
        if not frames:
            raise ValueError("radio: an empty transmission")
        frames_total += len(frames)
        entry = {"time": float(t["time"]), "by": t["by"], "frames": t["frames"]}
        if t["by"] == "pilot" and isinstance(t.get("name"), str) and t["name"]:
            entry["name"] = t["name"][:MAX_NAME]
        out.append(entry)
    if frames_total * FRAME_MS / 1000 > MAX_SECONDS:
        raise ValueError("radio: longer than a pass's radio can be")
    out.sort(key=lambda t: t["time"])
    return json.dumps({"codec": "opus", "sample_rate": SAMPLE_RATE, "frame_ms": FRAME_MS, "transmissions": out},
                      separators=(",", ":")).encode("utf-8")


def transmissions(path: Path) -> list[dict]:
    """The recording's transmissions in order: {"time", "by", "name"?, "seconds"} (their frames left out)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for t in data["transmissions"]:
        frames = unpack_frames(base64.b64decode(t["frames"]))
        out.append({k: t[k] for k in ("time", "by", "name") if k in t} | {"seconds": len(frames) * FRAME_MS / 1000})
    return out


def transmission_wav(path: Path, index: int, cache_dir: Path) -> Path:
    """Transmission `index` of the recording at `path`, as a 16 kHz WAV (made once, then kept in `cache_dir`).
    IndexError if there's no such transmission."""
    target = cache_dir / f"{path.stem}-{index}.wav"
    if target.exists():
        return target
    data = json.loads(path.read_text(encoding="utf-8"))
    if not 0 <= index < len(data["transmissions"]):
        raise IndexError(index)
    pcm = decode_frames(unpack_frames(base64.b64decode(data["transmissions"][index]["frames"])))
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".part")
    with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, target)
    return target


def remove_cached(sha256: str, cache_dir: Path) -> None:
    for f in cache_dir.glob(f"{sha256}-*.wav"):
        f.unlink(missing_ok=True)
