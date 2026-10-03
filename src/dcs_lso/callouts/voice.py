"""LSO voice clips: rendered once with Piper (neural TTS), loaded as Opus frames at startup.

A clip set is a directory with one WAV per phrase plus `manifest.json`, so it can also be
replaced by real recordings with the same file names. A call may have several phrasings
(variants); one is picked at random each time it is said.
"""

from __future__ import annotations

import json
import random
import wave
from dataclasses import dataclass
from pathlib import Path

from ..srs.audio import load_wav
from ..srs.opus import encode_pcm
from .rules import Call

# What the LSO says for each call (a tuple is a set of variants to pick from).
# Wording follows DCS's own LSO (Scripts/Speech/common_events.lua).
PHRASES: dict[Call, str | tuple[str, ...]] = {
    Call.WAVE_OFF: "Wave off, wave off, wave off!",
    Call.WAVE_OFF_GEAR: "Wave off, gear!",
    Call.BOLTER: "Bolter, bolter, bolter!",
    Call.TRAPPED: ("Welcome aboard.", "Welcome home.", "Welcome aboard, nice trap.", "Welcome home, good trap.",
                   "Welcome back aboard."),
    Call.TRAPPED_WAVED_OFF: ("Welcome aboard. That was a wave off, by the way.",
                             "Welcome home. See me in the ready room.",
                             "Welcome aboard. You did hear the wave off, right?",
                             "Welcome home. We'll talk about that wave off later.",
                             "Welcome aboard, I guess. Wave off means go around."),
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


for _wire, _word in ((1, "one"), (2, "two"), (3, "three"), (4, "four")):
    PHRASES[Call[f"TRAPPED_WIRE_{_wire}"]] = (
        f"Welcome aboard, {_word} wire.",
        f"Welcome home. {_word.capitalize()} wire.",
        f"{_word.capitalize()} wire, welcome aboard.",
        f"Nice trap, {_word} wire. Welcome home.",
    )
    PHRASES[Call[f"TRAPPED_WAVED_OFF_WIRE_{_wire}"]] = (
        f"{_word.capitalize()} wire. That was a wave off, by the way.",
        f"Welcome aboard. {_word.capitalize()} wire, through a wave off. See me in the ready room.",
        f"{_word.capitalize()} wire. You did hear the wave off, right?",
        f"Welcome home, {_word} wire. We'll talk about that wave off later.",
    )


def variants(call: Call) -> tuple[str, ...]:
    text = PHRASES[call]
    return (text,) if isinstance(text, str) else text


def clip_name(call: Call, variant: int = 0) -> str:
    return call.name.lower() + (f"_{variant + 1}" if variant else "") + ".wav"


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
    for call in PHRASES:
        entries = manifest["clips"][call.name] = []
        for i, text in enumerate(variants(call)):
            path = out / clip_name(call, i)
            with wave.open(str(path), "wb") as wav:
                voice.synthesize_wav(text, wav, syn_config=config)
            entries.append({"file": path.name, "text": text})
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
    def __init__(self, clips: dict[Call, list[Clip]], voice: str) -> None:
        self.clips = clips  # every variant of each call
        self.voice = voice

    @classmethod
    def load(cls, directory: str | Path) -> ClipLibrary:
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        clips: dict[Call, list[Clip]] = {}
        for call in Call:
            entries = manifest["clips"].get(call.name)
            if not entries:
                continue
            if isinstance(entries, dict):  # clip sets built before variants existed
                entries = [entries]
            clips[call] = [Clip(e["text"], encode_pcm(load_wav(directory / e["file"]))) for e in entries]
        return cls(clips, manifest.get("voice", "unknown"))

    def __getitem__(self, call: Call) -> Clip:
        return self.clips[call][0]

    def pick(self, call: Call) -> Clip:
        return random.choice(self.clips[call])

    def durations(self) -> dict[Call, float]:
        """How long each call takes to say (its longest variant)."""
        return {call: max(c.seconds for c in clips) for call, clips in self.clips.items()}

    def __contains__(self, call: Call) -> bool:
        return call in self.clips
