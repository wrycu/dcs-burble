"""Recording the carrier frequency's radio with each pass, for its replay (PLAN #52): on per server in the hub's config,
only the pass's pilot (if their SRS client allows recording) and the LSO, deletable by the pilot or an admin."""

import asyncio
import io
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from burble.acmi import load_recording
from burble.agent.callouts import CalloutSettings, SrsSink, SrsSettings
from burble.agent.radio import RadioRecorder, radio_sidecar
from burble.callouts.voice import Clip
from burble.detect import find_passes
from burble.hub.app import create_app
from burble.hub.service import Hub, IngestError
from burble.slices import sidecar, slice_objects
from burble.srs import Radio
from burble.srs.opus import encode_pcm, tone
from burble.srs.packet import VoicePacket

FIXTURES = Path(__file__).parent / "fixtures"
PASS = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"
LSO_HZ = 127.5e6
FRAMES = encode_pcm(tone(0.4))  # 10 real Opus frames


class FakeSink:
    def __init__(self, clients):
        self.on_voice = None
        self.on_transmit = None
        self.client = SimpleNamespace(guid="O" * 22, clients=clients)


def packet(guid, frame, mhz=127.5):
    return VoicePacket(frame, (mhz * 1e6,), (0,), (0,), 1, 1, guid)


def recorder_hearing(clients):
    clock = SimpleNamespace(now=100.0)
    recorder = RadioRecorder(lambda: clock.now)
    sink = FakeSink(clients)
    heard = []
    sink.on_voice = lambda p, at: heard.append(p)  # a listener attached first still hears everything
    recorder.attach(sink)
    return recorder, sink, clock, heard


CLIENTS = {"W" * 22: {"Name": "Wrycu", "AllowRecord": True}, "G" * 22: {"Name": "Goose", "AllowRecord": True},
           "M" * 22: {"Name": "Maverick", "AllowRecord": False}}


def test_the_pass_gets_its_pilot_and_the_lso_only():
    recorder, sink, clock, heard = recorder_hearing(CLIENTS)
    for i, frame in enumerate(FRAMES[:5]):  # Wrycu: "305, Hornet ball, 5.2"
        sink.on_voice(packet("W" * 22, frame), 10.0 + i * 0.04)
    clock.now = 101.0
    sent = sink.on_transmit(Radio(127.5))  # the LSO answers: frames put in as they go out
    sent.extend(FRAMES[:3])
    for i, frame in enumerate(FRAMES[:4]):
        sink.on_voice(packet("G" * 22, frame), 11.0 + i * 0.04)  # someone else on the frequency
        sink.on_voice(packet("M" * 22, frame), 11.0 + i * 0.04)  # doesn't allow recording
        sink.on_voice(packet("W" * 22, frame, mhz=251.0), 11.0 + i * 0.04)  # another frequency
        sink.on_voice(packet("O" * 22, frame), 11.0 + i * 0.04)  # ourselves
    clock.now = 104.0
    for frame in FRAMES[:2]:  # Wrycu again, after a pause: a second transmission
        sink.on_voice(packet("W" * 22, frame), 14.0)
    assert len(heard) == 23  # the listener heard all of it
    got = recorder.for_pass("wrycu", LSO_HZ, 90.0, 110.0)  # names compared as SRS's and Tacview's differ
    assert [(r.by, r.name, r.time, len(r.frames)) for r in got] == [
        ("pilot", "Wrycu", 100.0, 5), ("lso", "", 101.0, 3), ("pilot", "Wrycu", 104.0, 2)]
    assert [r.by for r in recorder.for_pass("Maverick", LSO_HZ, 90.0, 110.0)] == ["lso"]  # never recorded
    assert recorder.for_pass("Wrycu", LSO_HZ, 102.0, 103.0) == []  # outside the pass
    assert [r.by for r in recorder.for_pass("Wrycu", 251e6, 0.0, 200.0)] == ["pilot"]


def test_sidecar_round_trip_and_checks():
    from burble.hub.radio import parse_radio

    recorder, sink, clock, _ = recorder_hearing(CLIENTS)
    for frame in FRAMES:
        sink.on_voice(packet("W" * 22, frame), 10.0)
    meta = radio_sidecar(recorder.for_pass("Wrycu", LSO_HZ, 0.0, 200.0))
    assert meta["transmissions"][0]["name"] == "Wrycu"
    assert parse_radio(meta)
    for bad in ({**meta, "codec": "mp3"}, {**meta, "transmissions": []},
                {**meta, "transmissions": [{"time": 1.0, "by": "pilot", "frames": "not base64!"}]},
                {**meta, "transmissions": [{"time": 1.0, "by": "someone", "frames": ""}]}):
        with pytest.raises(ValueError):
            parse_radio(bad)


def test_config_and_the_srs_name_says_recording():
    assert CalloutSettings.from_config({"callouts": {"enabled": True}}).record_radio  # on by default
    assert not CalloutSettings.from_config({"callouts": {"enabled": True, "record_radio": False}}).record_radio
    assert SrsSink(SrsSettings(name="LSO"), [Radio(127.5)], recording=True).name == "LSO (recording)"
    assert SrsSink(SrsSettings(name="LSO"), [Radio(127.5)]).name == "LSO"


def test_the_lso_is_recorded_as_sent():
    class Client:
        connected = True

        async def transmit(self, frames, radios, on_sent=None):
            for i, _ in enumerate(frames[:4]):  # cut off after four frames (e.g. by a wave-off)
                on_sent(i, 0.0)

    from burble.callouts.rules import Call
    sink = SrsSink(SrsSettings(), [Radio(127.5)], recording=True)
    sink._client = Client()
    recorded = []
    sink.on_transmit = lambda radio: recorded.append((radio, [])) or recorded[-1][1]
    asyncio.run(sink.say(Call.POWER, Clip("Power.", FRAMES), Radio(127.5), __import__("time").monotonic()))
    assert recorded == [(Radio(127.5), FRAMES[:4])]


def test_the_agent_picks_the_pass_frequency():
    from burble.agent.service import Agent

    recorder, sink, clock, _ = recorder_hearing(CLIENTS)
    for frame in FRAMES:
        sink.on_voice(packet("W" * 22, frame, mhz=127.6), 10.0)
    settings = CalloutSettings.from_config({"callouts": {"enabled": True, "record_radio": True,
                                                         "carriers": {"CVN-75": {"frequency_mhz": 127.6}}}})
    callouts = SimpleNamespace(settings=settings, carrier_radio=None)
    session = SimpleNamespace(radio=recorder, callouts=callouts,
                              detector=SimpleNamespace(tracks={1: SimpleNamespace(pilot="CVN-75")}))
    live = SimpleNamespace(carrier_id=1, pilot="Wrycu", start_time=95.0, end_time=105.0)
    radio = Agent._pass_radio(None, session, live)
    assert radio and len(radio["transmissions"]) == 1
    session.radio = None
    assert Agent._pass_radio(None, session, live) is None  # not recording


# -- the hub -------------------------------------------------------------------------------------------------------

@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.add_source("server1")
    h.register_pilot("Boss", "boss password")
    h.set_admin("Boss", True)
    h.register_pilot("Goose", "goose password")
    return h


def report(with_radio=True):
    r = load_recording(PASS)
    (p,) = find_passes(r)
    meta = sidecar(r, p, "x", slice_objects(r, p))
    if with_radio:
        recorder, sink, clock, _ = recorder_hearing(CLIENTS)
        clock.now = p.start_time + 5.0
        for frame in FRAMES:
            sink.on_voice(packet("W" * 22, frame), 10.0)
        clock.now = p.start_time + 6.0
        sink.on_transmit(Radio(127.5)).extend(FRAMES[:5])
        meta["radio"] = radio_sidecar(recorder.for_pass(p.pilot, LSO_HZ, p.start_time - 10, p.end_time + 10))
    return PASS.read_bytes(), meta


def signed_in(hub, name, password) -> TestClient:
    client = TestClient(create_app(hub))
    assert client.post("/signin", data={"name": name, "password": password},
                       follow_redirects=False).status_code == 303
    return client


def test_stored_played_and_deleted(hub):
    data, meta = report()
    pass_id = hub.ingest(1, data, meta).pass_id
    hub.set_pilot_password("Wrycu", "wrycu password")
    with hub.sessions() as s:
        from burble.hub.db import Pass
        p = s.get(Pass, pass_id)
        assert "radio" not in p.slice.sidecar  # stored on its own
        path = hub.radio_store.path(p.radio_sha256)
    assert path.exists()
    visitor = TestClient(create_app(hub))
    page = visitor.get(f"/passes/{pass_id}").text
    assert "recorded on the carrier frequency: 2 transmissions" in page and "Delete recording" not in page
    replay = visitor.get(f"/passes/{pass_id}/replay.json").json()
    assert [(r["by"], r.get("name"), r["seconds"]) for r in replay["radio"]] == [("pilot", "Wrycu", 0.4),
                                                                               ("lso", None, 0.2)]
    assert replay["radio"][1]["t"] - replay["radio"][0]["t"] == pytest.approx(1.0)
    wav = visitor.get(replay["radio"][0]["audio"])
    assert wav.status_code == 200
    with wave.open(io.BytesIO(wav.content)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 0.4 * 16000
    assert visitor.get(f"/passes/{pass_id}/radio/9.wav").status_code == 404
    # Only the pass's pilot or an admin may delete it.
    assert visitor.post(f"/passes/{pass_id}/radio/delete").status_code == 403
    assert signed_in(hub, "Goose", "goose password").post(f"/passes/{pass_id}/radio/delete").status_code == 403
    wrycu = signed_in(hub, "Wrycu", "wrycu password")
    assert "Delete recording" in wrycu.get(f"/passes/{pass_id}").text
    r = wrycu.post(f"/passes/{pass_id}/radio/delete", follow_redirects=False)
    assert r.status_code == 303 and not path.exists() and not list(hub.radio_cache.glob("*.wav"))
    assert "Radio" not in visitor.get(f"/passes/{pass_id}").text
    assert visitor.get(f"/passes/{pass_id}/replay.json").json()["radio"] == []


def test_an_admin_deletes_it_and_deleting_the_pilot_does_too(hub):
    pass_id = hub.ingest(1, *report()).pass_id
    admin = signed_in(hub, "Boss", "boss password")
    assert "Delete recording" in admin.get(f"/passes/{pass_id}").text
    admin.post(f"/passes/{pass_id}/radio/delete")
    assert not list(hub.radio_store.root.rglob("*.json"))
    assert admin.get(f"/passes/{pass_id}/replay.json").status_code == 200  # the pass itself stays
    hub.delete_pilot("Wrycu")
    hub.ingest(1, *report())  # flown again, recorded again
    assert list(hub.radio_store.root.rglob("*.json"))
    hub.delete_pilot("Wrycu")
    assert not list(hub.radio_store.root.rglob("*.json"))


def test_without_radio_and_with_a_bad_one(hub):
    pass_id = hub.ingest(1, *report(with_radio=False)).pass_id
    assert TestClient(create_app(hub)).get(f"/passes/{pass_id}/replay.json").json()["radio"] == []
    data, meta = report()
    meta["radio"]["codec"] = "mp3"
    with pytest.raises(IngestError, match="radio"):
        hub.ingest(1, data, meta)


def test_members_only_covers_it(hub):
    pass_id = hub.ingest(1, *report()).pass_id
    hub.set_members_only(True)
    r = TestClient(create_app(hub)).get(f"/passes/{pass_id}/radio/0.wav", follow_redirects=False)
    assert r.status_code == 303
