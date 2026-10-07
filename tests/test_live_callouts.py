import asyncio
import json
import os
import sqlite3
import shutil
import wave
from pathlib import Path

import httpx
import pytest

from dcs_lso.acmi.stream import serve_recording
from dcs_lso.callouts.rules import Call
from dcs_lso.callouts.voice import DIGITS, PHRASES, ClipLibrary, clip_name, variants
from dcs_lso.hub.app import create_app
from dcs_lso.hub.service import Hub
from dcs_lso.agent.callouts import CalloutSettings, SrsSink
from dcs_lso.agent.service import Agent, AgentConfig
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
    for call in PHRASES:
        manifest["clips"][call.name] = []
        for i, text in enumerate(variants(call)):
            with wave.open(str(d / clip_name(call, i)), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(tone(0.2).tobytes())
            manifest["clips"][call.name].append({"file": clip_name(call, i), "text": text})
    manifest["numbers"] = {}
    for digit, word in DIGITS.items():
        with wave.open(str(d / f"number_{digit}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(tone(0.12).tobytes())
        manifest["numbers"][digit] = {"file": f"number_{digit}.wav", "text": word}
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


class RecordingSink:
    def __init__(self) -> None:
        self.said: list[tuple[Call, Radio]] = []
        self.texts: list[str] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def say(self, call, clip, radio, issued_at) -> None:
        self.said.append((call, radio))
        self.texts.append(clip.text)

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
    assert s.voice is None and CalloutSettings.from_config({"callouts": {"voice": "clips-amy"}}).voice == "clips-amy"


def test_the_hubs_config_picks_the_voice(tmp_path, clips):
    from dcs_lso.agent.service import Agent, AgentConfig
    from dcs_lso.callouts.voice import choose_clip_set
    voices = tmp_path / "voices"
    for name in ("clips-amy", "clips-ryan"):
        shutil.copytree(clips, voices / name)
    (voices / "en_US-ryan-high.onnx").write_bytes(b"")  # other things in the folder are ignored
    assert choose_clip_set(voices, "clips-ryan")[0] == voices / "clips-ryan"
    assert choose_clip_set(voices, "nope") == (voices / "clips-amy",
                                               "voice: clips-amy (no clip set 'nope'; have clips-amy, clips-ryan)")
    assert choose_clip_set(voices, None)[0] == voices / "clips-amy"
    assert choose_clip_set(clips, "clips-ryan")[0] == clips  # a single clip set: used whatever the config says
    assert choose_clip_set(tmp_path / "empty", None)[0] is None
    agent = Agent(AgentConfig(work_dir=tmp_path / "edge", voice_dir=voices))
    assert agent._clips("clips-ryan") is agent._clips("clips-ryan")  # loaded once
    assert agent._clips("clips-amy") is not agent._clips("clips-ryan")


def test_clip_library(clips):
    lib = ClipLibrary.load(clips)
    assert set(lib.clips) == set(Call) and lib.voice == "test-tones"
    assert lib[Call.POWER].seconds == pytest.approx(0.2) and lib[Call.POWER].text == "Power."


async def run_collector(source: Path, work: Path, clips: Path, mode: str = "server", client=None, hooks=None):
    server = await serve_recording(source, port=0, speed=0)
    port = server.sockets[0].getsockname()[1]
    sink = RecordingSink()
    async with server:
        agent = Agent(AgentConfig(work_dir=work, tacview_port=port, mode=mode, voice_dir=clips),
                              client=client)
        agent.hooks = hooks
        agent.remote_config = CONFIG
        agent.refresh_config = _no_refresh  # keep the config set above
        agent.sink_factory = lambda settings: sink
        session = await agent.run_session()
    return agent, session, sink


async def _no_refresh() -> None:
    return None


def test_live_calls_are_spoken_and_uploaded(tmp_path, clips):
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    token = hub.add_source("edge")
    app = create_app(hub)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            agent, session, sink = await run_collector(FLAT_LOW_CUT, tmp_path / "edge", clips, client=client)
            assert await agent.upload_once() == (1, 0)
            page = (await client.get("/passes/1")).text
            listed = (await client.get("/api/v1/passes", params={"days": 0})).json()
        return sink, listed, page

    sink, listed, page = asyncio.run(run())
    assert sink.started  # connected at session start, not on the first call
    said = [call for call, _ in sink.said]
    # The flat, low pass: power calls (escalating), a wave-off, and (as the pilot trapped anyway)
    # nothing after it but the salty welcome.
    from dcs_lso.callouts.rules import POWER_CALLS
    assert said[0] is Call.POWER and Call.POWER_X2 in said
    assert sum(c in POWER_CALLS for c in said) >= 2 and said[-2:] == [Call.WAVE_OFF, Call.TRAPPED_WAVED_OFF]
    assert all(radio == Radio(127.6, Modulation.AM) for _, radio in sink.said)  # the Truman's frequency
    (row,) = listed
    assert [c["call"] for c in row["calls"]] == [c.value for c in said]
    assert "LSO calls:" in page and "Wave off" in page


def test_pilot_mode_never_transmits(tmp_path, clips):
    _, session, sink = asyncio.run(run_collector(FLAT_LOW_CUT, tmp_path / "edge", clips, mode="pilot"))
    assert sink.said == [] and session.callouts is None


def test_config_is_fetched_and_cached(tmp_path):
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    token = hub.add_source("edge")
    hub.set_config("edge", CONFIG)
    app = create_app(hub)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            agent = Agent(AgentConfig(work_dir=tmp_path / "edge"), client=client)
            await agent.refresh_config()
            return agent

    agent = asyncio.run(run())
    assert agent.remote_config == CONFIG
    # A restarted agent with hub unreachable still has the cached copy.
    offline = Agent(AgentConfig(work_dir=tmp_path / "edge"))
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
    Hub(f"sqlite:///{db}", tmp_path)
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


@pytest.mark.parametrize(("name", "outcome"), [
    ("passes/20260927-204347_Wrycu_3209s", Call.BOLTER),
    ("passes/20260927-204347_Wrycu_3348s", Call.BOLTER),
    ("passes/20260927-204347_Wrycu_4013s", Call.TRAPPED),
    ("passes/20260927-204347_Wrycu_4769s", Call.TRAPPED_WAVED_OFF),  # trapped through our wave-off
    ("passes/20260928-025423_New_callsign_86s", Call.TRAPPED),
    # A real wire-4 trap after our wave-off (2026-10-04), as the server agent saw it: the server's copy of the
    # jet held full speed until 69 m past wire 4. It was called a bolter when the bolter line was 60 m past it.
    ("live/wire4-trap-server", Call.TRAPPED_WAVED_OFF),
])
def test_bolter_or_welcome_is_called_once(tmp_path, clips, name, outcome):
    _, _, sink = asyncio.run(run_collector(FIXTURES / f"{name}.zip.acmi", tmp_path / "edge", clips))
    said = [call for call, _ in sink.said]
    called = [c for c in said if c in (Call.BOLTER, Call.TRAPPED, Call.TRAPPED_WAVED_OFF)]
    assert called == [outcome] and said[-1] is outcome


def test_variants_are_picked_at_random(clips):
    lib = ClipLibrary.load(clips)
    assert len(lib.clips[Call.TRAPPED]) > 1
    assert {lib.pick(Call.TRAPPED).text for _ in range(200)} == set(PHRASES[Call.TRAPPED])
    assert lib.durations()[Call.POWER] == pytest.approx(0.2)


def test_old_single_clip_manifest_still_loads(clips):
    manifest = json.loads((clips / "manifest.json").read_text())
    manifest["clips"] = {k: v[0] for k, v in manifest["clips"].items()}
    (clips / "manifest.json").write_text(json.dumps(manifest))
    lib = ClipLibrary.load(clips)
    assert lib[Call.POWER].text == "Power." and len(lib.clips[Call.TRAPPED]) == 1


class WireHooks:
    """Stands in for HookFeed: DCS reports `wire` for every aircraft (None: never)."""

    def __init__(self, wire):
        self.wire = wire
        self.asked = 0

    def live_wire(self, tacview_id, since):
        self.asked += 1
        return self.wire

    def wind_for(self, carrier_unit):
        return None

    def wire_for(self, tacview_id, start, end):
        return None

    def slot_for(self, pilot, before):
        return None

    def carrier_radio(self, carrier_unit):
        return None

    def debrief(self):
        from dcs_lso.dcslog import Debrief
        return Debrief(None, [])


@pytest.mark.parametrize(("name", "wire", "expected"), [
    ("20260927-204347_Wrycu_4013s", 2, Call.TRAPPED_WIRE_2),
    ("20260927-204347_Wrycu_4769s", 3, Call.TRAPPED_WAVED_OFF_WIRE_3),  # trapped through our wave-off
    ("20260927-204347_Wrycu_4013s", None, Call.TRAPPED),  # DCS never reported a wire: the plain welcome
    ("20260927-204347_Wrycu_4769s", None, Call.TRAPPED_WAVED_OFF),
])
def test_welcome_names_dcs_wire(tmp_path, clips, monkeypatch, name, wire, expected):
    import dcs_lso.agent.callouts as callouts_mod
    monkeypatch.setattr(callouts_mod, "WIRE_WAIT_S", 0.2)
    hooks = WireHooks(wire)
    agent, _, sink = asyncio.run(run_collector(FIXTURES / "passes" / f"{name}.zip.acmi", tmp_path / "edge",
                                                   clips, hooks=hooks))
    said = [call for call, _ in sink.said]
    assert said[-1] is expected and hooks.asked >= 1
    (item,) = agent.outbox.pending()
    assert item.meta()["calls"][-1]["call"] == expected.value  # the trap card shows what was said


def test_hook_feed_reads_the_wire_from_dcs_grade(tmp_path):
    from dcs_lso.dcslog import HookEvent
    from dcs_lso.agent.service import HookFeed
    feed = HookFeed.__new__(HookFeed)  # no dcs.log follower thread
    import threading
    feed._events, feed._lock = [], threading.Lock()
    feed.add(HookEvent("landing_quality_mark", 362.1, "LSO: GRADE:C : _EGTL_  3PTSIW  WIRE# 2[BC]",
                       {"name": "Wrycu", "object_id": 16777474}, {"name": "CVN-75 Harry S. Truman"}, None, {}))
    assert feed.live_wire(0x103, since=340.0) == 2
    assert feed.live_wire(0x103, since=370.0) is None  # an earlier landing's grade
    assert feed.live_wire(0x203, since=340.0) is None  # someone else's


@pytest.mark.parametrize(("path", "glideslope", "grade", "praised"), [
    # OK when graded against a 3.6 deg glideslope (as until grading v7); Fair against 3.5.
    (FIXTURES / "wires" / "server-dcs-wire-2.zip.acmi", 3.6, "OK", True),
    (FIXTURES / "live" / "trap-server.zip.acmi", None, "---", False),
])
def test_only_good_passes_get_a_nice_trap(tmp_path, clips, monkeypatch, path, glideslope, grade, praised):
    """Welcomes that compliment the landing only for passes we grade OK or better."""
    from dataclasses import replace
    import dcs_lso.callouts.voice as voice
    from dcs_lso.detect import find_passes
    from dcs_lso.geometry import AIRCRAFT
    from dcs_lso.grading import grade_pass
    if glideslope is not None:
        monkeypatch.setitem(AIRCRAFT, "FA-18C_hornet", replace(AIRCRAFT["FA-18C_hornet"], glideslope=glideslope))
    (p,) = find_passes(__import__("dcs_lso.acmi", fromlist=["load_recording"]).load_recording(path))
    assert grade_pass(p).grade.value == grade
    # Always pick a complimenting welcome when one is allowed.
    monkeypatch.setattr(voice.random, "choice", lambda clips: next((c for c in clips if voice.is_praise(c.text)), clips[0]))
    _, _, sink = asyncio.run(run_collector(path, tmp_path / "edge", clips))
    assert sink.said[-1][0] is Call.TRAPPED
    assert voice.is_praise(sink.texts[-1]) is praised


def test_praise_is_never_picked_when_not_allowed(clips):
    from dcs_lso.callouts.voice import is_praise
    lib = ClipLibrary.load(clips)
    assert {is_praise(lib.pick(Call.TRAPPED, praise=False).text) for _ in range(300)} == {False}
    assert {is_praise(lib.pick(Call.TRAPPED_WIRE_3, praise=False).text) for _ in range(300)} == {False}
    assert True in {is_praise(lib.pick(Call.TRAPPED, praise=True).text) for _ in range(300)}



def test_side_number_goes_before_a_call(clips):
    lib = ClipLibrary.load(clips)
    power = lib[Call.POWER]
    numbered = lib.with_side_number(power, "301")
    assert numbered.text == "301, Power."
    assert len(numbered.frames) == 3 * len(lib.numbers["3"].frames) + 2 + len(power.frames)  # 3 digits, gap, call
    assert lib.with_side_number(power, None) is power and lib.with_side_number(power, "") is power


def test_foul_deck_is_another_aircraft_in_the_landing_area():
    from dcs_lso.acmi import ObjectTrack, Sample, Transform
    from dcs_lso.agent.live import LivePassDetector
    from dcs_lso.geometry import FA18C, NIMITZ, CarrierPose, DeckFrame
    frame = DeckFrame(NIMITZ, FA18C)
    pose = CarrierPose(u=0.0, v=0.0, alt=0.0, heading=0.0)
    detector = LivePassDetector()

    def parked(object_id: int, along: float, lateral: float) -> None:
        """A jet whose hook sits on deck at (along, lateral) in the landing-area frame."""
        def at(u: float, v: float) -> Transform:
            return Transform(u=u, v=v, alt=frame.carrier.deck_altitude + 2.24, roll=0.0, pitch=0.0, heading=0.0)
        u, v = 0.0, 0.0
        for _ in range(4):  # Newton steps on the (linear) map from (u, v) to (along, lateral)
            p0 = frame.position(pose, at(u, v))
            pu, pv = frame.position(pose, at(u + 1, v)), frame.position(pose, at(u, v + 1))
            j = ((pu.along - p0.along, pv.along - p0.along), (pu.lateral - p0.lateral, pv.lateral - p0.lateral))
            ea, el = along - p0.along, lateral - p0.lateral
            det = j[0][0] * j[1][1] - j[0][1] * j[1][0]
            u += (ea * j[1][1] - el * j[0][1]) / det
            v += (j[0][0] * el - j[1][0] * ea) / det
        track = ObjectTrack(object_id, {"Type": "Air+FixedWing", "Name": "FA-18C_hornet"})
        track.samples.append(Sample(10.0, at(u, v), None))
        detector.tracks[object_id] = track

    parked(0x201, along=-20.0, lateral=2.0)  # just past the wires, still in the landing area
    pos = frame.position(pose, detector.tracks[0x201].samples[-1].transform)
    assert abs(pos.along + 20.0) < 0.1 and abs(pos.lateral - 2.0) < 0.1 and abs(pos.hook_height) < 0.5
    assert detector.landing_area_foul(0x101, pose, frame, exclude=0x301, now=10.5)
    assert not detector.landing_area_foul(0x101, pose, frame, exclude=0x201, now=10.5)  # that's the one landing
    assert not detector.landing_area_foul(0x101, pose, frame, exclude=0x301, now=20.0)  # long gone
    detector.tracks.clear()
    parked(0x202, along=-60.0, lateral=40.0)  # parked forward on the starboard side: clear of the landing area
    assert not detector.landing_area_foul(0x101, pose, frame, exclude=0x301, now=10.5)


def test_foul_deck_waves_the_pilot_off(tmp_path, clips, monkeypatch):
    from dcs_lso.agent.live import LivePassDetector
    monkeypatch.setattr(LivePassDetector, "landing_area_foul", lambda self, *args: True)
    _, _, sink = asyncio.run(run_collector(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi",
                                           tmp_path / "edge", clips))
    said = [call for call, _ in sink.said]
    assert Call.WAVE_OFF_FOUL_DECK in said
    assert said[said.index(Call.WAVE_OFF_FOUL_DECK) + 1:] in ([], [Call.TRAPPED_WAVED_OFF])  # nothing else after it


class SlotHooks(WireHooks):
    def __init__(self, numbers: dict[str, str]):
        super().__init__(None)
        self.numbers = numbers

    def slot_for(self, pilot, before):
        return {"onboard_num": self.numbers[pilot]} if pilot in self.numbers else None


def test_side_numbers_when_two_jets_are_in_the_groove(tmp_path, clips):
    from test_backfill import two_pilots
    path = two_pilots(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi", tmp_path / "two.zip.acmi")
    _, _, sink = asyncio.run(run_collector(path, tmp_path / "edge", clips,
                                           hooks=SlotHooks({"Wrycu": "301", "Maverick": "302"})))
    groove_calls = [text for (call, _), text in zip(sink.said, sink.texts) if call not in WELCOMES]
    assert groove_calls and all(t.startswith(("301, ", "302, ")) for t in groove_calls)
    assert {t[:3] for t in groove_calls} == {"301", "302"}
    # (The copy flies exactly the same path, so each also finds the other in the landing area.)
    assert Call.WAVE_OFF_FOUL_DECK in [c for c, _ in sink.said]
    # One jet alone: no side numbers.
    _, _, alone = asyncio.run(run_collector(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi",
                                            tmp_path / "edge2", clips, hooks=SlotHooks({"Wrycu": "301"})))
    assert not any(t.startswith("301") for t in alone.texts)


WELCOMES = {Call.TRAPPED, Call.TRAPPED_WAVED_OFF, Call.BOLTER} | {c for c in Call if c.name.startswith("TRAPPED_")}



def test_hook_feed_reads_each_carriers_mission_frequency():
    import threading

    from dcs_lso.dcslog import parse_hook_line
    from dcs_lso.agent.service import HookFeed
    feed = HookFeed.__new__(HookFeed)
    feed._events, feed._lock = [], threading.Lock()
    line = ('2026-10-03 12:00:00.000 INFO    DCSLSO (Main): DCSLSO {"event":"carrier","t":0,'
            '"name":"CVN-75 Harry S. Truman","type":"CVN_75","frequency":127500000,"modulation":0}')
    feed.add(parse_hook_line(line))
    feed.add(parse_hook_line(line.replace("CVN-75 Harry S. Truman", "Tarawa").replace("127500000", "264000000")
                             .replace('"modulation":0', '"modulation":1')))
    assert feed.carrier_radio("CVN-75 Harry S. Truman") == Radio(127.5, Modulation.AM)
    assert feed.carrier_radio("Tarawa") == Radio(264.0, Modulation.FM)
    assert feed.carrier_radio("CVN-71 Theodore Roosevelt") is None


def test_carrier_radio_precedence():
    settings = CalloutSettings.from_config(CONFIG)  # sets the Truman to 127.6 and a default of 127.5
    detected = Radio(127.75)
    assert settings.radio_for("CVN-75 Harry S. Truman", detected) == Radio(127.6)  # the config wins
    assert settings.radio_for("CVN-71 Theodore Roosevelt", detected) == detected  # then the mission
    assert settings.radio_for("CVN-71 Theodore Roosevelt") == Radio(127.5)  # then the default


def test_srs_sink_announces_a_detected_frequency_before_using_it():
    from dcs_lso.callouts.voice import Clip

    class FakeClient:
        connected = True

        def __init__(self):
            self.events = []

        async def set_radios(self, radios):
            self.events.append(("radios", tuple(radios)))

        async def transmit(self, frames, radios):
            self.events.append(("transmit", tuple(radios)))

        async def close(self):
            pass

    async def run():
        import time
        sink = SrsSink(CalloutSettings().srs, [Radio(127.5)])
        sink._client = FakeClient()
        await sink.say(Call.POWER, Clip("p", [b"f"]), Radio(127.75), time.monotonic())
        await sink.say(Call.POWER, Clip("p", [b"f"]), Radio(127.75), time.monotonic())
        return sink._client.events

    assert asyncio.run(run()) == [("radios", (Radio(127.5), Radio(127.75))), ("transmit", (Radio(127.75),)),
                                  ("transmit", (Radio(127.75),))]  # announced once


class CarrierHooks(WireHooks):
    def __init__(self):
        super().__init__(None)

    def carrier_radio(self, carrier_unit):
        return Radio(127.75) if carrier_unit == "CVN-75 Harry S. Truman" else None


def test_calls_go_out_on_the_frequency_set_in_the_mission(tmp_path, clips):
    async def run():
        server = await serve_recording(FLAT_LOW_CUT, port=0, speed=0)
        sink = RecordingSink()
        async with server:
            agent = Agent(AgentConfig(work_dir=tmp_path, tacview_port=server.sockets[0].getsockname()[1],
                                                  voice_dir=clips))
            agent.hooks = CarrierHooks()
            agent.remote_config = {"callouts": {"enabled": True, "frequency_mhz": 127.5}}  # no carrier settings
            agent.refresh_config = _no_refresh
            agent.sink_factory = lambda settings: sink
            await agent.run_session()
        return sink

    sink = asyncio.run(run())
    assert sink.said and all(radio == Radio(127.75) for _, radio in sink.said)


class PlayerHooks(WireHooks):
    """The server hook's view: who is a player in this mission."""

    def __init__(self, players: set[str] | None):
        super().__init__(None)
        self.players = players

    def player_names(self):
        return self.players


@pytest.mark.parametrize(("players", "uploaded"), [
    ({"Maverick"}, False),  # Wrycu isn't a player here: an AI jet as far as the server knows
    ({"Wrycu"}, True),
    (None, True),  # no player list from the hook (older hook): can't tell, so upload as before
])
def test_ai_passes_get_calls_but_are_not_uploaded(tmp_path, clips, players, uploaded):
    collector, session, sink = asyncio.run(run_collector(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi",
                                                         tmp_path / "edge", clips, hooks=PlayerHooks(players)))
    assert any(call is Call.TRAPPED or call in WELCOMES for call, _ in sink.said)  # the LSO still talked to it
    assert bool(collector.outbox.pending()) is uploaded


@pytest.mark.parametrize(("roll", "dig"), [(0.0, True), (0.99, False)])
def test_rough_landings_get_a_dig_now_and_then(tmp_path, clips, monkeypatch, roll, dig):
    """A poor pass (graded No Grade) gets a dig after its welcome, but only by chance (default one in three)."""
    import dcs_lso.agent.callouts as callouts_mod
    from dcs_lso.callouts.voice import PHRASES
    monkeypatch.setattr(callouts_mod.random, "random", lambda: roll)
    _, _, sink = asyncio.run(run_collector(FIXTURES / "live" / "trap-server.zip.acmi", tmp_path / "edge", clips))
    assert sink.said[-1][0] is Call.TRAPPED
    assert any(sink.texts[-1].endswith(d) for d in PHRASES[Call.ROUGH_LANDING]) is dig


def test_a_visitor_is_welcomed_aboard_not_home(tmp_path, clips, monkeypatch):
    """The jet in this recording never sat on the carrier's deck before its trap: "welcome aboard", never "home"."""
    import dcs_lso.agent.callouts as callouts_mod
    import dcs_lso.callouts.voice as voice
    monkeypatch.setattr(callouts_mod.random, "random", lambda: 0.99)  # no dig: the last pick is the welcome
    picked = []
    real = voice.random.choice
    monkeypatch.setattr(voice.random, "choice", lambda options: picked.append([c.text for c in options]) or real(options))
    _, _, sink = asyncio.run(run_collector(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi",
                                           tmp_path / "edge", clips))
    welcome_options = picked[-1]
    assert welcome_options and all(voice.welcome_kind(t) != "home" for t in welcome_options)


class Said:
    """A sink that records what would be said, and on which radio."""

    def __init__(self):
        self.said: list[tuple[Call, float]] = []

    async def start(self): ...

    async def say(self, call, clip, radio, issued_at):
        self.said.append((call, radio.frequency_mhz))

    async def close(self): ...


def test_pilots_calls_are_answered_for_the_jet_that_made_them(clips):
    from dcs_lso.acmi import ObjectTrack
    from dcs_lso.agent.callouts import LiveCallouts
    from dcs_lso.agent.listening import Heard
    from dcs_lso.callouts.heard import parse

    sink = Said()
    callouts = LiveCallouts(CalloutSettings.from_config(CONFIG), ClipLibrary.load(clips), sink)
    callouts.side_number_for = lambda pilot, t: {"Wrycu": "305", "Goose": "214"}.get(pilot)
    carrier = ObjectTrack(1, {"Name": "CVN_75", "Pilot": "CVN-75 Harry S. Truman"})  # its LSO is on 127.6
    wrycu = ObjectTrack(2, {"Name": "FA-18C_hornet", "Pilot": "Wrycu"})
    goose = ObjectTrack(3, {"Name": "FA-18C_hornet", "Pilot": "Goose"})

    def hear(text, speaker, mhz=127.6):
        callouts.on_heard(Heard(parse(text), speaker, 0, mhz * 1e6, 0.0))

    async def run():
        callouts._live = {(1, 2): (carrier, wrycu, 100.0, 1200.0), (1, 3): (carrier, goose, 100.0, 3000.0)}
        hear("three zero five hornet ball five point two", "wrycu")  # by name (case doesn't matter)
        hear("three zero five hornet ball five point two", "Wrycu")  # said already this pass: not again
        hear("two one four hornet ball six point zero", "Mav")  # an SRS name we don't know: by side number
        hear("hornet ball five point five", "Mav")  # nothing to tell two jets apart: not answered
        hear("three zero five hornet ball five point two", "Wrycu", mhz=251.0)  # not this carrier's frequency
        hear("paddles three zero five", "Mav", mhz=127.5)  # a radio check: answered whoever it is
        await asyncio.sleep(0)
        callouts._live.pop((1, 3))
        hear("clara", "Mav")  # one jet left in the groove: it's theirs
        await asyncio.sleep(0)

    asyncio.run(run())
    assert sink.said == [(Call.ROGER_BALL, 127.6), (Call.ROGER_BALL, 127.6), (Call.LOUD_AND_CLEAR, 127.5),
                         (Call.ROGER_CLARA, 127.6)]
    assert [(m.aircraft_id, m.call) for m in callouts.made] == [(2, Call.ROGER_BALL), (3, Call.ROGER_BALL),
                                                                 (2, Call.ROGER_CLARA)]


def test_the_listener_hands_recognised_calls_on():
    from dcs_lso.agent.listening import Listener
    from dcs_lso.callouts.heard import PilotCall
    from dcs_lso.srs.listen import END_GAP_S
    from dcs_lso.srs.opus import encode_pcm
    from dcs_lso.srs.packet import VoicePacket

    class Recognises:
        def hear(self, pcm):
            return PilotCall("ball", "305", "Hornet", 5.2, "three zero five hornet ball five point two")

    class Sink:
        on_voice = None
        client = type("C", (), {"clients": {"G" * 22: {"Name": "Wrycu"}}, "guid": "O" * 22})()

    heard = []
    listener = Listener(Recognises(), heard.append)
    listener.attach(sink := Sink())
    import time as _time
    for n, frame in enumerate(encode_pcm(tone(0.5))):
        sink.on_voice(VoicePacket(frame, (127.6e6,), (0,), (0,), 42, n, "G" * 22), _time.perf_counter())

    async def run():
        task = asyncio.create_task(listener.run())
        await asyncio.sleep(END_GAP_S + 0.3)
        task.cancel()

    asyncio.run(run())
    (h,) = heard
    assert (h.speaker, h.unit_id, h.frequency_hz, h.call.call) == ("Wrycu", 42, 127.6e6, "ball")
