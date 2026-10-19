"""The LSO's calls as audio for the replay page: rendered with a Piper voice on the hub, cached in its data folder.

Which voice and phrasing the server agent actually used isn't recorded with the calls yet (PLAN #51), so the hub's
own voice says what was recorded (callouts.voice.spoken_text).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import wave
from pathlib import Path

log = logging.getLogger(__name__)

# As `burble voice build` renders the server agent's clips: LSO calls are brisk.
SPEED = 1.15


class CallVoice:
    """Renders text with one Piper voice model (`.onnx`, its `.onnx.json` beside it), once per text."""

    def __init__(self, model: str | Path, cache_dir: str | Path) -> None:
        self.model = Path(model)
        if not self.model.is_file():
            raise FileNotFoundError(f"no voice model at {self.model}")
        self.name = self.model.stem
        self.cache_dir = Path(cache_dir) / self.name
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._voice = None
        self._lock = threading.Lock()

    def wav(self, text: str) -> Path:
        """The WAV of `text`, rendered the first time it's asked for."""
        path = self.cache_dir / f"{hashlib.sha256(text.encode()).hexdigest()[:24]}.wav"
        if path.is_file():
            return path
        with self._lock:  # one rendering at a time (Piper is CPU-bound)
            if not path.is_file():
                from piper import PiperVoice, SynthesisConfig

                if self._voice is None:
                    log.info("loading voice %s", self.model)
                    self._voice = PiperVoice.load(str(self.model))
                partial = path.with_suffix(".part")
                with wave.open(str(partial), "wb") as out:
                    self._voice.synthesize_wav(text, out, syn_config=SynthesisConfig(length_scale=1.0 / SPEED))
                partial.replace(path)
        return path
