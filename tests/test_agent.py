import asyncio
import json
from pathlib import Path

import httpx
import pytest

from burble.acmi import AcmiParser, iter_lines, load_recording
from burble.acmi.stream import serve_recording
from burble.hub.app import create_app
from burble.hub.service import Hub
from burble.dcslog import Debrief, HookEvent
from burble.detect import find_passes
from burble.agent.service import Agent, AgentConfig, to_dcs_event, wind_profile
from burble.agent.live import LivePassDetector
from burble.slices import TAIL_S

FIXTURES = Path(__file__).parent / "fixtures"
AI_TRAP = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
PASS_FILES = sorted((FIXTURES / "passes").glob("*.zip.acmi"))


@pytest.mark.parametrize("path", [AI_TRAP, *PASS_FILES, FIXTURES / "live" / "crash-server.zip.acmi"], ids=lambda p: p.stem)
def test_live_detection_matches_offline(path):
    parser, live = AcmiParser(), LivePassDetector()
    found = []
    for line in iter_lines(path):
        for record in parser.feed(line):
            found += live.feed(record)
    found += live.flush()
    offline = list(find_passes(load_recording(path)))
    assert [(p.aircraft_id, p.outcome) for p in found] == [(p.aircraft_id, p.outcome) for p in offline]
    for a, b in zip(found, offline):
        # The slice window (live start - LEAD_S .. live end + TAIL_S) must contain the whole
        # offline pass, and not run on much longer.
        assert abs(a.start_time - b.start_time) < 1.0
        assert a.end_time + TAIL_S >= b.end_time and a.end_time - b.end_time < 15.0


class StubHooks:
    """Stands in for HookFeed: DCS's grade for the AI trap, as the hook reports it."""

    def wire_for(self, tacview_id, start, end):
        return None

    def slot_for(self, pilot, before):
        # As the hook logs a player's slot: from the mission, the slot's livery and side number.
        return {"livery": "VFA-37", "onboard_num": "300", "unit": "Aerial-1-1"} if pilot == "Aerial-1-1" else None

    def wind_for(self, carrier_unit, at=None):
        # As logged by the hook (Lua arrays arrive keyed "1", "2", ...).
        return wind_profile({"carrier": carrier_unit, "levels": {"2": {"alt": 100, "east": 1.0, "north": -6.0},
                                                                  "1": {"alt": 10, "east": 0.5, "north": -4.0}}})

    def debrief(self) -> Debrief:
        event = HookEvent("landing_quality_mark", 294.655, "LSO: GRADE:C : LNFIW  WIRE# 3",
                          {"name": "Aerial-1-1", "type": "FA-18C_hornet", "object_id": 16777728},
                          {"name": "Naval-1-1"}, None, {})
        return Debrief(None, [to_dcs_event(event)])


@pytest.fixture
def hub(tmp_path):
    return Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")


def make_client(hub) -> httpx.AsyncClient:
    token = hub.add_source("edge")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(hub)), base_url="http://hub",
                             headers={"Authorization": f"Bearer {token}"})


async def collect(source: Path, work: Path, port_holder: list, client=None, hooks=None) -> Agent:
    server = await serve_recording(source, port=0, speed=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        agent = Agent(AgentConfig(work_dir=work, tacview_port=port), client=client)
        agent.hooks = hooks
        port_holder.append(await agent.run_session())
    return agent


def passes_on(hub) -> list[dict]:
    with hub.sessions() as s:
        from burble.hub.db import Pass
        return [{"outcome": p.outcome, "wire": p.wire, "dcs": p.dcs_grade, "grade": p.grade.grade}
                for p in s.query(Pass).all()]


def test_stream_to_central_with_hook_grade(tmp_path, hub):
    async def run():
        sessions: list = []
        async with make_client(hub) as client:
            agent = await collect(AI_TRAP, tmp_path / "edge", sessions, client, StubHooks())
            assert await agent.upload_once() == (1, 0)
        return agent, sessions[0]

    agent, session = asyncio.run(run())
    assert passes_on(hub) == [{"outcome": "trap", "wire": 3, "dcs": "LSO: GRADE:C : LNFIW  WIRE# 3", "grade": "C"}]
    archives = list((tmp_path / "edge" / "archive").glob("*.zip.acmi"))
    assert len(archives) == 1 and not list((tmp_path / "edge" / "archive").glob("*.txt.acmi"))
    (sent,) = agent.outbox.sent()
    meta = sent.meta()
    assert meta["recording"]["first_frame_time"] == pytest.approx(0.04)
    assert meta["aircraft"] == {"livery": "VFA-37", "onboard_num": "300", "unit": "Aerial-1-1"}
    assert meta["wind"] == {"levels": [{"alt": 10.0, "east": 0.5, "north": -4.0}, {"alt": 100.0, "east": 1.0, "north": -6.0}]}
    # The archived session is a complete recording: it still yields the same pass.
    assert [p.outcome.value for p in find_passes(load_recording(archives[0]))] == ["trap"]


def test_outbox_survives_central_outage(tmp_path, hub):
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)),
                             base_url="http://hub")

    async def run():
        agent = await collect(AI_TRAP, tmp_path / "edge", [], down)
        assert await agent.upload_once() == (0, 1)
        assert len(agent.outbox.pending()) == 1
        async with make_client(hub) as up:
            agent.client = up
            assert await agent.upload_once() == (1, 0)
        return agent

    agent = asyncio.run(run())
    assert len(agent.outbox.sent()) == 1 and len(passes_on(hub)) == 1


def test_debrief_fills_in_missing_dcs_grade(tmp_path, hub):
    async def run():
        sessions: list = []
        async with make_client(hub) as client:
            agent = await collect(AI_TRAP, tmp_path / "edge", sessions, client)  # no hook events
            await agent.upload_once()
            assert passes_on(hub)[0]["dcs"] is None
            updated = agent.apply_debrief(sessions[0].names, FIXTURES / "ai_hornet_trap_cvn75.debrief.log")
            assert updated == 1
            assert await agent.upload_once() == (1, 0)

    asyncio.run(run())
    assert passes_on(hub) == [{"outcome": "trap", "wire": 3, "dcs": "LSO: GRADE:C : LNFIW  WIRE# 3", "grade": "C"}]


def test_rejected_upload_is_set_aside(tmp_path):
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"detail": "x"})),
                            base_url="http://hub")

    async def run():
        agent = await collect(AI_TRAP, tmp_path / "edge", [], bad)
        assert await agent.upload_once() == (0, 0)
        return agent

    agent = asyncio.run(run())
    assert agent.outbox.pending() == [] and len(list(agent.outbox.rejected_dir.glob("*.json"))) == 1


def test_warns_when_connection_has_no_frames(tmp_path, monkeypatch, caplog):
    """A Tacview host that sends the header and then nothing (Export.lua missing Tacview)."""
    import logging

    import burble.agent.service as collector_mod

    monkeypatch.setattr(collector_mod, "NO_FRAMES_WARNING_S", 0.2)
    monkeypatch.setattr(collector_mod, "WATCH_INTERVAL_S", 0.05)
    header_only = tmp_path / "header.txt.acmi"
    header_only.write_text("FileType=text/acmi/tacview\nFileVersion=2.2\n0,ReferenceTime=2026-01-01T00:00:00Z\n")

    async def run():
        async def handle(reader, writer):
            writer.write(b"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\nhost\n\0")
            await reader.readuntil(b"\0")
            writer.write(header_only.read_bytes())
            await writer.drain()
            await asyncio.sleep(0.7)  # connected, but no frames
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            await Agent(AgentConfig(work_dir=tmp_path / "edge", tacview_port=port)).run_session()

    with caplog.at_level(logging.WARNING, logger="burble.agent.service"):
        asyncio.run(run())
    assert any("no new frames" in r.message and "Export.lua" in r.message for r in caplog.records)


def test_pass_is_sliced_when_the_server_pauses_after_it(tmp_path, monkeypatch):
    """The pilot leaves right after trapping, DCS pauses the empty server, and frames stop while the
    connection stays open: the waiting pass is still sliced, from what was recorded."""
    import burble.agent.service as collector_mod

    monkeypatch.setattr(collector_mod, "STALLED_SLICE_S", 0.3)
    monkeypatch.setattr(collector_mod, "WATCH_INTERVAL_S", 0.05)
    (trap,) = find_passes(load_recording(AI_TRAP))
    lines, parser = [], AcmiParser()
    for line in iter_lines(AI_TRAP):
        if any(getattr(r, "time", 0.0) > trap.end_time + 2.0 for r in parser.feed(line)) and line.startswith("#"):
            break  # the server pauses 2 s after the pass ended: well before the 10 s slicing delay
        lines.append(line if line.endswith("\n") else line + "\n")
    queued_while_paused = []

    async def run():
        agent = Agent(AgentConfig(work_dir=tmp_path / "edge", tacview_port=0))

        async def handle(reader, writer):
            writer.write(b"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\nhost\n\0")
            await reader.readuntil(b"\0")
            writer.write("".join(lines).encode())
            await writer.drain()
            await asyncio.sleep(1.0)  # paused: connected, no frames
            queued_while_paused.extend(i.name for i in agent.outbox.pending())
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        agent.config.tacview_port = server.sockets[0].getsockname()[1]
        async with server:
            await agent.run_session()

    from datetime import UTC, datetime
    started = datetime.now(UTC)
    asyncio.run(run())
    assert len(queued_while_paused) == 1
    # Stamped with the wall clock (mission time says nothing about how long the server was paused).
    (item,) = Agent(AgentConfig(work_dir=tmp_path / "edge")).outbox.pending()
    stamped = datetime.fromisoformat(item.meta()["pass"]["occurred_at"])
    assert abs((stamped - started).total_seconds()) < 120


def test_wire_from_carrier_animation_without_dcs_grade(tmp_path):
    """No DCS comms: the hook's wire-animation samples still give the wire."""
    from burble.agent.service import HookFeed

    feed = HookFeed.__new__(HookFeed)
    import threading
    feed._events, feed._lock = [], threading.Lock()
    me = {"object_id": 16777728, "type": "FA-18C_hornet"}  # -> Tacview 0x201, the AI trap's aircraft
    other = {"object_id": 16777984, "type": "FA-18C_hornet"}
    for t, wires, who in [(292.4, {"w1": 0, "w2": 0, "w3": 1, "w4": 0}, me),
                          (292.9, {"w1": 0, "w2": 0, "w3": 1, "w4": 0}, me),
                          (292.9, {"w1": 0, "w2": 1, "w3": 0, "w4": 0}, other),   # someone else's trap
                          (900.0, {"w1": 1, "w2": 0, "w3": 0, "w4": 0}, me)]:      # outside this pass
        feed.add(HookEvent("wire_sample", t, None, who, None, None, {"initiator": who, "wires": wires}))
    assert feed.wire_for(0x201, 264.0, 306.0) == 3
    assert feed.wire_for(0x201, 0.0, 100.0) is None

    async def run():
        server = await serve_recording(AI_TRAP, port=0, speed=0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            agent = Agent(AgentConfig(work_dir=tmp_path / "edge", tacview_port=port))
            agent.hooks = feed
            await agent.run_session()
        return agent

    agent = asyncio.run(run())
    (item,) = agent.outbox.pending()
    meta = item.meta()
    assert meta["dcs"]["wire"] == 3 and meta["dcs"]["grade"] is None and meta["wire_source"] == "carrier-animation"


def test_hook_events_are_scoped_to_the_current_mission():
    """Mission time restarts at 0, so a previous mission's grade/wire must not match a new pass."""
    import threading

    from burble.agent.service import HookFeed

    feed = HookFeed.__new__(HookFeed)
    feed._events, feed._lock = [], threading.Lock()
    me = {"object_id": 16777728, "type": "FA-18C_hornet"}
    old_mission = [HookEvent("handler_installed", 0.0, None, None, None, None, {}),
                   HookEvent("wire_sample", 300.0, None, me, None, None, {"initiator": me, "wires": {"w3": 1}}),
                   HookEvent("landing_quality_mark", 300.2, "LSO: GRADE:C : WIRE# 3", me, {"name": "CVN"}, None, {})]
    for e in old_mission:
        feed.add(e)
    assert feed.wire_for(0x201, 290.0, 310.0) == 3
    feed.add(HookEvent("handler_installed", 0.0, None, None, None, None, {}))  # next mission loads
    assert feed.wire_for(0x201, 290.0, 310.0) is None
    assert feed.debrief().landing_marks() == []


def test_retention(tmp_path):
    import os
    import time
    agent = Agent(AgentConfig(work_dir=tmp_path, keep_archives_days=30, keep_sent_days=14,
                                          keep_rejected_days=0))
    now = time.time()

    def aged(path, days):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        os.utime(path, (now - days * 86400, now - days * 86400))
        return path

    archives = agent.archive_dir
    old_archive, new_archive = aged(archives / "a-session.zip.acmi", 40), aged(archives / "b-session.zip.acmi", 5)
    open_session = aged(archives / "c-session.txt.acmi", 90)  # a session still being written
    box = agent.outbox
    for directory, name, days in ((box.sent_dir, "old", 20), (box.sent_dir, "new", 2), (box.rejected_dir, "bad", 400),
                                  (box.pending_dir, "waiting", 400)):
        aged(directory / f"{name}.zip.acmi", days)
        aged(directory / f"{name}.json", days)
    assert agent.prune(now) == {"archives": 1, "sent": 1, "rejected": 0}  # rejected: kept forever (0)
    assert not old_archive.exists() and new_archive.exists() and open_session.exists()
    assert [i.name for i in box.sent()] == ["new"] and [i.name for i in box.pending()] == ["waiting"]
    assert (box.rejected_dir / "bad.json").exists()
    # 0 everywhere: nothing is ever removed.
    keep = Agent(AgentConfig(work_dir=tmp_path, keep_archives_days=0, keep_sent_days=0, keep_rejected_days=0))
    assert keep.prune(now + 10 * 365 * 86400) == {"archives": 0, "sent": 0, "rejected": 0}



def test_retention_settings_from_central(tmp_path):
    agent = Agent(AgentConfig(work_dir=tmp_path))
    assert agent.retention() == {"archives": 90.0, "sent": 14.0, "rejected": 30.0}  # the defaults
    agent.remote_config = {"retention": {"archives_days": 365, "sent_days": 0}}
    assert agent.retention() == {"archives": 365.0, "sent": 0.0, "rejected": 30.0}
    local = Agent(AgentConfig(work_dir=tmp_path, keep_archives_days=7))  # set on the agent: wins
    local.remote_config = {"retention": {"archives_days": 365}}
    assert local.retention()["archives"] == 7.0
    agent.remote_config = {"retention": {"archives_days": "lots"}}
    assert agent.retention()["archives"] == 90.0  # a bad value falls back to the default


def test_server_agent_reports_connected_players(tmp_path, hub):
    from burble.agent.service import HookFeed
    from burble.dcslog import parse_hook_line

    log_file = tmp_path / "dcs.log"
    log_file.write_text("")
    feed = HookFeed(log_file)
    agent = Agent(AgentConfig(work_dir=tmp_path / "agent"), client=make_client(hub))
    agent.hooks = feed
    line = ('2026-10-03 20:33:05.817 INFO    BURBLE (Main): BURBLE {"event":"players","t":42.5,"players":'
            '[{"id":2,"ucid":"fa26","ip":"69.222.184.25","name":"Wrycu"}]}')

    async def run():
        assert not await agent.report_players(now=0.0)  # no list from the hook yet
        feed.add(parse_hook_line(line))
        assert await agent.report_players(now=0.0)
        assert not await agent.report_players(now=10.0)  # unchanged: not again yet
        assert await agent.report_players(now=130.0)  # but every 2 minutes anyway
        feed.add(parse_hook_line(line.replace('[{"id"', '[{"id":3,"ucid":"b0b0","ip":"10.0.0.5",'
                                                                                 '"name":"Goose"},{"id"')))
        assert await agent.report_players(now=131.0)  # changed

    asyncio.run(run())
    assert hub.pilot_hook_here("fa26", "69.222.184.25") and hub.pilot_hook_here("b0b0", "10.0.0.9")
    assert not hub.pilot_hook_here("fa26", "8.8.4.4")


def test_hook_feed_knows_who_is_a_player(tmp_path):
    from burble.agent.service import HookFeed
    from burble.dcslog import parse_hook_line

    log_file = tmp_path / "dcs.log"
    log_file.write_text("")
    feed = HookFeed(log_file)
    assert feed.player_names() is None  # nothing from the hook yet: can't tell AI from players
    prefix = "2026-10-05 10:00:00.000 INFO    BURBLE (Main): BURBLE "
    feed.add(parse_hook_line(prefix + '{"event":"slot","t":10,"player":"Host Pilot","unit":"Hornet 1"}'))  # a listen server's host
    feed.add(parse_hook_line(prefix + '{"event":"players","t":11,"players":[{"ucid":"a","ip":"1.2.3.4","name":"Wrycu"}]}'))
    assert feed.player_names() == {"Host Pilot", "Wrycu"}
    feed.add(parse_hook_line(prefix + '{"event":"handler_installed","t":0}'))  # a new mission
    assert feed.player_names() is None


def test_a_jet_sitting_on_the_deck_departed_from_that_carrier():
    from burble.acmi import AcmiParser
    from burble.agent.live import LivePassDetector
    detector = LivePassDetector()
    parser = AcmiParser()
    lines = ["FileType=text/acmi/tacview", "FileVersion=2.2", "0,ReferenceTime=2016-06-21T05:00:00Z"]
    for i in range(30):  # the carrier steams north at 10 m/s; the jet sits on its deck, then takes off
        t = i * 1.0
        lines.append(f"#{t}")
        lines.append(f"1,T=35|35|0|0|0|0|0|{10 * t}|0,Type=Sea+Watercraft+AircraftCarrier,Name=CVN_75")
        jet_alt = 20.2 if t < 10 else 20.2 + (t - 10) * 15
        jet_v = 10 * t - 60 if t < 10 else 10 * t - 60 + (t - 10) ** 2 * 5
        lines.append(f"2,T=35|35|{jet_alt}|0|0|0|5|{jet_v}|0,Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=Wrycu")
    for line in lines:
        for record in parser.feed(line):
            detector.feed(record)
    assert detector.departed_from(1, 2, before=600.0)  # a trap here later: "welcome home"
    assert not detector.departed_from(1, 2, before=60.0)  # not within 2 minutes of being on deck (the trap itself)
    assert not detector.departed_from(1, 3, before=600.0)  # another jet: never on this deck


@pytest.mark.parametrize("name", ["crash-server", "crash-pilot-hook"])
def test_a_crash_on_deck_is_not_a_bolter(name):
    from burble.detect.passes import Outcome
    from burble.grading import grade_pass
    # Wrycu dove into the deck at about 100 m/s; the jet's track ends there (the server's copy: removed).
    (result,) = find_passes(load_recording(FIXTURES / "live" / f"{name}.zip.acmi"))
    assert result.outcome is Outcome.CRASH and grade_pass(result).grade.value == "C"


def test_a_bolter_cut_off_by_the_end_of_the_stream_is_not_a_crash():
    from burble.detect.passes import Outcome
    parser, live = AcmiParser(), LivePassDetector()
    found = []
    for line in iter_lines(FIXTURES / "live" / "crash-server.zip.acmi"):
        if line.startswith("-"):
            break  # the stream ends before the jet is removed
        for record in parser.feed(line):
            found += live.feed(record)
    assert [p.outcome for p in found + live.flush()] != [Outcome.CRASH]


def test_the_hub_records_a_crash_and_regrading_corrects_old_bolters(tmp_path):
    from burble.hub.db import Pass
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    hub.add_source("server1")
    hub.ingest_recording(1, FIXTURES / "live" / "crash-server.zip.acmi")
    with hub.sessions.begin() as s:
        (p,) = s.query(Pass).all()
        assert (p.outcome, p.grade.grade) == ("crash", "C")
        p.outcome = "bolter"  # as stored before crashes were told apart
    hub.regrade(force=True)
    with hub.sessions() as s:
        assert s.query(Pass).one().outcome == "crash"
