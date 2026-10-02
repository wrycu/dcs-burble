import asyncio
import json
import os
import sqlite3
import wave
from pathlib import Path

import httpx
import pytest

from dcs_lso.acmi.stream import serve_recording
from dcs_lso.callouts.rules import Call
from dcs_lso.callouts.voice import PHRASES, ClipLibrary, clip_name
from dcs_lso.central.app import create_app
from dcs_lso.central.service import Central
from dcs_lso.edge.callouts import CalloutSettings, SrsSink
from dcs_lso.edge.collector import Collector, CollectorConfig
from dcs_lso.srs import Modulation, Radio, SrsClient
from dcs_lso.srs.opus import tone

FIXTURES = Path(__file__).parent / "fixtures"
FLAT_LOW_CUT = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"  # flown ~2.8 deg low throughout
CONFIG = {"callouts": {"enabled": True, "srs": {"host": "127.0.0.1", "port": 5002},
                       "frequency_mhz": 127.5, "modulation": "AM",
                       "carriers": {"CVN-75 Harry S. Truman": {"frequency_mhz": 127.6}}}}


@pytest.fixture
def clips(tmp_path) -> Path:
    """A clip set made of short tones (stands in for `dcs-lso voice build`)."""
    d = tmp_path / "voice"
    d.mkdir()
    manifest = {"voice": "test-tones", "clips": {}}
    for call, text in PHRASES.items():
        with wave.open(str(d / clip_name(call)), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(tone(0.2).tobytes())
        manifest["clips"][call.name] = {"file": clip_name(call), "text": text}
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


class RecordingSink:
    def __init__(self) -> None:
        self.said: list[tuple[Call, Radio]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def say(self, call, clip, radio, issued_at) -> None:
        self.said.append((call, radio))

    async def close(self) -> None:
        pass


def test_settings_from_config():
    s = CalloutSettings.from_config({"callouts": {**CONFIG["callouts"], "calls": ["wave off", "power"],
                                                  "thresholds": {"little_deg": 0.4, "bogus": 1}}})
    assert s.enabled and s.srs.port == 5002
    assert s.calls == {Call.WAVE_OFF, Call.POWER}
    assert s.thresholds.little_deg == 0.4
    assert s.radio_for("CVN-75 Harry S. Truman") == Radio(127.6, Modulation.AM)
    assert s.radio_for("some other carrier") == Radio(127.5, Modulation.AM)
    assert not CalloutSettings.from_config({}).enabled


def test_clip_library(clips):
    lib = ClipLibrary.load(clips)
    assert set(lib.clips) == set(Call) and lib.voice == "test-tones"
    assert lib[Call.POWER].seconds == pytest.approx(0.2) and lib[Call.POWER].text == "Power."


async def run_collector(source: Path, work: Path, clips: Path, mode: str = "server", client=None):
    server = await serve_recording(source, port=0, speed=0)
    port = server.sockets[0].getsockname()[1]
    sink = RecordingSink()
    async with server:
        collector = Collector(CollectorConfig(work_dir=work, tacview_port=port, mode=mode, voice_dir=clips),
                              client=client)
        collector.remote_config = CONFIG
        collector.refresh_config = _no_refresh  # keep the config set above
        collector.sink_factory = lambda settings: sink
        session = await collector.run_session()
    return collector, session, sink


async def _no_refresh() -> None:
    return None


def test_live_calls_are_spoken_and_uploaded(tmp_path, clips):
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    token = central.add_source("edge")
    app = create_app(central)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            collector, session, sink = await run_collector(FLAT_LOW_CUT, tmp_path / "edge", clips, client=client)
            assert await collector.upload_once() == (1, 0)
            page = (await client.get("/passes/1")).text
            listed = (await client.get("/api/v1/passes", params={"days": 0})).json()
        return sink, listed, page

    sink, listed, page = asyncio.run(run())
    assert sink.started  # connected at session start, not on the first call
    said = [call for call, _ in sink.said]
    # The flat, low pass: power calls (escalating), then a wave-off (and nothing after it).
    from dcs_lso.callouts.rules import POWER_CALLS
    assert said[0] is Call.POWER and Call.POWER_X2 in said
    assert sum(c in POWER_CALLS for c in said) >= 2 and said[-1] is Call.WAVE_OFF
    assert all(radio == Radio(127.6, Modulation.AM) for _, radio in sink.said)  # the Truman's frequency
    (row,) = listed
    assert [c["call"] for c in row["calls"]] == [c.value for c in said]
    assert "LSO calls:" in page and "Wave off" in page


def test_pilot_mode_never_transmits(tmp_path, clips):
    _, session, sink = asyncio.run(run_collector(FLAT_LOW_CUT, tmp_path / "edge", clips, mode="pilot"))
    assert sink.said == [] and session.callouts is None


def test_config_is_fetched_and_cached(tmp_path):
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    token = central.add_source("edge")
    central.set_config("edge", CONFIG)
    app = create_app(central)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            collector = Collector(CollectorConfig(work_dir=tmp_path / "edge"), client=client)
            await collector.refresh_config()
            return collector

    collector = asyncio.run(run())
    assert collector.remote_config == CONFIG
    # A restarted collector with central unreachable still has the cached copy.
    offline = Collector(CollectorConfig(work_dir=tmp_path / "edge"))
    assert offline.remote_config == CONFIG


def test_wave_off_cuts_off_the_current_call():
    class SlowClient:
        connected = True
        sent: list[str] = []

        async def transmit(self, frames, radios):
            self.sent.append(f"start {len(frames)}")
            await asyncio.sleep(len(frames) * 0.04)
            self.sent.append(f"done {len(frames)}")

        async def close(self):
            pass

    from dcs_lso.callouts.voice import Clip

    async def run():
        import time
        sink = SrsSink(CalloutSettings().srs, [Radio(127.5)])
        sink._client = SlowClient()
        long_call = asyncio.create_task(sink.say(Call.HIGH, Clip("x", [b"f"] * 25), Radio(127.5), time.monotonic()))
        await asyncio.sleep(0.2)
        await sink.say(Call.WAVE_OFF, Clip("w", [b"f"] * 3), Radio(127.5), time.monotonic())
        await long_call
        return sink._client.sent

    assert asyncio.run(run()) == ["start 25", "start 3", "done 3"]


def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("create table sources (id integer primary key, name varchar(100) unique, kind varchar(16), "
                "token_hash varchar(64) unique, created_at datetime)")
    con.commit()
    con.close()
    Central(f"sqlite:///{db}", tmp_path)
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(sources)")}
    assert "config" in cols


@pytest.mark.skipif(not os.environ.get("SRS_SERVER_BIN"), reason="SRS_SERVER_BIN not set")
def test_srs_sink_against_real_server(srs_server_port, clips):
    async def run():
        heard = []
        rx = SrsClient("127.0.0.1", srs_server_port, radios=(Radio(127.5),), on_voice=lambda p, t: heard.append(p))
        await rx.connect()
        settings = CalloutSettings.from_config({"callouts": {**CONFIG["callouts"], "srs": {"port": srs_server_port}}})
        sink = SrsSink(settings.srs, [Radio(127.5)])
        lib = ClipLibrary.load(clips)
        import time
        await sink.say(Call.POWER, lib[Call.POWER], Radio(127.5), time.monotonic())
        await asyncio.sleep(0.3)
        await sink.close()
        await rx.close()
        return heard, lib[Call.POWER].frames

    heard, frames = asyncio.run(run())
    assert [p.audio for p in heard] == frames


@pytest.fixture(scope="module")
def srs_server_port(tmp_path_factory):
    import socket
    import subprocess
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([os.environ["SRS_SERVER_BIN"], "--port", str(port)], cwd=tmp_path_factory.mktemp("srs"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.parametrize(("name", "bolter"), [
    ("20260927-204347_Wrycu_3209s", True),
    ("20260927-204347_Wrycu_3348s", True),
    ("20260927-204347_Wrycu_4013s", False),
    ("20260927-204347_Wrycu_4769s", False),
    ("20260928-025423_New_callsign_86s", False),
])
def test_bolter_is_called_only_for_bolters(tmp_path, clips, name, bolter):
    _, _, sink = asyncio.run(run_collector(FIXTURES / "passes" / f"{name}.zip.acmi", tmp_path / "edge", clips))
    said = [call for call, _ in sink.said]
    assert (Call.BOLTER in said) is bolter
    assert said.count(Call.BOLTER) <= 1
