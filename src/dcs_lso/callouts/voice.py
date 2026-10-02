"""LSO voice clips: rendered once with Piper (neural TTS), loaded as Opus frames at startup.

A clip set is a directory with one WAV per call plus `manifest.json`, so it can also be
replaced by real recordings with the same file names.
"""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path

from ..srs.audio import load_wav
from ..srs.opus import encode_pcm
from .rules import Call

# What the LSO says for each call.
# Wording follows DCS's own LSO (Scripts/Speech/common_events.lua).
PHRASES: dict[Call, str] = {
    Call.WAVE_OFF: "Wave off, wave off, wave off!",
    Call.WAVE_OFF_GEAR: "Wave off, gear!",
    Call.BOLTER: "Bolter, bolter, bolter!",
    Call.POWER_X3: "Power, power, POWER!",
    Call.POWER_X2: "Power. Power!",
    Call.POWER: "Power.",
    Call.LOW: "You're low.",
    Call.HIGH: "You're high.",
    Call.EASY_WITH_IT: "Easy with it.",
    Call.RIGHT_FOR_LINEUP: "Right for lineup.",
    Call.COME_LEFT: "Come left.",
    Call.GOING_LOW: "You're going low.",
    Call.LITTLE_LOW: "You're a little low.",
    Call.LITTLE_HIGH: "You're a little high.",
    Call.GOING_HIGH: "You're going high.",
    Call.LITTLE_RIGHT: "A little right for lineup.",
    Call.LITTLE_LEFT: "A little come left.",
    Call.DRIFTING_LEFT: "You're drifting left.",
    Call.DRIFTING_RIGHT: "You're drifting right.",
    Call.EASY_WINGS: "Easy with your wings.",
    Call.EASY_NOSE: "Easy with the nose.",
    Call.FAST: "You're fast.",
    Call.SLOW: "You're slow.",
}


def clip_name(call: Call) -> str:
    return call.name.lower() + ".wav"


def build_clips(model: str | Path, out_dir: str | Path, speed: float = 1.15) -> Path:
    """Render every phrase with a Piper voice model (`.onnx`, with its `.onnx.json` beside it).

    `speed` > 1 speaks faster (LSO calls are brisk).
    """
    from piper import PiperVoice, SynthesisConfig

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    voice = PiperVoice.load(str(model))
    config = SynthesisConfig(length_scale=1.0 / speed)
    manifest = {"voice": Path(model).stem, "speed": speed, "clips": {}}
    for call, text in PHRASES.items():
        path = out / clip_name(call)
        with wave.open(str(path), "wb") as wav:
            voice.synthesize_wav(text, wav, syn_config=config)
        manifest["clips"][call.name] = {"file": path.name, "text": text}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


@dataclass(frozen=True, slots=True)
class Clip:
    text: str
    frames: list[bytes]  # 40 ms Opus frames, ready for SRS

    @property
    def seconds(self) -> float:
        return len(self.frames) * 0.04


class ClipLibrary:
    def __init__(self, clips: dict[Call, Clip], voice: str) -> None:
        self.clips = clips
        self.voice = voice

    @classmethod
    def load(cls, directory: str | Path) -> ClipLibrary:
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        clips: dict[Call, Clip] = {}
        for call in Call:
            entry = manifest["clips"].get(call.name)
            if entry is None:
                continue
            clips[call] = Clip(entry["text"], encode_pcm(load_wav(directory / entry["file"])))
        return cls(clips, manifest.get("voice", "unknown"))

    def __getitem__(self, call: Call) -> Clip:
        return self.clips[call]

    def __contains__(self, call: Call) -> bool:
        return call in self.clips
