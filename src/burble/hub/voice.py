"""The LSO's calls as audio, for the live view and the replays of passes without a radio recording: put together
from the same clip sets the server agent speaks with (`burble voice build`), so they sound as they did on the radio.

Each call recorded since 2026-10-27 (PLAN #51) says exactly what was said ("301, Welcome home. Two wire.") and in
which clip set's voice; it's made again from that set's clips (the side number's digits, then one phrase, or two
with a pause between), with the agent's own gaps. A call naming no voice, or one this hub hasn't got, is said in the
default voice (`[hub] default_voice`, else the first set by name); one that can't be made from the clips (e.g. a
phrase since reworded) isn't said by the hub, and the browser's own voice says it instead.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import wave
from array import array
from pathlib import Path

from ..callouts.voice import FOLLOW_GAP_FRAMES, SIDE_NUMBER_GAP_FRAMES, _trim, clip_sets
from ..srs.audio import load_wav
from ..srs.opus import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

RESCAN_S = 30  # how often the folder is looked at again for a voice it hasn't got (e.g. one just built)


class _ClipSet:
    """One clip set's WAVs by what they say."""

    def __init__(self, directory: Path) -> None:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.voice = str(manifest.get("voice") or directory.name)
        self.phrases: dict[str, Path] = {}
        for entries in (manifest.get("clips") or {}).values():
            for e in entries if isinstance(entries, list) else [entries]:
                self.phrases.setdefault(e["text"], directory / e["file"])
        for e in (manifest.get("roger_ball_wind") or {}).values():
            self.phrases.setdefault(e["text"], directory / e["file"])
        self.numbers = {d: directory / e["file"] for d, e in (manifest.get("numbers") or {}).items()}

    def say(self, text: str) -> array | None:
        """`text` made from the clips, as the agent says it, or None when they can't make it."""
        digits, rest = "", text
        if m := re.fullmatch(r"(\d+), (.+)", text, flags=re.S):
            digits, rest = m[1], m[2]
            if any(d not in self.numbers for d in digits):
                return None
        phrase = self._phrase(rest)
        if phrase is None:
            return None
        if not digits:
            return phrase
        pcm = array("h")
        for d in digits:
            pcm.extend(_trim(load_wav(self.numbers[d])))
        pcm.extend([0] * (FRAME_SAMPLES * SIDE_NUMBER_GAP_FRAMES))
        return pcm + phrase

    def _phrase(self, text: str) -> array | None:
        if text in self.phrases:
            return load_wav(self.phrases[text])
        # A welcome and a remark after it: two clips, a pause between.
        for m in re.finditer(r"(?<=[.!?]) ", text):
            first, then = text[:m.start()], text[m.end():]
            if first in self.phrases and then in self.phrases:
                gap = array("h", [0] * (FRAME_SAMPLES * FOLLOW_GAP_FRAMES))
                return load_wav(self.phrases[first]) + gap + load_wav(self.phrases[then])
        return None


class CallVoice:
    """The clip sets in `voice_dir` (a folder of them, or one), saying recorded calls in the voice they name."""

    def __init__(self, voice_dir: str | Path, default: str | None = None) -> None:
        self.voice_dir = Path(voice_dir)
        self.default = default
        self._sets: dict[str, _ClipSet] = {}
        self._order: list[_ClipSet] = []
        self._scanned = None
        self._lock = threading.Lock()

    def _scan(self) -> None:
        with self._lock:
            if self._scanned is not None and time.monotonic() - self._scanned < RESCAN_S:
                return
            self._scanned = time.monotonic()
            sets, order = {}, []
            for name, directory in clip_sets(self.voice_dir).items():
                try:
                    clip_set = _ClipSet(directory)
                except (OSError, ValueError, KeyError) as exc:
                    log.warning("clip set %s unusable: %s", directory, exc)
                    continue
                order.append(clip_set)
                for key in (name, clip_set.voice):
                    if key:
                        sets.setdefault(key, clip_set)
            if [s.voice for s in order] != [s.voice for s in self._order]:
                log.info("voices for the LSO's calls: %s", ", ".join(s.voice for s in order) or f"none in "
                         f"{self.voice_dir}")
            self._sets, self._order = sets, order

    def available(self) -> bool:
        """Has it any clip set (looking again now and then, for ones built since)?"""
        self._scan()
        return bool(self._order)

    def _set_for(self, voice: str | None) -> _ClipSet | None:
        self._scan()
        return (self._sets.get(voice or "") or self._sets.get(self.default or "")
                or (self._order[0] if self._order else None))

    def wav(self, text: str, voice: str | None = None) -> bytes | None:
        """`text` said in `voice` (a clip set's voice or folder name), as a WAV, or None when it can't be."""
        clip_set = self._set_for(voice)
        try:
            pcm = clip_set.say(text) if clip_set is not None else None
        except (OSError, ValueError, EOFError) as exc:  # e.g. a clip missing from a set copied part way
            log.warning("clip set for %r unusable: %s", voice or self.default, exc)
            pcm = None
        if pcm is None:
            return None
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return out.getvalue()
