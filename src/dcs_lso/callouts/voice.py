"""LSO voice clips: rendered once with Piper (neural TTS), loaded as Opus frames at startup.

A clip set is a directory with one WAV per phrase plus `manifest.json`, so it can also be
replaced by real recordings with the same file names. A call may have several phrasings
(variants); one is picked at random each time it is said.
"""

from __future__ import annotations

import json
from array import array
import random
import wave
from dataclasses import dataclass
from pathlib import Path

from ..srs.audio import load_wav
from ..srs.opus import FRAME_SAMPLES, encode_pcm
from .rules import Call

# What the LSO says for each call (a tuple is a set of variants to pick from).
# Wording follows DCS's own LSO (Scripts/Speech/common_events.lua).
PHRASES: dict[Call, str | tuple[str, ...]] = {
    Call.WAVE_OFF: "Wave off, wave off, wave off!",
    Call.WAVE_OFF_FOUL_DECK: "Wave off, wave off, foul deck!",
    Call.WAVE_OFF_GEAR: "Wave off, gear!",
    Call.BOLTER: "Bolter, bolter, bolter!",
    # Welcomes: "home"/"back" variants are for jets returning to the carrier they launched from, "aboard" ones
    # for visitors, and ones saying neither for both (see `welcome_kind`).
    Call.TRAPPED: ("Welcome aboard.", "Welcome home.", "Welcome aboard, nice trap.", "Welcome home, good trap.",
                   "Welcome back aboard.", "Welcome back.", "Welcome aboard, good trap."),
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
    Call.DONT_SETTLE: "Don't settle.",
    Call.DONT_CLIMB: "Don't climb.",
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
    Call.KEEP_TURN_IN: "Keep your turn in.",
    Call.KEEP_IT_COMING: ("Keep it coming.", "Keep it coming, looking good."),
    Call.ROUGH_LANDING: ("The crew chief wants to talk to you.", "Maintenance would like a word about that landing.",
                         "Go easy on my deck.", "The airframe guys are going to love that one.",
                         "Somebody check that landing gear."),
}
# Spoken digit by digit to put a side number in front of a call ("three zero one, power") when more
# than one aircraft is in the groove.
DIGITS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven",
          "8": "Eight", "9": "Nine"}
SIDE_NUMBER_GAP_FRAMES = 2  # a short pause (80 ms) between the side number and the call
FOLLOW_GAP_FRAMES = 8  # and between a welcome and a remark after it (320 ms)
SILENCE_LEVEL = 150  # trimmed from the ends of digit clips (16-bit samples)...
TRIM_MARGIN = 480  # ...keeping 30 ms either side so soft consonants ("eight", "two") survive


for _wire, _word in ((1, "one"), (2, "two"), (3, "three"), (4, "four")):
    PHRASES[Call[f"TRAPPED_WIRE_{_wire}"]] = (
        f"Welcome aboard, {_word} wire.",
        f"Welcome home. {_word.capitalize()} wire.",
        f"{_word.capitalize()} wire, welcome aboard.",
        f"Nice trap, {_word} wire. Welcome home.",
        f"Nice trap, {_word} wire. Welcome aboard.",
        f"Welcome back, {_word} wire.",
    )
    PHRASES[Call[f"TRAPPED_WAVED_OFF_WIRE_{_wire}"]] = (
        f"{_word.capitalize()} wire. That was a wave off, by the way.",
        f"Welcome aboard. {_word.capitalize()} wire, through a wave off. See me in the ready room.",
        f"{_word.capitalize()} wire. You did hear the wave off, right?",
        f"Welcome home, {_word} wire. We'll talk about that wave off later.",
    )


def welcome_kind(text: str) -> str | None:
    """"home" for a welcome back to the jet's own carrier ("welcome home", "welcome back"), "aboard" for a
    visitor's, None for one that says neither."""
    text = text.lower()
    if "home" in text or "back" in text:
        return "home"
    return "aboard" if "aboard" in text else None


def is_praise(text: str) -> bool:
    """A welcome that compliments the landing: only for passes graded OK or better."""
    text = text.lower()
    return "nice trap" in text or "good trap" in text


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
    manifest["numbers"] = {}
    for digit, word in DIGITS.items():
        path = out / f"number_{digit}.wav"
        with wave.open(str(path), "wb") as wav:
            voice.synthesize_wav(word, wav, syn_config=config)
        manifest["numbers"][digit] = {"file": path.name, "text": word}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


@dataclass(frozen=True, slots=True)
class Clip:
    text: str
    frames: list[bytes]  # 40 ms Opus frames, ready for SRS

    @property
    def seconds(self) -> float:
        return len(self.frames) * 0.04


def _trim(pcm: array) -> array:
    """Drop the near-silence Piper leaves before and after a word."""
    loud = [i for i, v in enumerate(pcm) if abs(v) >= SILENCE_LEVEL]
    if not loud:
        return pcm
    return array("h", pcm[max(0, loud[0] - TRIM_MARGIN):loud[-1] + 1 + TRIM_MARGIN])


def clip_sets(directory: str | Path) -> dict[str, Path]:
    """The clip sets in `directory`, by name: the folder itself when it is one (has a manifest.json), named
    "", else each folder in it that is one, by folder name."""
    directory = Path(directory)
    if (directory / "manifest.json").is_file():
        return {"": directory}
    if not directory.is_dir():
        return {}
    return {d.name: d for d in sorted(directory.iterdir()) if (d / "manifest.json").is_file()}


def choose_clip_set(directory: str | Path, voice: str | None) -> tuple[Path | None, str]:
    """The clip set to use for `voice` (a folder name from the hub's config), and a note on the choice for the
    log. A folder that is a single clip set is used whatever `voice` says; an unknown or missing `voice` gets
    the first set by name."""
    sets = clip_sets(directory)
    if not sets:
        return None, f"no voice clips in {directory} (build them with `dcs-lso voice build`)"
    if "" in sets:
        note = f" (the config's voice {voice!r} needs a folder of clip sets)" if voice else ""
        return sets[""], f"voice: {sets['']}{note}"
    if voice in sets:
        return sets[voice], f"voice: {voice}"
    first = next(iter(sets))
    why = f"no clip set {voice!r}" if voice else "no voice set in the config"
    return sets[first], f"voice: {first} ({why}; have {', '.join(sets)})"


class ClipLibrary:
    def __init__(self, clips: dict[Call, list[Clip]], voice: str, numbers: dict[str, Clip] | None = None) -> None:
        self.clips = clips  # every variant of each call
        self.voice = voice
        self.numbers = numbers or {}  # "0".."9", for side numbers (clip sets built before them have none)

    def with_side_number(self, clip: Clip, side_number: str | None) -> Clip:
        """`clip` preceded by the side number, digit by digit ("301, Power."); unchanged without one or
        without digit clips."""
        digits = "".join(ch for ch in (side_number or "") if ch.isdigit())
        if not digits or any(d not in self.numbers for d in digits):
            return clip
        frames = [f for d in digits for f in self.numbers[d].frames]
        gap = encode_pcm(array("h", [0] * (FRAME_SAMPLES * SIDE_NUMBER_GAP_FRAMES)))
        return Clip(f"{digits}, {clip.text}", frames + gap + clip.frames)

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
        numbers = {d: Clip(e["text"], encode_pcm(_trim(load_wav(directory / e["file"]))))
                   for d, e in (manifest.get("numbers") or {}).items()}
        return cls(clips, manifest.get("voice", "unknown"), numbers)

    def __getitem__(self, call: Call) -> Clip:
        return self.clips[call][0]

    def pick(self, call: Call, praise: bool = True, home: bool | None = None) -> Clip:
        """A random variant; without `praise`, never one that compliments the landing. `home`: a welcome back to
        the jet's own carrier (True) or a visitor's (False), else either."""
        clips = self.clips[call] if praise else [c for c in self.clips[call] if not is_praise(c.text)]
        if home is not None:
            wrong = "aboard" if home else "home"
            clips = [c for c in clips if welcome_kind(c.text) != wrong] or clips
        return random.choice(clips or self.clips[call])

    def followed_by(self, clip: Clip, then: Clip) -> Clip:
        """`clip`, a short pause, then `then` (e.g. a welcome and a dig about the landing)."""
        gap = encode_pcm(array("h", [0] * (FRAME_SAMPLES * FOLLOW_GAP_FRAMES)))
        return Clip(f"{clip.text} {then.text}", clip.frames + gap + then.frames)

    def durations(self) -> dict[Call, float]:
        """How long each call takes to say (its longest variant)."""
        return {call: max(c.seconds for c in clips) for call, clips in self.clips.items()}

    def __contains__(self, call: Call) -> bool:
        return call in self.clips
