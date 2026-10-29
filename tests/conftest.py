import httpx
import pytest


@pytest.fixture(autouse=True)
def burble_home(tmp_path, monkeypatch):
    """Burble's folder (~/.burble: its config file, and the default data folders) in the test's temporary folder."""
    home = tmp_path / "burble-home"
    monkeypatch.setenv("BURBLE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """Models are downloaded when missing (e.g. the agent's Vosk model): never from a test."""
    def refuse(method, url, **_kwargs):
        raise httpx.ConnectError(f"no downloads in tests ({url})")
    monkeypatch.setattr(httpx, "stream", refuse)
