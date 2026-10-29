"""This machine's settings (~/.burble/config.toml): written with every default on first use, read by each command
from its own section, with a flag on the command line winning."""

import re
import tomllib

import pytest

from burble import localconfig as lc
from burble.cli import main


def test_the_config_file_is_written_on_first_use(burble_home, capsys):
    assert main(["hub", "add-agent", "server1"]) == 0
    path = burble_home / "config.toml"
    text = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600 and "wrote Burble's settings" in capsys.readouterr().err
    assert tomllib.loads(text) == {s: {} for s in ("hub", "agent", "pilot_uploader", "upload", "voice", "telemetry")}
    # Every setting is there, commented out at its default, and valid once uncommented.
    uncommented = tomllib.loads(re.sub(r"^#(\w+ = )", r"\1", text, flags=re.M))
    assert uncommented["hub"]["port"] == 8000 and uncommented["agent"]["tacview_port"] == 42674
    assert uncommented["voice"]["speakers"][0] == "am_michael" and uncommented["hub"]["public_url"] == ""
    assert set(uncommented["agent"]) == {o.key for o in lc.SECTIONS["agent"]}
    # The hub went in the default folder (made as needed, no --create).
    assert (burble_home / "hub" / "lso.db").exists()
    assert main(["hub", "add-agent", "server2"]) == 0 and path.read_text() == text  # not written again


def test_a_flag_wins_over_the_file_over_the_default(burble_home, tmp_path):
    import argparse

    burble_home.mkdir()
    (burble_home / "config.toml").write_text(
        '[agent]\ntacview_host = "10.0.0.2"\ntoken = "base"\nurl = ""\n'
        '[agent.server1]\ntoken = "one"\ndcs_log = "~/dcs.log"\n')
    seen = {}

    def run(argv):
        import burble.cli as cli
        original = cli._agent
        cli._agent = lambda args: seen.update(vars(args)) or 0
        try:
            assert main(argv) == 0
        finally:
            cli._agent = original
        return argparse.Namespace(**seen)

    a = run(["agent"])
    assert (a.tacview_host, a.token, a.tacview_port) == ("10.0.0.2", "base", 42674)
    assert a.url == "http://127.0.0.1:8000"  # "" is unset
    assert a.work_dir == burble_home / "agent" and a.dcs_log is None
    a = run(["agent", "server1", "--tacview-port", "4000"])
    assert (a.tacview_host, a.token, a.tacview_port) == ("10.0.0.2", "one", 4000)
    assert a.work_dir == burble_home / "agent" / "server1" and a.dcs_log.name == "dcs.log" and "~" not in str(a.dcs_log)
    assert a._given["token"] == "config" and a._given["tacview_port"] == "flag" and a._given["url"] == "default"


@pytest.mark.parametrize("config,says", [
    ("[hub]\nport = \"eighty\"\n", "hub.port should be a whole number"),
    ("[hub]\nnope = 1\n", "no setting 'nope' in [hub]"),
    ("[hub]\nother_servers = \"some\"\n", "should be one of shown, hidden"),
    ("[hub\n", "config.toml"),
])
def test_a_config_file_that_wont_do_says_why(burble_home, config, says):
    burble_home.mkdir()
    (burble_home / "config.toml").write_text(config)
    with pytest.raises(SystemExit, match=re.escape(says)):
        main(["hub", "add-agent", "server1"])


def test_a_hub_folder_set_but_missing_needs_create(burble_home, tmp_path):
    burble_home.mkdir()
    (burble_home / "config.toml").write_text(f'[hub]\ndata_dir = "{tmp_path / "typo"}"\n')
    with pytest.raises(SystemExit, match="no hub in"):
        main(["hub", "add-agent", "server1"])
    assert main(["hub", "--create", "add-agent", "server1"]) == 0


def test_another_config_file(tmp_path, burble_home):
    path = tmp_path / "other.toml"
    path.write_text(f'[hub]\ndata_dir = "{tmp_path / "hub"}"\n')
    assert main(["--config", str(path), "hub", "--create", "add-agent", "server1"]) == 0
    assert (tmp_path / "hub" / "lso.db").exists() and not burble_home.exists()
    with pytest.raises(SystemExit, match="no config file"):
        main(["--config", str(tmp_path / "missing.toml"), "hub", "add-agent", "x"])


class _FakeStream:
    """Stands in for `httpx.stream`: each URL's bytes, and the URLs asked for."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.urls = files, []

    def __call__(self, method, url, **_kwargs):
        import contextlib

        import httpx

        self.urls.append(url)
        body = self.files.get(url)

        @contextlib.contextmanager
        def stream():
            response = httpx.Response(200 if body is not None else 404, content=body or b"",
                                      request=httpx.Request(method, url))
            yield response
        return stream()


def test_voice_build_downloads_missing_models(burble_home, monkeypatch):
    import httpx

    from burble import models

    folder = burble_home / "folder"
    fake = _FakeStream({f"{models.KOKORO_RELEASE}/kokoro-v1.0.onnx": b"model", f"{models.KOKORO_RELEASE}/voices-v1.0.bin": b"v"})
    monkeypatch.setattr(httpx, "stream", fake)
    for name in models.KOKORO_FILES:
        models.download_model(folder / name, "kokoro")
    assert (folder / "kokoro-v1.0.onnx").read_bytes() == b"model" and (folder / "voices-v1.0.bin").read_bytes() == b"v"
    assert not list(folder.glob("*.part"))
    # Piper's by its voice name, with its .json.
    fake.files = {u: b"x" for u in ("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/"
                                    f"en_US-ryan-high{ext}?download=true" for ext in (".onnx", ".onnx.json"))}
    models.download_model(folder / "en_US-ryan-high.onnx", "piper")
    assert (folder / "en_US-ryan-high.onnx").exists() and (folder / "en_US-ryan-high.onnx.json").exists()
    # A failed download leaves nothing that looks like a model; a name with nowhere to get it from is refused.
    with pytest.raises(OSError):
        models.download_model(folder / "en_GB-alan-low.onnx", "piper")
    assert not list(folder.glob("en_GB*"))
    with pytest.raises(ValueError):
        models.download_model(folder / "mine.onnx", "kokoro")


def test_a_vosk_model_is_downloaded_and_unpacked(burble_home, monkeypatch):
    import io
    import zipfile

    import httpx

    from burble import models

    name = "vosk-model-small-en-us-0.15"
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr(f"{name}/am/final.mdl", b"model")
    monkeypatch.setattr(httpx, "stream", _FakeStream({f"{models.VOSK_MODELS}/{name}.zip": zipped.getvalue()}))
    model = burble_home / "models" / name
    models.download_model(model, "vosk")
    assert (model / "am" / "final.mdl").read_bytes() == b"model"
    assert sorted(p.name for p in model.parent.iterdir()) == [name]  # no zip or half-unpacked folder left


def test_the_agent_runs_without_listening_when_its_model_cant_be_downloaded(burble_home, monkeypatch, caplog):
    from burble import cli
    from burble.agent import service

    seen = {}

    class FakeAgent:
        def __init__(self, config):
            seen["listen_model"] = config.listen_model

        async def run_forever(self):
            pass

    monkeypatch.setattr(service, "Agent", FakeAgent)
    assert cli.main(["agent"]) in (0, None)
    assert seen["listen_model"] is None and "listening stays off" in caplog.text
