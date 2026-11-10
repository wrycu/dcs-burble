"""Two reports of one landing: the server's (carrier, 4.8 Hz, no AOA) and the pilot's own Tacview
(own jet only, ~8 Hz, real AOA). Hub combines them into one landing graded from the pilot's
track against the server's carrier, keeping the server's DCS grade, wire, and live calls."""

from pathlib import Path

import pytest

from burble.acmi import load_recording
from burble.hub.service import Hub
from burble.detect import find_passes
from burble.detect.approaches import find_approaches
from burble.slices import sidecar, slice_objects, track_sidecar

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
    c.pilot = c.add_pilot_token("Wrycu", "wrycu-pc")
    return c


def landings(hub: Hub):
    from fastapi.testclient import TestClient

    from burble.hub.app import create_app
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
    assert sorted(r["source"] for r in landing["reports"]) == ["Wrycu: wrycu-pc", "server1"]
    # The server's knowledge is kept...
    assert landing["wire"] == 3 and landing["dcs_grade"] == "LSO: GRADE:OK : WIRE# 3" and landing["calls"] == CALLS
    # ...and the pilot's track (recorded AOA, higher rate) is what's graded.
    with hub.sessions() as s:
        from burble.hub.db import Pass
        row = s.get(Pass, landing["id"])
        result = hub.load_pass(row)
    assert result.track_source == "Wrycu: wrycu-pc"
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

    from burble.hub.app import create_app
    hub.ingest(1, *server_report("trap"))
    hub.ingest(2, *pilot_report("trap"))
    client = TestClient(create_app(hub))
    for source in ("server1", "Wrycu: wrycu-pc"):
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




def test_livery_and_side_number_are_kept_and_shown(hub):
    from fastapi.testclient import TestClient

    from burble.hub.app import create_app
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
    assert "#301 · VFA-37" not in page  # the pass page's card is bare: the details above say it
    assert "#301 · VFA-37" in client.get(f"/passes/{landings['bolter']['id']}/card.svg").text  # the full card's title line
    pilot = client.get("/pilots/Wrycu").text
    assert '<span class="modex">#301</span>' in pilot
    assert "Last livery" not in pilot
    trends = client.get("/api/v1/pilots/Wrycu/trends").json()
    assert (trends["modex"], trends["last_livery"]) == ("301", "VFA-106 high visibility")


