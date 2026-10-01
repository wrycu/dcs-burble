import asyncio
import json
from pathlib import Path

import httpx
import pytest

from dcs_lso.acmi import AcmiParser, iter_lines, load_recording
from dcs_lso.acmi.stream import serve_recording
from dcs_lso.central.app import create_app
from dcs_lso.central.service import Central
from dcs_lso.dcslog import Debrief, HookEvent
from dcs_lso.detect import find_passes
from dcs_lso.edge.collector import Collector, CollectorConfig, to_dcs_event
from dcs_lso.edge.live import LivePassDetector
from dcs_lso.slices import TAIL_S

FIXTURES = Path(__file__).parent / "fixtures"
AI_TRAP = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
PASS_FILES = sorted((FIXTURES / "passes").glob("*.zip.acmi"))


@pytest.mark.parametrize("path", [AI_TRAP, *PASS_FILES], ids=lambda p: p.stem)
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

    def debrief(self) -> Debrief:
        event = HookEvent("landing_quality_mark", 294.655, "LSO: GRADE:C : LNFIW  WIRE# 3",
                          {"name": "Aerial-1-1", "type": "FA-18C_hornet", "object_id": 16777728},
                          {"name": "Naval-1-1"}, None, {})
        return Debrief(None, [to_dcs_event(event)])


@pytest.fixture
def central(tmp_path):
    return Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")


def make_client(central) -> httpx.AsyncClient:
    token = central.add_source("edge")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(central)), base_url="http://central",
                             headers={"Authorization": f"Bearer {token}"})


async def collect(source: Path, work: Path, port_holder: list, client=None, hooks=None) -> Collector:
    server = await serve_recording(source, port=0, speed=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        collector = Collector(CollectorConfig(work_dir=work, tacview_port=port), client=client)
        collector.hooks = hooks
        port_holder.append(await collector.run_session())
    return collector


def passes_on(central) -> list[dict]:
    with central.sessions() as s:
        from dcs_lso.central.db import Pass
        return [{"outcome": p.outcome, "wire": p.wire, "dcs": p.dcs_grade, "grade": p.grade.grade}
                for p in s.query(Pass).all()]


def test_stream_to_central_with_hook_grade(tmp_path, central):
    async def run():
        sessions: list = []
        async with make_client(central) as client:
            collector = await collect(AI_TRAP, tmp_path / "edge", sessions, client, StubHooks())
            assert await collector.upload_once() == (1, 0)
        return collector, sessions[0]

    collector, session = asyncio.run(run())
    assert passes_on(central) == [{"outcome": "trap", "wire": 3, "dcs": "LSO: GRADE:C : LNFIW  WIRE# 3", "grade": "C"}]
    archives = list((tmp_path / "edge" / "archive").glob("*.zip.acmi"))
    assert len(archives) == 1 and not list((tmp_path / "edge" / "archive").glob("*.txt.acmi"))
    (sent,) = collector.outbox.sent()
    meta = sent.meta()
    assert meta["recording"]["first_frame_time"] == pytest.approx(0.04)
    # The archived session is a complete recording: it still yields the same pass.
    assert [p.outcome.value for p in find_passes(load_recording(archives[0]))] == ["trap"]


def test_outbox_survives_central_outage(tmp_path, central):
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)),
                             base_url="http://central")

    async def run():
        collector = await collect(AI_TRAP, tmp_path / "edge", [], down)
        assert await collector.upload_once() == (0, 1)
        assert len(collector.outbox.pending()) == 1
        async with make_client(central) as up:
            collector.client = up
            assert await collector.upload_once() == (1, 0)
        return collector

    collector = asyncio.run(run())
    assert len(collector.outbox.sent()) == 1 and len(passes_on(central)) == 1


def test_debrief_fills_in_missing_dcs_grade(tmp_path, central):
    async def run():
        sessions: list = []
        async with make_client(central) as client:
            collector = await collect(AI_TRAP, tmp_path / "edge", sessions, client)  # no hook events
            await collector.upload_once()
            assert passes_on(central)[0]["dcs"] is None
            updated = collector.apply_debrief(sessions[0].names, FIXTURES / "ai_hornet_trap_cvn75.debrief.log")
            assert updated == 1
            assert await collector.upload_once() == (1, 0)

    asyncio.run(run())
    assert passes_on(central) == [{"outcome": "trap", "wire": 3, "dcs": "LSO: GRADE:C : LNFIW  WIRE# 3", "grade": "C"}]


def test_rejected_upload_is_set_aside(tmp_path):
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"detail": "x"})),
                            base_url="http://central")

    async def run():
        collector = await collect(AI_TRAP, tmp_path / "edge", [], bad)
        assert await collector.upload_once() == (0, 0)
        return collector

    collector = asyncio.run(run())
    assert collector.outbox.pending() == [] and len(list(collector.outbox.rejected_dir.glob("*.json"))) == 1


def test_warns_when_connection_has_no_frames(tmp_path, monkeypatch, caplog):
    """A Tacview host that sends the header and then nothing (Export.lua missing Tacview)."""
    import logging

    import dcs_lso.edge.collector as collector_mod

    monkeypatch.setattr(collector_mod, "NO_FRAMES_WARNING_S", 0.2)
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
            await Collector(CollectorConfig(work_dir=tmp_path / "edge", tacview_port=port)).run_session()

    with caplog.at_level(logging.WARNING, logger="dcs_lso.edge.collector"):
        asyncio.run(run())
    assert any("no new frames" in r.message and "Export.lua" in r.message for r in caplog.records)
