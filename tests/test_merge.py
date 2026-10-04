"""Two reports of one landing: the server's (carrier, 4.8 Hz, no AOA) and the pilot's own Tacview
(own jet only, ~8 Hz, real AOA). Hub combines them into one landing graded from the pilot's
track against the server's carrier, keeping the server's DCS grade, wire and live calls."""

import asyncio
from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.acmi.stream import serve_recording
from dcs_lso.hub.service import Hub
from dcs_lso.detect import find_passes
from dcs_lso.detect.approaches import find_approaches
from dcs_lso.agent.service import Agent, AgentConfig
from dcs_lso.slices import sidecar, slice_objects, track_sidecar

PAIRS = Path(__file__).parent / "fixtures" / "server_vs_client"
CALLS = [{"time": 1100.0, "along": 700.0, "call": "you're a little high"}]


def server_report(kind: str) -> tuple[bytes, dict]:
    path = PAIRS / f"server-{kind}.zip.acmi"
    recording = load_recording(path)
    (p,) = find_passes(recording)
    meta = sidecar(recording, p, "session.txt.acmi", slice_objects(recording, p))
    if kind == "trap":
        meta["dcs"] = {"wire": 3, "grade": {"raw": "LSO: GRADE:OK : WIRE# 3"}}
        meta["calls"] = CALLS
    return path.read_bytes(), meta


def pilot_report(kind: str) -> tuple[bytes, dict]:
    path = PAIRS / f"client-{kind}.zip.acmi"
    recording = load_recording(path)
    (jet,) = [o.id for o in recording.objects.values() if o.name == "FA-18C_hornet"]
    (approach,) = find_approaches(recording, jet)
    return path.read_bytes(), track_sidecar(recording, approach, "pilot-session.txt.acmi")


@pytest.fixture
def hub(tmp_path):
    c = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    c.server = c.add_source("server1")  # tokens unused here; ingest takes the source id
    c.pilot = c.add_source("wrycu-pc", kind="pilot")
    return c


def landings(hub: Hub):
    from fastapi.testclient import TestClient

    from dcs_lso.hub.app import create_app
    return TestClient(create_app(hub)).get("/api/v1/passes", params={"days": 0}).json()


@pytest.mark.parametrize("pilot_first", [False, True])
def test_reports_of_one_landing_are_combined(hub, pilot_first):
    server, pilot = (1, server_report("trap")), (2, pilot_report("trap"))
    first, second = (pilot, server) if pilot_first else (server, pilot)
    r1 = hub.ingest(first[0], *first[1])
    if pilot_first:
        assert r1.grade == "" and "waiting" in r1.text  # no carrier yet: nothing to grade
    r2 = hub.ingest(second[0], *second[1])
    assert r2.grade

    (landing,) = landings(hub)
    assert sorted(r["source"] for r in landing["reports"]) == ["server1", "wrycu-pc"]
    # The server's knowledge is kept...
    assert landing["wire"] == 3 and landing["dcs_grade"] == "LSO: GRADE:OK : WIRE# 3" and landing["calls"] == CALLS
    # ...and the pilot's track (recorded AOA, higher rate) is what's graded.
    with hub.sessions() as s:
        from dcs_lso.hub.db import Pass
        row = s.get(Pass, landing["id"])
        result = hub.load_pass(row)
    assert result.track_source == "wrycu-pc"
    assert result.outcome.value == "trap"
    groove = [x for x in result.samples if 150 < x.along < 1389]
    assert groove and not any(x.aoa_derived for x in groove)
    (server_pass,) = find_passes(load_recording(PAIRS / "server-trap.zip.acmi"))
    assert len(groove) > 1.4 * len([x for x in server_pass.samples if 150 < x.along < 1389])


def test_a_different_landing_is_not_merged(hub):
    hub.ingest(1, *server_report("bolter"))
    waiting = hub.ingest(2, *pilot_report("trap"))  # the pilot's trap, but the server only has the bolter
    assert waiting.grade == ""
    (landing,) = landings(hub)
    assert landing["outcome"] == "bolter" and [r["source"] for r in landing["reports"]] == ["server1"]


def test_the_board_lists_a_landing_under_every_source_that_reported_it(hub):
    from fastapi.testclient import TestClient

    from dcs_lso.hub.app import create_app
    hub.ingest(1, *server_report("trap"))
    hub.ingest(2, *pilot_report("trap"))
    client = TestClient(create_app(hub))
    for source in ("server1", "wrycu-pc"):
        (row,) = client.get("/api/v1/passes", params={"days": 0, "source": source}).json()
    page = client.get(f"/passes/{row['id']}").text
    assert "Reports" in page and "track used" in page and "wrycu-pc" in page
    # The merged report's own page leads to the landing.
    other = next(r["id"] for r in row["reports"] if r["id"] != row["id"])
    assert client.get(f"/passes/{other}", follow_redirects=False).headers["location"] == f"/passes/{row['id']}"


def test_regrade_grades_landings_only(hub):
    hub.ingest(1, *server_report("trap"))
    hub.ingest(2, *pilot_report("trap"))
    hub.ingest(2, *pilot_report("bolter"))  # a lone track report: nothing to grade it against
    assert hub.regrade(force=True) == (1, 0)


def test_pilot_mode_collector_uploads_its_own_track(tmp_path):
    async def run():
        server = await serve_recording(PAIRS / "client-trap.zip.acmi", port=0, speed=0)
        async with server:
            agent = Agent(AgentConfig(work_dir=tmp_path, tacview_port=server.sockets[0].getsockname()[1],
                                                  mode="pilot"))
            await agent.run_session()
        return agent

    agent = asyncio.run(run())
    (item,) = agent.outbox.pending()
    meta = item.meta()
    assert meta["kind"] == "track" and meta["pass"]["aoa_recorded"] and meta["pass"]["pilot"] == "Wrycu"
    assert meta["window"]["start"] <= meta["pass"]["start_time"] - 40


def test_livery_and_side_number_are_kept_and_shown(hub):
    from fastapi.testclient import TestClient

    from dcs_lso.hub.app import create_app
    data, meta = server_report("trap")
    meta["aircraft"] = {"livery": "VFA-106 high visibility", "onboard_num": "301", "unit": "Hornet 2"}
    hub.ingest(1, data, meta)
    hub.ingest(2, *pilot_report("trap"))  # the pilot's own track has no slot info; the landing keeps it
    # An earlier pass (uploaded later) in another slot: its livery is kept for that pass, but the pilot's side
    # number stays the first one recorded.
    data, meta = server_report("bolter")
    meta["aircraft"] = {"livery": "VFA-37", "onboard_num": "305"}
    hub.ingest(1, data, meta)
    client = TestClient(create_app(hub))
    landings = {x["outcome"]: x for x in client.get("/api/v1/passes", params={"days": 0}).json()}
    assert (landings["trap"]["livery"], landings["trap"]["modex"]) == ("VFA-106 high visibility", "301")
    assert (landings["bolter"]["livery"], landings["bolter"]["modex"]) == ("VFA-37", "301")
    page = client.get(f"/passes/{landings['bolter']['id']}").text
    assert "<dt>Side number</dt><dd>301</dd>" in page and "<dt>Livery</dt><dd>VFA-37</dd>" in page
    assert "#301 · VFA-37" in page  # the trap card's title line
    pilot = client.get("/pilots/Wrycu").text
    assert '<span class="modex">#301</span>' in pilot
    assert "Last livery: VFA-106 high visibility · " in pilot  # the newest landing's (the trap, at 03:31)
    trends = client.get("/api/v1/pilots/Wrycu/trends").json()
    assert (trends["modex"], trends["last_livery"]) == ("301", "VFA-106 high visibility")


def test_pilot_collector_adds_dcs_wire_from_its_debrief(tmp_path, hub):
    """A multiplayer client's DCS writes its own traps' grades and wires to debrief.log at mission end;
    the pilot's agent re-uploads its track report with it, and the landing gets DCS's wire."""
    from dcs_lso.dcslog import Debrief, DcsEvent

    async def run():
        server = await serve_recording(PAIRS / "client-trap.zip.acmi", port=0, speed=0)
        async with server:
            agent = Agent(AgentConfig(work_dir=tmp_path, tacview_port=server.sockets[0].getsockname()[1],
                                                  mode="pilot"))
            session = await agent.run_session()
        return agent, session

    agent, session = asyncio.run(run())
    (item,) = agent.outbox.pending()
    info = item.meta()["pass"]
    jet = info["aircraft_id"]
    assert info["aircraft_first_seen"] == session.first_seen[jet]
    # The client joined 129 s into the mission: debrief.log's times are 129 s ahead of the stream's.
    dcs_id, offset = 0x1000000 | (jet - 1), 129.0
    debrief_path = tmp_path / "debrief.log"
    debrief = Debrief(None, [
        DcsEvent("under control", info["aircraft_first_seen"] + offset, initiator_object_id=dcs_id),
        DcsEvent("landing quality mark", info["end_time"] - 2.0 + offset, place="CVN-75 Harry S. Truman",
                 initiator_unit_type="FA-18C_hornet", initiator_object_id=dcs_id,
                 comment="LSO: GRADE:C : _EGTL_  3PTSIW  WIRE# 2[BC]"),
    ])
    import dcs_lso.agent.service as collector_mod
    collector_mod.load_debrief = lambda path: debrief  # (the file's format is covered by test_dcslog)
    try:
        assert agent.apply_debrief(session.names, debrief_path) == 1
    finally:
        from dcs_lso.dcslog import load_debrief
        collector_mod.load_debrief = load_debrief
    (item,) = agent.outbox.pending()
    assert item.meta()["dcs"]["wire"] == 2
    # On hub: the server's report (no comms on the server, so no DCS grade there), the pilot's track as
    # first uploaded, then its re-upload with the pilot's own DCS grade: the landing now has DCS's wire.
    data, meta = server_report("trap")
    meta["dcs"] = {"wire": None, "grade": None}
    hub.ingest(1, data, meta)
    track = item.meta()
    hub.ingest(2, item.acmi.read_bytes(), dict(track, dcs={"wire": None, "grade": None}))
    (landing,) = landings(hub)
    assert landing["wire"] is None and landing["wire_estimated"] is not None  # until the debrief arrives: an estimate
    again = hub.ingest(2, item.acmi.read_bytes(), track)
    assert again.created is False
    (landing,) = landings(hub)
    assert landing["wire"] == 2 and landing["dcs_grade"].startswith("LSO: GRADE:C")  # DCS's own, from the pilot
