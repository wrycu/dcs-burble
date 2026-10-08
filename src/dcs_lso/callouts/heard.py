"""What pilots say to the LSO (PLAN #20): speech recognised from an SRS transmission, made into a call.

- the ball call: "[side number], [aircraft] ball, [fuel]", e.g. "three zero one, Hornet ball, five point two";
- "Clara": no ball in sight ("three zero one, Clara");
- "Paddles": calling the LSO (e.g. a radio check).

Recognition is Vosk (offline, CPU, about 40 ms a call), limited to the words of these calls (VOCABULARY): on
synthetic calls through SRS's codec it got 36 of 36 fully right, where Whisper (tiny to small) was 3-12 times
slower and heard "Paddles" as "pedals". Vosk is an optional dependency (`uv sync --extra listen`) with a model to
download (e.g. vosk-model-small-en-us-0.15, 68 MB).
"""

from __future__ import annotations

import json
import re
from array import array
from dataclasses import dataclass
from pathlib import Path

DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
          "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
# Aircraft that trap (no Harrier: "three-oh" said quickly was heard as "Harrier").
AIRCRAFT = {"hornet": "Hornet", "rhino": "Rhino", "tomcat": "Tomcat", "goshawk": "Goshawk", "viking": "Viking",
            "skyhawk": "Skyhawk", "corsair": "Corsair", "hawkeye": "Hawkeye", "greyhound": "Greyhound",
            "prowler": "Prowler", "growler": "Growler", "lightning": "Lightning"}
# Words the recogniser may hear; anything else comes out as "[unk]".
# Only these: every extra word is one more thing a call's words can be mistaken for (with "read" and "radio" in
# it, "three" was heard as "read" in five of the user's ten calls). A radio check comes out as "paddles" and
# [unk]s, which is enough.
VOCABULARY = [*DIGITS, *AIRCRAFT, "point", "state", "ball", "clara", "paddles", "correction", "[unk]"]
CALLS = ("ball", "clara", "paddles")


@dataclass(frozen=True, slots=True)
class PilotCall:
    call: str | None  # "ball", "clara", "paddles", or None (not one of ours)
    side_number: str | None = None  # "301"
    aircraft: str | None = None  # "Hornet"
    fuel: float | None = None  # thousands of pounds, e.g. 5.2
    text: str = ""  # what was recognised


def describe(call: PilotCall) -> str:
    """The call as a pilot would write it: "301, Hornet ball, 5.2", "301, Clara", "Paddles"."""
    what = {"ball": f"{call.aircraft} ball" if call.aircraft else "Ball", "clara": "Clara",
            "paddles": "Paddles"}.get(call.call or "", call.text)
    parts = [p for p in (call.side_number, what) if p]
    if call.fuel is not None and call.call in ("ball", "clara"):
        parts.append(f"{call.fuel:.1f}")
    return ", ".join(parts)


def parse(text: str) -> PilotCall:
    """A pilot call from recognised text (words or digits; punctuation and case don't matter)."""
    words = [w.strip(".,!?") for w in re.sub(r"[^a-z0-9.\s]", " ", text.lower().replace("-", " ")).split()]
    tokens = [DIGITS.get(w, w) for w in words if w and w != "unk"]  # "[unk]": a word not in VOCABULARY
    joined = " ".join(tokens)
    call = next((c for c in CALLS if c in tokens), None)
    # The type: the word before "ball" ("Hornet ball"), else any type named.
    at = tokens.index("ball") if "ball" in tokens else -1
    aircraft = AIRCRAFT.get(tokens[at - 1]) if at > 0 else None
    aircraft = aircraft or next((AIRCRAFT[w] for w in tokens if w in AIRCRAFT), None)
    side = None
    lead = []
    start = 1 if tokens[:1] == ["paddles"] else 0  # "Paddles, 305, ..." too
    for t in tokens[start:]:  # the side number: the digits the call starts with
        if not t.isdigit():
            break
        lead.append(t)
    if 2 <= len("".join(lead)) <= 3:
        side = "".join(lead)
    fuel = None
    after = joined.split(call, 1)[1] if call in ("ball", "clara") else ""
    if found := re.findall(r"(\d+)\s*(?:point|\.)\s*(\d)", after):  # the last one: "5.3, correction, 5.8"
        fuel = float(f"{found[-1][0]}.{found[-1][1]}")
    return PilotCall(call, side, aircraft, fuel, text.strip())


class Recogniser:
    """Vosk, limited to VOCABULARY. `model`: a Vosk model folder."""

    def __init__(self, model: str | Path) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise RuntimeError("speech recognition needs Vosk: `uv sync --extra listen`") from exc
        SetLogLevel(-1)
        self._recognizer = KaldiRecognizer
        self._model = Model(str(model))
        self._grammar = json.dumps(VOCABULARY)

    def text(self, pcm: array, sample_rate: int = 16000) -> str:
        r = self._recognizer(self._model, sample_rate, self._grammar)
        # A little silence first and last: the first word is often lost without it.
        lead = array("h", [0] * (sample_rate * 3 // 10))
        r.AcceptWaveform((lead + pcm + lead).tobytes())
        return json.loads(r.FinalResult()).get("text", "")

    def hear(self, pcm: array, sample_rate: int = 16000) -> PilotCall:
        return parse(self.text(pcm, sample_rate))
