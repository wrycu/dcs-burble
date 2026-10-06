"""Uploads from the pilot hook: the pilot's own jet from DCS's Export.lua, on the mission clock, merged with
the server agent's report of the landing.

The upload is built from the pilot's own Tacview recording of a real wire-2 trap (its clock starts 174 s
after mission start), converted to what the pilot hook sends: DCS coordinates and mission time.
"""

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.detect import find_passes
from dcs_lso.hub.app import create_app
from dcs_lso.hub.db import Pass, Source
from sqlalchemy import select
from dcs_lso.hub.pilothook import HookUploadError, parse_upload
from dcs_lso.hub.service import Hub
from dcs_lso.slices import sidecar, slice_objects

WIRES = Path(__file__).parent / "fixtures" / "wires"
SERVER = WIRES / "server-dcs-wire-2.zip.acmi"  # the server's recording: starts at mission start
PILOT = WIRES / "pilot-dcs-wire-2.zip.acmi"  # the pilot's own recording of the same trap
JOINED_S = 174.0  # the pilot's recording starts at 05:02:54Z, the mission at 05:00:00Z
UCID = "fa2691780c6ae51b644a3a84aea04ceb"
HOME = "69.222.184.25"  # the pilot's address, as the DCS server sees it


def hook_upload(pilot: str = "Wrycu", **extra) -> dict:
    recording = load_recording(PILOT)
    (jet,) = [o for o in recording.objects.values() if o.name == "FA-18C_hornet"]
    lines = ["t,x,y,z,heading,pitch,bank,aoa,lat,lon"]
    for s in jet.samples:
        t = s.transform
        lines.append(f"{s.time + JOINED_S:.4f},{t.v:.3f},{t.alt:.3f},{t.u:.3f},{math.radians(t.heading):.6f},"
                     f"{math.radians(t.pitch):.6f},{math.radians(t.roll):.6f},{s.aoa if s.aoa is not None else 'nil'},"
                     f"{t.lat:.7f},{t.lon:.7f}")
    return {"version": 1, "pilot": pilot, "aircraft": "FA-18C_hornet", "mission": recording.globals["Title"],
            "ucid": UCID, "server": "192.168.1.238:10308",
            "csv": "\n".join(lines), **extra}


def server_report() -> tuple[bytes, dict]:
    r = load_recording(SERVER)
    (p,) = find_passes(r)
    return SERVER.read_bytes(), sidecar(r, p, "s", slice_objects(r, p))


def make_hub(path: Path, **options) -> Hub:
    path.mkdir(parents=True, exist_ok=True)
    h = Hub(f"sqlite:///{path / 'lso.db'}", path / "hub", **options)
    h.add_source("server1")
    h.token = h.add_pilot_token("Wrycu", "pilot hook")
    # The server agent: Wrycu is connected to its DCS server.
    h.report_players(1, [{"ucid": UCID, "ip": HOME, "name": "Wrycu"}])
    return h


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path)


def client_at(hub, address: str = "testclient") -> TestClient:
    return TestClient(create_app(hub), client=(address, 50000))


def post(client, body: dict, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/v1/pilot-hook/approaches", content=json.dumps(body), headers=headers)


def landing_of(hub, pass_id: int):
    with hub.sessions() as s:
        row = s.get(Pass, pass_id)
        landing_id = row.merged_into_id or row.id
        return hub.load_pass(s.get(Pass, landing_id))


@pytest.mark.parametrize("hook_first", [False, True])
def test_pilot_hook_track_merges_with_the_server_agents_report(hub, hook_first):
    client = client_at(hub)
    if not hook_first:
        hub.ingest(1, *server_report())
    r = post(client, hook_upload(), hub.token)
    assert r.status_code == 200, r.text
    (report,) = r.json()["reports"]
    if hook_first:
        assert report["grade"] == "" and "waiting" in report["text"]  # no carrier yet
        hub.ingest(1, *server_report())
    landing = landing_of(hub, report["pass_id"])
    # Graded from the pilot hook's track (recorded AOA), against the server's carrier: the wire estimate
    # works (the server's own track would read wire 3).
    assert landing.track_source == "Wrycu: pilot hook" and landing.wire_estimate == 2
    rows = client.get("/api/v1/passes", params={"days": 0}).json()
    assert len(rows) == 1 and rows[0]["pilot"] == "Wrycu"


def test_who_may_send(hub, tmp_path):
    body = hook_upload()
    # A player on this hub's server, from the address the server sees them at: no token needed. Credited under
    # the server's name for them.
    r = post(client_at(hub, HOME), hook_upload(pilot="whatever the hook says"), None)
    assert r.status_code == 200, r.text
    assert client_at(hub).get("/api/v1/passes", params={"days": 0}).json() == []  # a track: waits for the server's report
    hub.ingest(1, *server_report())
    assert [x["pilot"] for x in client_at(hub).get("/api/v1/passes", params={"days": 0}).json()] == ["Wrycu"]
    # From the LAN (the hub sees the LAN address, the DCS server another): trusted too.
    assert post(client_at(hub, "192.168.1.50"), body, None).status_code == 200
    # Anyone else, or another player's UCID, needs a token; a server agent token isn't one.
    assert post(client_at(hub, "8.8.4.4"), body, None).status_code == 401
    assert post(client_at(hub, HOME), {**body, "ucid": "someone else"}, None).status_code == 401
    assert post(client_at(hub, "8.8.4.4"), body, hub.add_source("server2")).status_code == 401
    assert post(client_at(hub, "8.8.4.4"), body, hub.token).status_code == 200
    assert post(client_at(hub, "8.8.4.4"), hook_upload(token=hub.token), None).status_code == 200  # token in the body


def test_our_servers_or_any(tmp_path):
    for accept, expected in (("ours", 403), ("any", 200)):
        hub = make_hub(tmp_path / accept, pilot_hook_accept=accept)
        # Flown somewhere else: this hub's servers never saw this UCID.
        r = post(client_at(hub, "8.8.4.4"), {**hook_upload(), "ucid": "flown elsewhere"}, hub.token)
        assert r.status_code == expected, r.text
    with pytest.raises(ValueError):
        Hub(f"sqlite:///{tmp_path / 'x.db'}", tmp_path / "x", pilot_hook_accept="maybe")


def test_is_this_player_here(hub):
    here = lambda address, ucid=UCID: client_at(hub, address).get("/api/v1/pilot-hook/here", params={"ucid": ucid}).json()["here"]  # noqa: E731
    assert here(HOME) and here("192.168.1.50")
    assert not here("8.8.4.4")  # nobody can look players up from elsewhere
    assert not here(HOME, "someone else")
    hub.report_players(1, [])  # Wrycu left
    assert not here(HOME)
    # A player still counts as having flown here for a while (their last approach can arrive late).
    assert post(client_at(hub, HOME), hook_upload(), None).status_code == 200
    # Only server agents report players.
    r = client_at(hub).post("/api/v1/players", json={"players": []}, headers={"Authorization": f"Bearer {hub.token}"})
    assert r.status_code == 403


def test_invalid_uploads(hub):
    client = client_at(hub)
    assert post(client, hook_upload(aircraft="A-10C"), hub.token).status_code == 400
    body = hook_upload()
    body["csv"] = body["csv"].replace("aoa,", "angle,", 1)
    assert "missing columns" in post(client, body, hub.token).text
    assert client.post("/api/v1/pilot-hook/approaches", content=b"not json",
                       headers={"Authorization": f"Bearer {hub.token}"}).status_code == 400
    with pytest.raises(HookUploadError):
        parse_upload({"pilot": "Wrycu", "aircraft": "FA-18C_hornet", "mission": "m", "csv": "t,x\n1,2"})


def test_upload_is_credited_to_the_tokens_pilot(hub):
    client = client_at(hub)
    hub.ingest(1, *server_report())
    post(client, hook_upload(pilot="CVW-17 | Wrycu"), hub.token)
    assert [a.name for a in hub.pilot_aliases("Wrycu")] == ["CVW-17 | Wrycu"]


def carrier_upload(**extra) -> dict:
    """The upload with the carrier the pilot approached (the server's recording of it, mission time)."""
    server = load_recording(SERVER)
    (carrier,) = [o for o in server.objects.values() if o.name == "CVN_75"]
    lines = ["t,x,y,z,heading,lat,lon"]
    for s in carrier.samples:
        t = s.transform
        lines.append(f"{s.time:.4f},{t.v:.3f},{t.alt or 0:.3f},{t.u:.3f},{math.radians(t.heading):.6f},{t.lat:.7f},{t.lon:.7f}")
    return hook_upload(carrier={"type": "CVN_75", "unit": carrier.pilot, "csv": "\n".join(lines)}, **extra)


def test_with_the_carrier_a_hook_upload_is_graded_on_its_own(tmp_path):
    # Another community's hub: no server agent, accepts traps from any server with the pilot's token.
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    token = hub.add_pilot_token("Wrycu", "pilot hook")
    r = post(client_at(hub, "8.8.4.4"), carrier_upload(), token)
    assert r.status_code == 200, r.text
    (report,) = r.json()["reports"]
    assert report["grade"] and report["created"]  # graded straight away: no server report needed
    landing = landing_of(hub, report["pass_id"])
    assert landing.outcome.value == "trap" and landing.wire_estimate == 2 and landing.carrier_type == "CVN_75"
    rows = client_at(hub).get("/api/v1/passes", params={"days": 0}).json()
    assert [(x["pilot"], x["outcome"], x["carrier"]) for x in rows] == [("Wrycu", "trap", "CVN-75 Harry S. Truman")]
    with hub.sessions() as s:
        assert s.get(Pass, report["pass_id"]).night is None  # mission start (UTC) unknown without a server report


def test_with_the_carrier_it_still_merges_with_the_server_agents_report(hub):
    hub.ingest(1, *server_report())
    r = post(client_at(hub, HOME), carrier_upload(), None)
    (report,) = r.json()["reports"]
    landing = landing_of(hub, report["pass_id"])
    assert landing.track_source == "pilot hooks" and landing.wire_estimate == 2
    rows = client_at(hub).get("/api/v1/passes", params={"days": 0}).json()
    assert len(rows) == 1 and len(rows[0]["reports"]) == 2


def test_unknown_ships_are_ignored(hub):
    body = carrier_upload()
    body["carrier"]["type"] = "LHA_Tarawa"  # no deck data: as if no carrier was sent
    (report,) = post(client_at(hub, HOME), body, None).json()["reports"]
    assert report["grade"] == "" and "waiting" in report["text"]


@pytest.mark.parametrize(("elevation", "night"), [(-12.5, True), (30.0, False), (None, None)])
def test_night_from_dccs_sun_without_a_server_agent(tmp_path, elevation, night):
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    token = hub.add_pilot_token("Wrycu", "pilot hook")
    body = carrier_upload() if elevation is None else carrier_upload(sun_elevation=elevation)
    (report,) = post(client_at(hub, "8.8.4.4"), body, token).json()["reports"]
    with hub.sessions() as s:
        assert s.get(Pass, report["pass_id"]).night is night


CALLS = [{"time": 1000.0, "along": 900.0, "call": "you're high"}, {"time": 1010.0, "along": 120.0, "call": "power"}]


def test_relaying_calls_to_another_hub(tmp_path):
    # Community A: its server agent made the calls; the pilot hook's report merges into that landing.
    a = make_hub(tmp_path / "a")
    data, meta = server_report()
    meta["calls"] = [{**c, "time": c["time"] - 1000 + meta["pass"]["start_time"]} for c in CALLS]
    client_a = client_at(a, HOME)
    (report,) = post(client_a, carrier_upload(), None).json()["reports"]
    asked = lambda: client_a.get("/api/v1/pilot-hook/calls", params={"pass_id": report["pass_id"], "ucid": UCID})  # noqa: E731
    assert asked().json() == {"ready": False, "calls": []}  # no server report yet: the calls may still come
    a.ingest(1, data, meta)
    answer = asked().json()
    assert answer["ready"] and [c["call"] for c in answer["calls"]] == ["you're high", "power"]
    # Only that pilot hook may ask: another address, another UCID.
    assert client_at(a, "8.8.4.4").get("/api/v1/pilot-hook/calls", params={"pass_id": report["pass_id"], "ucid": UCID}).status_code == 401
    assert client_a.get("/api/v1/pilot-hook/calls", params={"pass_id": report["pass_id"], "ucid": "other"}).status_code in (401, 404)
    # Community B: no server agent; the pilot hook relays A's calls with its upload.
    b = Hub(f"sqlite:///{tmp_path / 'b.db'}", tmp_path / "b", pilot_hook_accept="any")
    token = b.add_pilot_token("Wrycu", "pilot hook")
    body = carrier_upload(calls=answer["calls"], calls_from="lso.wrycu.com")
    (relayed,) = post(client_at(b, "8.8.4.4"), body, token).json()["reports"]
    with b.sessions() as s:
        assert [c["call"] for c in s.get(Pass, relayed["pass_id"]).calls] == ["you're high", "power"]
    page = client_at(b).get(f"/passes/{relayed['pass_id']}").text
    assert "relayed by the pilot hook" in page and "lso.wrycu.com" in page and "Power (" in page


def test_dcs_grade_added_by_a_later_upload(tmp_path):
    """The pilot hook sends an approach again once DCS's debrief.log has its grade: the hub adds it."""
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    token = hub.add_pilot_token("Wrycu", "pilot hook")
    client = client_at(hub, "8.8.4.4")
    (first,) = post(client, carrier_upload(), token).json()["reports"]
    (again,) = post(client, carrier_upload(dcs_grade="LSO: GRADE:OK : (LOAR)  WIRE# 2"), token).json()["reports"]
    assert again["pass_id"] == first["pass_id"] and not again["created"]
    with hub.sessions() as s:
        landing = s.get(Pass, first["pass_id"])
        assert landing.dcs_grade == "LSO: GRADE:OK : (LOAR)  WIRE# 2" and landing.wire == 2
    assert "#2 (DCS)" in client.get(f"/passes/{first['pass_id']}").text


def test_an_old_pilot_hook_is_told_to_update(hub):
    from dcs_lso.hub.pilothook import PILOT_HOOK_VERSION
    client = client_at(hub)
    old = post(client, {**hook_upload(), "version": PILOT_HOOK_VERSION - 1}, hub.token).json()["pilot_hook"]
    current = post(client, {**hook_upload(), "version": PILOT_HOOK_VERSION}, hub.token).json()["pilot_hook"]
    assert old == {"latest": PILOT_HOOK_VERSION, "update": True} and current["update"] is False
    lua = (Path(__file__).parents[1] / "pilot-hook/Scripts/Hooks/dcs-lso-pilot-hook.lua").read_text()
    assert f"local VERSION = {PILOT_HOOK_VERSION}\n" in lua  # bump both together


def test_wire_check_measures_the_server_copy_against_the_pilots_own_track(hub, capsys):
    hub.ingest(1, *server_report())
    assert [r.known for r in hub.wire_check()] == [None]  # only the server's copy: the wire isn't known
    post(client_at(hub), hook_upload(), hub.token)
    (row,) = hub.wire_check()
    assert (row.known, row.known_from) == (2, "own track")  # from the pilot hook's track
    assert 5 < row.overshoot_m < 20 and row.signals.wire in (None, 2)
    from dcs_lso.cli import main
    data_dir = hub.store.root.parent
    assert main(["hub", "--data-dir", str(data_dir), "--database-url", f"sqlite:///{data_dir.parent / 'lso.db'}",
                 "wire-check"]) == 0
    assert "the wire known on 1 (0 from DCS)" in capsys.readouterr().out


DCS_GRADE = "LSO: GRADE:(OK) : _LULX_ 3PTSIW  WIRE# 3"


def other_communitys_hub(tmp_path) -> tuple[Hub, TestClient, str]:
    """A hub with no server agent of its own: the pilot hook sends it everything, with a pilot token."""
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    return hub, client_at(hub, "8.8.4.4"), hub.add_pilot_token("Wrycu", "pilot hook")


def test_a_track_with_dcss_grade_is_a_landing_graded_by_dcs(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (track,) = post(client, hook_upload(), token).json()["reports"]  # no carrier visible: waits as a track
    assert "waiting" in track["text"] and client.get("/api/v1/passes?days=0").json() == []
    # After the mission: DCS's grade from debrief.log, sent again.
    (again,) = post(client, hook_upload(dcs_grade=DCS_GRADE), token).json()["reports"]
    assert again["pass_id"] == track["pass_id"] and again["grade"] == "(OK)"
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert (landing["outcome"], landing["wire"], landing["grade"], landing["text"]) == ("trap", 3, "(OK)", "(OK) : _LULX_ 3PTSIW")
    page = client.get(f"/passes/{landing['id']}").text
    assert "graded by DCS&#x27;s LSO only" in page or "graded by DCS's LSO only" in page
    assert client.get(f"/passes/{landing['id']}/card.svg").status_code == 404
    hub.regrade(force=True)  # stays as it is
    assert client.get("/api/v1/passes?days=0").json()[0]["text"] == "(OK) : _LULX_ 3PTSIW"


def test_a_track_sent_with_dcss_grade_the_first_time(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (report,) = post(client, hook_upload(dcs_grade=DCS_GRADE), token).json()["reports"]  # e.g. sent after the mission
    assert report["grade"] == "(OK)"
    assert [p["id"] for p in client.get("/api/v1/passes?days=0").json()] == [report["pass_id"]]


def test_a_report_with_the_carrier_takes_over_from_dcss_grade(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (dcs_only,) = post(client, hook_upload(dcs_grade=DCS_GRADE), token).json()["reports"]
    hub.add_source("server1")
    with hub.sessions() as s:
        server1 = s.scalar(select(Source.id).where(Source.name == "server1"))
    hub.ingest(server1, *server_report())  # e.g. this community's server agent was late
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert landing["id"] != dcs_only["pass_id"] and dcs_only["pass_id"] in {r["id"] for r in landing["reports"]}
    # Graded by us now (DCS's grade and wire are kept with it), with a trap card.
    assert landing["wire"] == 3 and landing["dcs_grade"] == DCS_GRADE and landing["text"] != "(OK) : _LULX_ 3PTSIW"
    assert client.get(f"/passes/{landing['id']}/card.svg").status_code == 200


def accuracy_of(client, pass_id=None):
    (landing,) = [p for p in client.get("/api/v1/passes?days=0").json() if pass_id in (None, p["id"])]
    a = landing["accuracy"]
    return a["parts"], {k: a[k]["level"] for k in ("overall", "approach", "wire", "comms")}


def test_accuracy_rows(tmp_path, hub):
    client = client_at(hub)
    hub.ingest(1, *server_report())  # the server report alone: its copy of the jet
    parts, scores = accuracy_of(client)
    assert parts == {"Server report": True, "Pilot hook": False, "DCS comms": False}
    assert scores == {"overall": "High", "approach": "High", "wire": "None", "comms": "None"}
    post(client, hook_upload(), hub.token)  # plus the pilot hook's own track: our wire estimate
    parts, scores = accuracy_of(client)
    assert parts == {"Server report": True, "Pilot hook": True, "DCS comms": False}
    assert scores == {"overall": "Full", "approach": "Full", "wire": "High", "comms": "None"}
    page = client.get(f"/passes/{client.get('/api/v1/passes?days=0').json()[0]['id']}").text
    assert "<h2>Accuracy</h2>" in page and "level-Full" in page and "estimated from where the jet stopped" in page


def test_accuracy_of_a_landing_graded_by_dcs_alone(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    post(client, hook_upload(dcs_grade=DCS_GRADE), token)
    parts, scores = accuracy_of(client)
    assert parts == {"Server report": False, "Pilot hook": True, "DCS comms": True}
    assert scores == {"overall": "Low", "approach": "None", "wire": "Full", "comms": "None"}
