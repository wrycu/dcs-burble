"""The live LSO view (PLAN #50): the server agent's feed, the hub's broker, and the pages (signed-in pilots only)."""

import asyncio
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from burble.acmi import AcmiParser, Frame, ObjectRemoved, ObjectUpdate
from burble.agent.callouts import MadeCall
from burble.agent.live import Listeners, LivePassDetector
from burble.agent.livefeed import LiveFeed, LiveSender, live_settings
from burble.callouts.rules import Call
from burble.hub.app import create_app
from burble.hub.live import KEEP_S, QUEUE_MAX, STALE_S, LiveBroker
from burble.hub.service import Hub

FIXTURES = Path(__file__).parent / "fixtures"
SERVER_TRAP = FIXTURES / "live" / "trap-server.zip.acmi"


class StubCallouts:
    """What the feed uses of LiveCallouts."""

    def __init__(self) -> None:
        self.on_made = None

    def state_for(self, carrier_id, aircraft_id):
        return None

    def deck_wind(self, carrier):
        return None


def feed_recording(path: Path, feed: LiveFeed, every_s: float = 0.25, at: tuple[float, object] | None = None) -> list[dict]:
    """Stream a recording through the detector and the feed as the agent does, taking a batch every `every_s`
    (and calling `at`'s function at its mission time)."""
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode("utf-8-sig")
    detector = LivePassDetector(feed)
    parser = AcmiParser()
    feed.globals = parser.globals
    batches, due = [], None
    for line in io.StringIO(text):
        for record in parser.feed(line.rstrip("\n")):
            if isinstance(record, Frame):
                if at is not None and record.time >= at[0]:
                    at[1]()
                    at = None
                due = record.time if due is None else due
                if record.time >= due:
                    if (batch := feed.batch()) is not None:
                        batches.append(batch)
                    due = record.time + every_s
            detector.feed(record)
            if isinstance(record, ObjectUpdate) and record.moved and record.id in detector.tracks:
                feed.update(detector.tracks[record.id])
            elif isinstance(record, ObjectRemoved):
                feed.removed(record.id)
    if (batch := feed.batch()) is not None:
        batches.append(batch)
    return batches


def planes(batches: list[dict]) -> list[dict]:
    return [a for b in batches for c in b["carriers"] for a in c["aircraft"]]


def test_the_feed_sends_the_jet_in_the_carriers_frame_with_its_groove():
    batches = feed_recording(SERVER_TRAP, LiveFeed())
    carriers = {(c["unit"], c["type"]) for b in batches for c in b["carriers"]}
    assert carriers == {("CVN-75 Harry S. Truman", "CVN_75")}
    jets = planes(batches)
    assert {(a["pilot"], a["type"]) for a in jets} == {("Wrycu", "FA-18C_hornet")}
    track = [row for a in jets for row in a["track"]]
    groove = [row for a in jets for row in a["groove"]]
    times = [row[0] for row in track]
    assert times == sorted(times) and len(set(times)) == len(times)  # each sample once, in order
    # The trap: it ends stopped on the deck (y about the jet's height over it), well forward of the ramp.
    t, x, y, z, *_ = track[-1]
    assert abs(x) < 40 and 0 < y < 4 and -150 < z < 150
    # The groove numbers run from about a mile out to past the aim point.
    assert groove[0][1] > 1500 and groove[-1][1] < 0
    assert all(abs(row[3]) < 30 for row in groove)  # glideslope deviation, meters
    assert batches[-1]["clock"].startswith("20")  # the mission's date and time, for the PLAT view
    # About a quarter second of samples in each batch: small.
    assert max(len(str(b)) for b in batches) < 3000


def test_the_feed_drops_aircraft_beyond_its_radius():
    batches = feed_recording(SERVER_TRAP, LiveFeed(radius_m=1000.0))
    track = [row for a in planes(batches) for row in a["track"]]
    assert track and all((row[1] ** 2 + row[3] ** 2) ** 0.5 < 1100 for row in track)
    gone = [g for b in batches for c in b["carriers"] for g in c["gone"]]
    assert gone == []  # it flew in, never out
    far = feed_recording(SERVER_TRAP, LiveFeed(radius_m=1.0))
    assert planes(far) == [] and far  # the carrier alone (so the hub lists it)


def test_the_feed_sends_calls_as_theyre_made():
    callouts = StubCallouts()
    feed = LiveFeed(callouts=callouts)
    jet = planes(feed_recording(SERVER_TRAP, LiveFeed()))[0]["id"]
    batches = feed_recording(SERVER_TRAP, feed, at=(710.0, lambda: callouts.on_made(MadeCall(jet, 710.0, 500.0, Call.POWER))))
    calls = [c for b in batches for carrier in b["carriers"] for c in carrier["calls"]]
    assert calls == [{"aircraft": jet, "time": 710.0, "along": 500.0, "call": "power"}]


def test_live_settings():
    assert live_settings({}) == (True, pytest.approx(10 * 1852))
    assert live_settings({"live": {"enabled": False, "radius_nm": 5}}) == (False, pytest.approx(5 * 1852))


def test_listeners_tell_each_in_turn():
    heard = []

    class L:
        def __init__(self, name):
            self.name = name

        def on_sample(self, *args):
            heard.append(self.name)

        def pass_ended(self, *args):
            heard.append(self.name + " ended")

    tee = Listeners(L("a"), L("b"))
    tee.on_sample(None, None, None, None, None)
    tee.pass_ended(1, 2)
    assert heard == ["a", "b", "a ended", "b ended"]


# -- the hub's broker --------------------------------------------------------------------------------------------

def batch(now: float, carrier_id: int = 7, unit: str = "CVN-75 Harry S. Truman", **extra) -> dict:
    return {"now": now, "carriers": [{"id": carrier_id, "unit": unit, "type": "CVN_75", "speed": 14.0,
                                      "aircraft": [{"id": 42, "type": "FA-18C_hornet", "pilot": "Goose",
                                                    "track": [[now, 1.0, 100.0, -2000.0, 0.0, 3.0, 0.0]],
                                                    "groove": []}],
                                      "calls": [], "gone": [], **extra}]}


def drain(queue: asyncio.Queue) -> list:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_a_viewer_gets_the_ship_and_the_recent_traffic_then_each_update():
    async def run():
        broker = LiveBroker()
        for i in range(10):
            broker.publish("server1", batch(100.0 + i))
        broker.publish("server1", batch(100.0 + KEEP_S + 5))  # the first few are now too old to keep
        channel = broker.get("server1", "CVN-75 Harry S. Truman")
        async with broker.watch(channel) as queue:
            first = drain(queue)
            assert first[0][0] == "carrier"
            opening = first[0][1]
            assert opening["carrier"]["type"] == "CVN_75" and "wires" in opening["carrier"]
            assert opening["types"]["FA-18C_hornet"]["model"] == "hornet"
            assert [e for e, _ in first[1:]] == ["update"] * len(first[1:])
            assert all(u["now"] >= 100.0 + 5 for _, u in first[1:])
            broker.publish("server1", batch(300.0, calls=[{"aircraft": 42, "time": 299.5, "along": 300.0,
                                                           "call": "power"}]))
            ((event, update),) = drain(queue)
            assert event == "update" and update["calls"] == [
                {"aircraft": 42, "t": 299.5, "text": "Power", "by": "lso", "say": "Power."}]
            assert broker.may_say("Power.") and not broker.may_say("Anything else.")
            broker.announce("server1", "CVN-75 Harry S. Truman", 42, {"pass_id": 9, "url": "/passes/9"})
            assert drain(queue) == [("graded", {"aircraft": 42, "pass_id": 9, "url": "/passes/9"})]
            # A new mission: the carrier is another object; viewers start over with it.
            broker.publish("server1", batch(5.0, carrier_id=8))
            events = drain(queue)
            assert [e for e, _ in events] == ["carrier", "update"]
            # Or the same object id with the clock back at the start (DCS can reuse ids).
            broker.publish("server1", batch(500.0, carrier_id=8))
            broker.publish("server1", batch(3.0, carrier_id=8))
            assert [e for e, _ in drain(queue)] == ["update", "carrier", "update"]
        assert not broker.get("server1", "CVN-75 Harry S. Truman").viewers

    asyncio.run(run())


def test_a_viewer_too_far_behind_is_let_go():
    async def run():
        broker = LiveBroker()
        broker.publish("server1", batch(0.0))
        channel = broker.get("server1", "CVN-75 Harry S. Truman")
        async with broker.watch(channel) as queue:
            for i in range(QUEUE_MAX + 5):
                broker.publish("server1", batch(float(i)))
            assert drain(queue) == [(None, None)] and not channel.viewers

    asyncio.run(run())


def test_carriers_not_heard_from_drop_off_the_list():
    now = [0.0]
    broker = LiveBroker(clock=lambda: now[0])
    broker.publish("server1", batch(1.0))
    broker.publish("server2", batch(1.0, unit="CVN-71 Theodore Roosevelt"))
    assert [c.unit for c in broker.live()] == ["CVN-75 Harry S. Truman", "CVN-71 Theodore Roosevelt"]
    now[0] = STALE_S - 1
    broker.publish("server2", batch(2.0, unit="CVN-71 Theodore Roosevelt"))
    now[0] = STALE_S + 1
    assert [c.unit for c in broker.live()] == ["CVN-71 Theodore Roosevelt"]
    assert broker.publish("server1", {"now": 1, "carriers": [{"unit": "Kuznetsov", "type": "KUZNECOW"}]}) == 0


# -- the pages and the API ---------------------------------------------------------------------------------------

@pytest.fixture
def hub(tmp_path):
    return Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")


def test_server_agents_send_and_signed_in_pilots_watch(hub):
    agent_token = hub.add_source("server1")
    hub.register_pilot("Goose", "goose password")
    pilot_token = hub.add_pilot_token("Goose")
    app = create_app(hub)
    client = TestClient(app)
    update = batch(10.0)
    assert client.post("/api/v1/live", json=update).status_code == 401
    assert client.post("/api/v1/live", json=update,
                       headers={"Authorization": f"Bearer {pilot_token}"}).status_code == 403
    r = client.post("/api/v1/live", json=update, headers={"Authorization": f"Bearer {agent_token}"})
    assert r.status_code == 200 and r.json() == {"carriers": 1}
    assert client.post("/api/v1/live", content=b"[1]", headers={"Authorization": f"Bearer {agent_token}"}).status_code == 400

    # Not signed in: sent to sign in, and no events.
    r = client.get("/live", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin?next=/live"
    r = client.get("/live/server1/CVN-75%20Harry%20S.%20Truman", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin?next=/live/server1/")
    assert client.get("/live/server1/CVN-75%20Harry%20S.%20Truman/events").status_code == 401
    assert client.get("/live/say.wav", params={"text": "Power."}).status_code == 401

    client.post("/signin", data={"name": "Goose", "password": "goose password"})
    index = client.get("/live").text
    assert 'href="/live/server1/CVN-75%20Harry%20S.%20Truman"' in index and "Goose" in index
    page = client.get("/live/server1/CVN-75%20Harry%20S.%20Truman").text
    assert 'data-src="/live/server1/CVN-75%20Harry%20S.%20Truman/events"' in page and "/static/live.js" in page
    assert '<a href="/live">Live</a>' in page  # in the bar, signed in
    assert client.get("/live/say.wav", params={"text": "Power."}).status_code == 404  # no voice on this hub
    assert client.get("/static/live.js").status_code == 200 and client.get("/static/lso3d.js").status_code == 200


def test_the_agents_sender_posts_the_feeds_batches(hub):
    token = hub.add_source("server1")
    app = create_app(hub)

    async def run():
        feed = LiveFeed()
        batches = feed_recording(SERVER_TRAP, feed)
        feed.batch = iter(batches[100:110] + [None]).__next__  # replay some of them
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub",
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            sender = LiveSender(feed, client, clock=lambda: 0.0)
            sent = [await sender.send_once() for _ in range(11)]
        return sent

    assert asyncio.run(run()) == [True] * 10 + [False]
    (channel,) = app.state.live.live()
    assert channel.unit == "CVN-75 Harry S. Truman" and channel.pilots and len(channel.updates) == 10


def test_the_sender_logs_each_new_reason_it_cant_send(caplog):
    answers = iter([httpx.ConnectError("All connection attempts failed"), httpx.ConnectError("All connection attempts failed"),
                    httpx.Response(401, text="invalid token"), httpx.Response(401, text="invalid token"),
                    httpx.Response(200, json={"carriers": 1})])

    def answer(request):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    async def run():
        feed = LiveFeed()
        feed.batch = lambda: {"now": 1.0, "carriers": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://hub") as client:
            sender = LiveSender(feed, client, clock=lambda: 0.0)
            return [await sender.send_once() for _ in range(5)]

    with caplog.at_level("INFO", logger="burble.agent.livefeed"):
        assert asyncio.run(run()) == [False, False, False, False, True]
    assert [r.getMessage() for r in caplog.records] == [
        "live view: hub not taking updates (All connection attempts failed); dropping updates meanwhile",
        "live view: hub refused an update (401 invalid token); dropping updates meanwhile",
        "live view: hub taking updates again"]


def test_each_server_agents_status():
    now = [100.0]
    broker = LiveBroker(clock=lambda: now[0])
    broker.publish("server1", batch(10.0))  # a carrier: live
    broker.publish("server2", {"now": 50.0, "carriers": []})  # a heartbeat: mission running, no carrier
    broker.publish("server3", {"now": 70.0, "carriers": []})
    now[0] = 110.0
    broker.publish("server2", {"now": 60.0, "carriers": []})
    broker.publish("server3", {"now": 70.0, "carriers": []})  # its clock isn't moving
    now[0] = 120.0
    statuses = broker.status(["server4", "server3", "server2", "server1"])
    assert [(a.agent, a.status) for a in statuses] == [
        ("server1", "live"), ("server2", "running"), ("server3", "paused"), ("server4", "silent")]
    assert [c.unit for c in statuses[0].carriers] == ["CVN-75 Harry S. Truman"]
    assert statuses[3].heard is None and statuses[2].heard is not None
    now[0] = 110.0 + STALE_S + 1  # nothing from anyone since
    assert {a.status for a in broker.status(["server1", "server2", "server3"])} == {"silent"}


def test_the_sender_sends_a_heartbeat_when_theres_nothing_else():
    posted = []

    def answer(request):
        posted.append(request.read())
        return httpx.Response(200, json={"carriers": 0})

    async def run():
        feed = LiveFeed()
        feed.now = 42.0
        clock = [0.0]
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://hub") as client:
            sender = LiveSender(feed, client, clock=lambda: clock[0])
            sent = [await sender.send_once()]  # nothing yet: a heartbeat
            clock[0] = 4.0
            sent.append(await sender.send_once())  # too soon for another
            clock[0] = 5.0
            sent.append(await sender.send_once())
            return sent

    assert asyncio.run(run()) == [True, False, True]
    assert posted == [b'{"now":42.0,"carriers":[]}'] * 2


def test_live_lists_every_server_agent(hub):
    token = hub.add_source("server1")
    hub.add_source("server2")
    hub.register_pilot("Goose", "goose password")
    client = TestClient(create_app(hub))
    client.post("/api/v1/live", json=batch(10.0), headers={"Authorization": f"Bearer {token}"})
    assert client.get("/live/server1", follow_redirects=False).status_code == 303  # signing in first
    client.post("/signin", data={"name": "Goose", "password": "goose password"})
    index = client.get("/live").text
    assert index.index('href="/live/server1"') < index.index('href="/live/server2"')
    assert "No live data since the hub started" in index
    # A server with one carrier live goes straight to it; one with none shows its status.
    r = client.get("/live/server1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/live/server1/CVN-75%20Harry%20S.%20Truman"
    page = client.get("/live/server2").text
    assert "<h1>server2</h1>" in page and "No live data" in page
    assert client.get("/live/nobody").status_code == 404
    assert 'Live from <a href="/live/server1">server1</a>' in client.get(r.headers["location"]).text
