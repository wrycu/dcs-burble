"""LSO voice clips: rendered once with Piper or Kokoro (neural TTS), loaded as Opus frames at startup.

A clip set is a directory with one WAV per phrase plus `manifest.json`, so it can also be
replaced by real recordings with the same file names. A call may have several phrasings
(variants); one is picked at random each time it is said.
"""

from __future__ import annotations

import json
from array import array
import random
import re
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..srs.audio import load_wav
from ..srs.opus import FRAME_SAMPLES, SAMPLE_RATE, encode_pcm
from .rules import Call

# What the LSO says for each call (a tuple is a set of variants to pick from).
# Wording follows DCS's own LSO (Scripts/Speech/common_events.lua).
PHRASES: dict[Call, str | tuple[str, ...]] = {
    Call.WAVE_OFF: "Wave off, wave off, wave off!",
    Call.WAVE_OFF_FOUL_DECK: "Wave off, wave off, foul deck!",
    Call.WAVE_OFF_GEAR: "Wave off, gear!",
    Call.WAVE_OFF_HOOK: "Wave off, hook!",
    Call.BOLTER: "Bolter, bolter, bolter!",
    Call.ROGER_BALL: "Roger ball.",
    Call.ROGER_CLARA: ("Roger, Clara. Keep it coming.", "Roger, Clara. Continue."),
    Call.LOUD_AND_CLEAR: ("Paddles, loud and clear.", "Loud and clear."),
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
    Call.ON_GLIDESLOPE: ("On glideslope.", "On glideslope, keep it coming.", "You're on glideslope."),
    Call.PADDLES_CONTACT: "Paddles contact.",
    Call.CALL_THE_BALL: ("Three quarter mile, call the ball.", "Call the ball."),
    Call.ROUGH_LANDING: ("The crew chief wants to talk to you.", "Maintenance would like a word about that landing.",
                         "Go easy on my deck.", "The airframe guys are going to love that one.",
                         "Somebody check that landing gear."),
}
# Spoken digit by digit to put a side number in front of a call ("three zero one, power") when more
# than one aircraft is in the groove.
DIGITS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven",
          "8": "Eight", "9": "Nine"}
# "Roger ball" with the wind over the deck, in knots ("Roger ball, 25 knots."), and ", axial." when the wind comes
# down the axial deck rather than the angled one: rendered whole for each speed, so they sound natural. Under
# CALM_KNOTS it's "Roger ball, winds calm." (one clip, no axial).
CALM_KNOTS = 3
WIND_KNOTS = range(CALM_KNOTS, 61)
AXIAL_DEG = 6.0  # the deck wind this far or more to starboard of the angled deck's axis is "axial"
ROGER_BALL_CALM = "Roger ball, winds calm."
CALM_CLIP = "roger_ball_calm.wav"


def roger_ball_wind(knots: int, axial: bool) -> str:
    return f"Roger ball, {knots} knots" + (", axial." if axial else ".")


def wind_clip_name(knots: int, axial: bool) -> str:
    return f"roger_ball_{knots}_knots{'_axial' if axial else ''}.wav"


SIDE_NUMBER_GAP_FRAMES = 2  # a short pause (80 ms) between the side number and the call
FOLLOW_GAP_FRAMES = 8  # and between a welcome and a remark after it (320 ms)
SILENCE_LEVEL = 150  # trimmed from the ends of digit clips (16-bit samples)...
TRIM_MARGIN = 480  # ...keeping 30 ms either side so soft consonants ("eight", "two") survive
# Pauses in rendered phrases (user, 2026-10-28: Piper's sounded odd): Piper writes a phrase's sentences back to back,
# with no pause, while its pauses at commas run 100-280 ms. So: this between sentences, pauses inside one cut to this,
# and less of Piper's random timing (its default noise_w_scale is 0.8). Quiet below PAUSE_LEVEL (16-bit samples).
SENTENCE_PAUSE_S = 0.22
MAX_PAUSE_S = 0.12
PAUSE_LEVEL = 400
NOISE_W_SCALE = 0.4
# Kokoro's voices the LSO speaks with (`burble voice build --kokoro-voices`), one clip set each: its American (am_)
# and British (bm_) men. Like Piper's, its sentences run together in a short phrase (it pauses only between the
# batches it splits long texts into), so each is rendered on its own; its pauses at commas are short already.
KOKORO_SPEAKERS = ("am_michael", "am_adam", "am_eric", "am_onyx", "am_fenrir", "am_liam", "bm_george", "bm_lewis")
# The config's voice (callouts.voice) for a different clip set for each carrier (CarrierVoices).
EACH_CARRIER = "each carrier"
# Told after each clip a build renders: (its clip set, clips done, clips in the set).
Progress = Callable[[str, int, int], None]

# Where distributions put espeak-ng's data, which Kokoro phonemizes with: the copy bundled with kokoro-onnx looks for
# its data where it was built, so the system's espeak-ng is used when there is one (`dnf/apt install espeak-ng`).
ESPEAK_DATA_DIRS = ("/usr/share/espeak-ng-data", "/usr/lib/x86_64-linux-gnu/espeak-ng-data",
                    "/usr/lib/aarch64-linux-gnu/espeak-ng-data", "/usr/local/share/espeak-ng-data")


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


def spoken_text(call: dict) -> str:
    """What the LSO said for a recorded call (`{"call", "text"?, "said"?}`, as the server agent records them): exactly
    what was said when recorded ("301, Welcome home, good trap."), else its text ("Roger ball, 25 knots."), else the
    call's first phrasing (calls recorded before 2026-10-25 don't say which variant was said)."""
    for key in ("said", "text"):
        if call.get(key):
            return call[key]
    try:
        return variants(Call(call["call"]))[0]
    except (ValueError, KeyError):
        text = str(call.get("call", ""))
        return text[:1].upper() + text[1:] + "."


def clip_name(call: Call, variant: int = 0) -> str:
    return call.name.lower() + (f"_{variant + 1}" if variant else "") + ".wav"


def _edges_trimmed(pcm: array) -> array:
    loud = [i for i, v in enumerate(pcm) if abs(v) >= PAUSE_LEVEL]
    return pcm[max(0, loud[0] - TRIM_MARGIN):loud[-1] + TRIM_MARGIN] if loud else pcm


def _pauses_capped(pcm: array, rate: int) -> array:
    """`pcm` with every quiet stretch longer than MAX_PAUSE_S cut to it (keeping its two ends)."""
    block, keep = int(rate * 0.01), int(rate * MAX_PAUSE_S)
    out, quiet = array("h"), array("h")
    for i in range(0, len(pcm), block):
        part = pcm[i:i + block]
        if max(abs(v) for v in part) < PAUSE_LEVEL:
            quiet.extend(part)
            continue
        out.extend(quiet[:keep // 2] + quiet[len(quiet) - (keep - keep // 2):] if len(quiet) > keep else quiet)
        quiet = array("h")
        out.extend(part)
    out.extend(quiet)
    return out


def _render(voice, text: str, config, path: Path) -> None:
    """One phrase to a WAV: each sentence Piper renders, edges trimmed and pauses capped, SENTENCE_PAUSE_S apart."""
    pcm, rate = array("h"), None
    for chunk in voice.synthesize(text, syn_config=config):
        if rate is not None:
            pcm.extend([0] * int(rate * SENTENCE_PAUSE_S))
        rate = chunk.sample_rate
        pcm.extend(_pauses_capped(_edges_trimmed(array("h", chunk.audio_int16_bytes)), rate))
    _write_wav(path, pcm, rate or 22050)


def _build(out: Path, voice: str, speed: float, render, progress: Progress | None = None) -> Path:
    """Every phrase into `out` with `render(text, path)`, and the manifest naming `voice`. `progress(out.name, done,
    total)` after each clip."""
    out.mkdir(parents=True, exist_ok=True)
    total = sum(len(variants(call)) for call in PHRASES) + len(DIGITS) + 1 + 2 * len(WIND_KNOTS)
    done = 0

    def say(text: str, path: Path) -> None:
        nonlocal done
        render(text, path)
        done += 1
        if progress is not None:
            progress(out.name, done, total)

    manifest = {"voice": voice, "speed": speed, "clips": {}}
    for call in PHRASES:
        entries = manifest["clips"][call.name] = []
        for i, text in enumerate(variants(call)):
            path = out / clip_name(call, i)
            say(text, path)
            entries.append({"file": path.name, "text": text})
    manifest["numbers"] = {}
    for digit, word in DIGITS.items():
        path = out / f"number_{digit}.wav"
        say(word, path)
        manifest["numbers"][digit] = {"file": path.name, "text": word}
    say(ROGER_BALL_CALM, out / CALM_CLIP)
    manifest["roger_ball_wind"] = {"calm": {"file": CALM_CLIP, "text": ROGER_BALL_CALM}}
    for knots in WIND_KNOTS:
        for axial in (False, True):
            path, text = out / wind_clip_name(knots, axial), roger_ball_wind(knots, axial)
            say(text, path)
            manifest["roger_ball_wind"][f"{knots}{'a' if axial else ''}"] = {"file": path.name, "text": text}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def _write_wav(path: Path, pcm: array, rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def build_clips(model: str | Path, out_dir: str | Path, speed: float = 1.15, progress: Progress | None = None) -> Path:
    """Render every phrase with a Piper voice model (`.onnx`, with its `.onnx.json` beside it).

    `speed` > 1 speaks faster (LSO calls are brisk).
    """
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(str(model))
    config = SynthesisConfig(length_scale=1.0 / speed, noise_w_scale=NOISE_W_SCALE)
    return _build(Path(out_dir), Path(model).stem, speed, lambda text, path: _render(voice, text, config, path),
                  progress)


def espeak_config():
    """Kokoro's espeak-ng: the system's (its library and data), else None for the copy bundled with kokoro-onnx."""
    import ctypes.util

    from kokoro_onnx.config import EspeakConfig

    lib = ctypes.util.find_library("espeak-ng")
    data = next((d for d in ESPEAK_DATA_DIRS if Path(d).is_dir()), None)
    return EspeakConfig(lib_path=lib, data_path=data) if lib and data else None


def build_kokoro_clips(model: str | Path, voices: str | Path, out_dir: str | Path,
                       speakers: tuple[str, ...] = KOKORO_SPEAKERS, speed: float = 1.15,
                       progress: Progress | None = None) -> list[Path]:
    """Render every phrase with a Kokoro model (`kokoro-v1.0.onnx` and its `voices-v1.0.bin`) in each of `speakers`,
    one clip set each in `out_dir` (named after the speaker). Written at 16 kHz, as SRS sends it."""
    import numpy as np
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(model), str(voices), espeak_config=espeak_config())
    unknown = [s for s in speakers if s not in kokoro.get_voices()]
    if unknown:
        raise ValueError(f"no Kokoro voice {', '.join(unknown)} (it has {', '.join(kokoro.get_voices())})")

    def render(speaker: str, text: str, path: Path) -> None:
        lang = "en-gb" if speaker.startswith("b") else "en-us"
        pcm = array("h")
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            samples, rate = kokoro.create(sentence, voice=speaker, speed=speed, lang=lang)
            n = int(len(samples) * SAMPLE_RATE / rate)
            samples = np.interp(np.arange(n) * rate / SAMPLE_RATE, np.arange(len(samples)), samples)
            if pcm:
                pcm.extend([0] * int(SAMPLE_RATE * SENTENCE_PAUSE_S))
            pcm.extend(_edges_trimmed(array("h", (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())))
        _write_wav(path, pcm, SAMPLE_RATE)

    return [_build(Path(out_dir) / speaker, f"kokoro {speaker}", speed,
                   lambda text, path, speaker=speaker: render(speaker, text, path), progress) for speaker in speakers]


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
    log. A folder that is a single clip set is used whatever `voice` says; an unknown or missing `voice` (or
    EACH_CARRIER) gets the first set by name."""
    sets = clip_sets(directory)
    if not sets:
        return None, f"no voice clips in {directory} (build them with `burble voice build`)"
    if "" in sets:
        note = f" (the config's voice {voice!r} needs a folder of clip sets)" if voice else ""
        return sets[""], f"voice: {sets['']}{note}"
    if voice in sets:
        return sets[voice], f"voice: {voice}"
    first = next(iter(sets))
    if voice == EACH_CARRIER:
        return sets[first], f"voices: a different one for each carrier ({', '.join(sets)}); {first} for the rest"
    why = f"no clip set {voice!r}" if voice else "no voice set in the config"
    return sets[first], f"voice: {first} ({why}; have {', '.join(sets)})"


class CarrierVoices:
    """The clip set each carrier speaks with (by its unit name): its own (`pinned`, from the config's carriers), else
    the config's `voice`. With EACH_CARRIER, every other carrier gets a different one while they last: the same
    carrier tends to get the same one from mission to mission (where it starts in `sets` comes from its name).
    `sets`: the loaded clip sets by name (`clip_sets`); `default`: the config's voice, or the first by name."""

    def __init__(self, sets: dict[str, ClipLibrary], default: ClipLibrary, voice: str | None = None,
                 pinned: dict[str, str] | None = None) -> None:
        self.sets = sets
        self.default = default
        self.each = voice == EACH_CARRIER
        self.pinned = {unit: name for unit, name in (pinned or {}).items() if name in sets}
        self._assigned: dict[str, str] = {}

    def for_carrier(self, unit: str | None) -> ClipLibrary:
        if unit in self.pinned:
            return self.sets[self.pinned[unit]]
        if not self.each or unit is None or not self.sets:
            return self.default
        if unit not in self._assigned:
            names = sorted(self.sets)
            used = set(self._assigned.values()) | set(self.pinned.values())
            start = zlib.crc32(unit.encode()) % len(names)
            turn = names[start:] + names[:start]
            self._assigned[unit] = next((n for n in turn if n not in used), turn[0])
        return self.sets[self._assigned[unit]]


class ClipLibrary:
    def __init__(self, clips: dict[Call, list[Clip]], voice: str, numbers: dict[str, Clip] | None = None,
                 wind: dict[tuple[int, bool], Clip] | None = None, calm: Clip | None = None) -> None:
        self.clips = clips  # every variant of each call
        self.voice = voice
        self.numbers = numbers or {}  # "0".."9", for side numbers (clip sets built before them have none)
        self.wind = wind or {}  # (knots, axial): "Roger ball, 25 knots." (clip sets built before them have none)
        self.calm = calm  # "Roger ball, winds calm." (clip sets built before it have none)

    def roger_ball(self, knots: float | None, off_axis: float = 0.0) -> Clip | None:
        """"Roger ball" with the wind over the deck (knots; `off_axis`: degrees off the angled deck's axis, +
        starboard), if this clip set has it."""
        if knots is None:
            return None
        if round(knots) < CALM_KNOTS:
            return self.calm
        return self.wind.get((min(round(knots), WIND_KNOTS[-1]), off_axis >= AXIAL_DEG))

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
        wind = {k: Clip(e["text"], encode_pcm(load_wav(directory / e["file"])))
                for k, e in (manifest.get("roger_ball_wind") or {}).items()}
        calm = wind.pop("calm", None)
        wind = {(int(k.rstrip("a")), k.endswith("a")): clip for k, clip in wind.items()}
        return cls(clips, manifest.get("voice", "unknown"), numbers, wind, calm)

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
