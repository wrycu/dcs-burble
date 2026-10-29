"""Models Burble downloads when they're missing: Kokoro's and Piper's voices, and Vosk's speech recognition. Each is
written beside where it goes and moved into place once whole, so a cut-off download never looks like a model."""

from __future__ import annotations

import sys
from pathlib import Path

KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
VOSK_MODELS = "https://alphacephei.com/vosk/models"


def download_model(path: Path, engine: str) -> None:
    """Download a missing model to `path`: Kokoro's from its release (by the file's name), Piper's by its voice name
    (e.g. en_US-ryan-high.onnx), and a Vosk model folder by its name (e.g. vosk-model-small-en-us-0.15)."""
    if engine == "vosk":
        if not path.name.startswith("vosk-model-"):
            raise ValueError("a Vosk model is named like vosk-model-small-en-us-0.15")
        download_zip(f"{VOSK_MODELS}/{path.name}.zip", path)
        return
    if engine == "kokoro":
        if path.name not in KOKORO_FILES:
            raise ValueError(f"Kokoro's release has only {', '.join(KOKORO_FILES)}")
        urls = {path: f"{KOKORO_RELEASE}/{path.name}"}
    else:
        from piper.download_voices import URL_FORMAT, VOICE_PATTERN

        match = VOICE_PATTERN.match(path.stem)
        if not match or path.suffix != ".onnx":
            raise ValueError("a Piper model is named after its voice, like en_US-ryan-high.onnx")
        parts = {"lang_family": match["lang_family"], "lang_code": f"{match['lang_family']}_{match['lang_region']}",
                 "voice_name": match["voice_name"], "voice_quality": match["voice_quality"]}
        urls = {path: URL_FORMAT.format(extension=".onnx", **parts),
                path.with_name(path.name + ".json"): URL_FORMAT.format(extension=".onnx.json", **parts)}
    for dest, url in urls.items():
        if not dest.exists():
            download(url, dest)


def download(url: str, dest: Path) -> None:
    """`url` to `dest`, written beside it first, so a cut-off download never looks like a model."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"downloading {dest.name} to {dest.parent}", file=sys.stderr)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            total, done, shown = int(response.headers.get("content-length") or 0), 0, -1
            with part.open("wb") as f:
                for chunk in response.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and sys.stderr.isatty() and (percent := 100 * done // total) != shown:
                        shown = percent
                        print(f"\r  {done >> 20} of {total >> 20} MB ({percent}%)", end="", file=sys.stderr)
            if total and sys.stderr.isatty():
                print(file=sys.stderr)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise OSError(str(exc)) from None
    part.replace(dest)


def download_zip(url: str, dest: Path) -> None:
    """A zip of one folder (`dest`'s name) from `url`, unpacked to `dest`."""
    import shutil
    import zipfile

    archive = dest.with_name(dest.name + ".zip")
    unpacked = dest.with_name(dest.name + ".unzipping")
    download(url, archive)
    try:
        with zipfile.ZipFile(archive) as z:
            if not all(n == dest.name or n.startswith(dest.name + "/") for n in z.namelist()):
                raise ValueError(f"{url} isn't just the folder {dest.name}")
            shutil.rmtree(unpacked, ignore_errors=True)
            z.extractall(unpacked)  # (zipfile keeps every member inside `unpacked`)
        (unpacked / dest.name).replace(dest)
    except zipfile.BadZipFile as exc:
        raise OSError(f"{url}: {exc}") from None
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(unpacked, ignore_errors=True)
