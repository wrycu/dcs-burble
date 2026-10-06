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


BOLTER = Path(__file__).parent / "fixtures" / "live" / "bolter-pilot-hook.zip.acmi"  # Wrycu's bolter, pass 27


def hook_upload(pilot: str = "Wrycu", source: Path = PILOT, shift: float = JOINED_S, **extra) -> dict:
    """`source`: an own-jet recording; `shift`: added to its times to make them mission time."""
    recording = load_recording(source)
    (jet,) = [o for o in recording.objects.values() if o.name == "FA-18C_hornet"]
    lines = ["t,x,y,z,heading,pitch,bank,aoa,lat,lon"]
    for s in jet.samples:
        t = s.transform
        lines.append(f"{s.time + shift:.4f},{t.v:.3f},{t.alt:.3f},{t.u:.3f},{math.radians(t.heading):.6f},"
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
DCS_BOLTER = "LSO: GRADE:B  _TMRDAR_  BIW [BC]"


def bolter_upload(**extra) -> dict:
    """Wrycu's bolter (pass 27), without the carrier: nothing to rebuild it from."""
    return hook_upload(source=BOLTER, shift=0.0, **extra)


def other_communitys_hub(tmp_path) -> tuple[Hub, TestClient, str]:
    """A hub with no server agent of its own: the pilot hook sends it everything, with a pilot token."""
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    return hub, client_at(hub, "8.8.4.4"), hub.add_pilot_token("Wrycu", "pilot hook")


def test_a_bolter_with_dcss_grade_is_a_landing_graded_by_dcs(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (track,) = post(client, bolter_upload(), token).json()["reports"]  # no carrier visible: waits as a track
    assert "waiting" in track["text"] and client.get("/api/v1/passes?days=0").json() == []
    # After the mission: DCS's grade from debrief.log, sent again. A bolter can't rebuild the carrier.
    (again,) = post(client, bolter_upload(dcs_grade=DCS_BOLTER), token).json()["reports"]
    assert again["pass_id"] == track["pass_id"] and again["grade"] == "B"
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert (landing["outcome"], landing["wire"], landing["grade"], landing["text"]) == ("bolter", None, "B", "B : _TMRDAR_ BIW [BC]")
    page = client.get(f"/passes/{landing['id']}").text
    assert "graded by DCS&#x27;s LSO only" in page or "graded by DCS's LSO only" in page
    assert client.get(f"/passes/{landing['id']}/card.svg").status_code == 404
    hub.regrade(force=True)  # stays as it is
    assert client.get("/api/v1/passes?days=0").json()[0]["text"] == "B : _TMRDAR_ BIW [BC]"


def test_a_bolter_sent_with_dcss_grade_the_first_time(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (report,) = post(client, bolter_upload(dcs_grade=DCS_BOLTER), token).json()["reports"]  # e.g. sent after the mission
    assert report["grade"] == "B"
    assert [p["id"] for p in client.get("/api/v1/passes?days=0").json()] == [report["pass_id"]]


def test_a_report_with_the_carrier_takes_over_from_a_rebuilt_carrier(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (dcs_only,) = post(client, hook_upload(dcs_grade=DCS_GRADE), token).json()["reports"]
    with hub.sessions() as s:
        assert s.get(Pass, dcs_only["pass_id"]).kind == "rebuilt"
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
    return {x["name"]: x["present"] for x in a["parts"]}, {k: a[k]["level"] for k in ("overall", "approach", "wire", "comms")}


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
    post(client, bolter_upload(dcs_grade=DCS_BOLTER), token)
    parts, scores = accuracy_of(client)
    assert parts == {"Server report": False, "Pilot hook": True, "DCS comms": True}
    assert scores == {"overall": "Low", "approach": "None", "wire": "n/a", "comms": "None"}


def test_accuracy_is_stored_with_the_grade_and_says_where_it_was_flown(tmp_path, hub):
    client = client_at(hub)
    hub.ingest(1, *server_report())
    post(client, {**hook_upload(), "version": 3, "here": True}, hub.token)
    with hub.sessions() as s:
        (landing,) = [p for p in s.query(Pass).all() if p.merged_into_id is None and not p.is_track]
        stored = landing.grade.detail["accuracy"]
    assert stored["overall"]["level"] == "Full" and stored["flown"] == {"where": "here", "note": "server1"}
    page = client.get(f"/passes/{landing.id}").text
    assert "Flown on" in page and "this hub&#x27;s servers (server1)" in page
    card = client.get(f"/passes/{landing.id}/card.svg").text
    assert "Full accuracy" in card and "another server" not in card


@pytest.mark.parametrize("extra, where", [({"here": False}, "elsewhere"), ({"server": None}, "single player"),
                                          ({}, "unknown")])
def test_where_a_pilot_hooks_landing_was_flown(tmp_path, extra, where):
    hub, client, token = other_communitys_hub(tmp_path)
    (report,) = post(client, {**carrier_upload(), "version": 3, **extra}, token).json()["reports"]
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert landing["accuracy"]["flown"]["where"] == where
    card = client.get(f"/passes/{report['pass_id']}/card.svg").text
    assert ("another server" in card) == (where == "elsewhere")


def test_accuracy_is_rescored_when_dcss_grade_arrives_later(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (first,) = post(client, carrier_upload(), token).json()["reports"]  # the landing itself (has the carrier)
    parts, scores = accuracy_of(client)
    assert not parts["DCS comms"]
    post(client, carrier_upload(dcs_grade="LSO: GRADE:OK : (LOAR)  WIRE# 3"), token)  # after the mission
    parts, scores = accuracy_of(client)
    assert parts["DCS comms"] and scores["wire"] == "Full"
    with hub.sessions() as s:
        assert s.get(Pass, first["pass_id"]).grade.detail["accuracy"]["wire"]["note"] == "#3, from DCS"


@pytest.mark.parametrize("default", ["shown", "hidden"])
def test_landings_from_other_servers_can_be_hidden(tmp_path, default):
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any", other_servers=default)
    client, token = client_at(hub, "8.8.4.4"), hub.add_pilot_token("Wrycu", "pilot hook")
    post(client, {**carrier_upload(), "version": 3, "here": False}, token)  # flown on another community's server

    def ids(query: str = "") -> int:
        return len(client.get(f"/api/v1/passes?days=0{query}").json())

    assert (ids(), ids("&servers=all"), ids("&servers=ours")) == ((1 if default == "shown" else 0), 1, 0)
    board = client.get("/?days=0").text
    assert ('value="ours" selected' in board) == (default == "hidden") and "This hub&#x27;s servers" in board
    assert ("Wrycu" in client.get("/?days=0&servers=ours").text.split("</form>")[1]) is False
    assert "Wrycu" in client.get("/?days=0&servers=all").text.split("</form>")[1]
    # The pilot's page and trends follow the same default, with the same filter.
    def landings(query: str = "") -> int:
        return client.get(f"/api/v1/pilots/Wrycu/trends{query}").json()["landings"]

    assert (landings(), landings("?servers=all"), landings("?servers=ours")) == ((1 if default == "shown" else 0), 1, 0)
    page = client.get("/pilots/Wrycu").text
    assert ('value="ours" selected' in page) == (default == "hidden") and 'name="servers"' in page
    from dcs_lso.hub.discord import Discord
    import httpx
    discord = Discord(hub, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), start=False)
    assert [r.name for r in discord.board_rows()] == ([] if default == "hidden" else ["Wrycu"])


def test_a_trap_on_another_server_is_graded_against_the_carrier_rebuilt_from_the_jet(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    # Flown on a server that shares no objects: no carrier in the upload, and no server agent of this hub there.
    (report,) = post(client, {**hook_upload(), "version": 3, "here": False}, token).json()["reports"]
    assert report["grade"]  # graded straight away, not waiting
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert (landing["outcome"], landing["wire"], landing["wire_estimated"]) == ("trap", None, None)
    a = landing["accuracy"]
    assert (a["approach"]["level"], a["wire"]["level"], a["flown"]["where"]) == ("Low", "Low", "elsewhere")
    page = client.get(f"/passes/{landing['id']}").text
    assert "rebuilt from the jet" in page and client.get(f"/passes/{landing['id']}/card.svg").status_code == 200
    # DCS's grade arrives after the mission: the carrier is placed by DCS's wire now.
    post(client, {**hook_upload(dcs_grade="LSO: GRADE:OK : (LOAR)  WIRE# 2"), "version": 3, "here": False}, token)
    (landing,) = client.get("/api/v1/passes?days=0").json()
    assert landing["wire"] == 2 and landing["accuracy"]["approach"]["level"] == "Medium"
    assert landing["accuracy"]["wire"]["level"] == "Full"


def test_a_track_on_this_hubs_server_waits_for_the_server_report(tmp_path):
    hub, client, token = other_communitys_hub(tmp_path)
    (report,) = post(client, {**hook_upload(), "version": 3, "here": True}, token).json()["reports"]
    assert "waiting" in report["text"] and client.get("/api/v1/passes?days=0").json() == []


def test_the_rebuilt_carrier_is_close_to_the_real_one():
    """Pass 25: the pilot hook recorded the real carrier too. Rebuilt from the jet alone (placed by wire 2)."""
    import math
    from dcs_lso.detect.passes import CarrierTimeline
    from dcs_lso.detect.rebuild import rebuild_carrier
    from dcs_lso.geometry import AIRCRAFT, CARRIERS
    recording = load_recording(Path(__file__).parent / "fixtures" / "live" / "trap-pilot-hook.zip.acmi")
    (p,) = [x for x in find_passes(recording) if x.outcome.value == "trap"]
    carrier, plane = recording.objects[p.carrier_id], recording.objects[p.aircraft_id]
    rebuilt = rebuild_carrier(plane, CARRIERS[carrier.name], AIRCRAFT[plane.name], wire=2)
    real, ours = CarrierTimeline(carrier.samples), CarrierTimeline(rebuilt.samples)
    for back_s, within_m in ((0, 3.0), (30, 15.0)):
        a, b = real.at(rebuilt.stop_time - back_s), ours.at(rebuilt.stop_time - back_s)
        assert math.hypot(a.u - b.u, a.v - b.v) < within_m
    assert abs((rebuilt.heading - real.at(rebuilt.stop_time).heading + 180) % 360 - 180) < 0.5
    assert abs(rebuilt.speed_ms - 13.9) < 1.0


def test_a_player_who_left_hours_ago_is_still_recognised(tmp_path, hub):
    """The pilot hook sends again after the mission (DCS's grade), or in the next DCS session: without a token,
    the hub still knows the player from its server (same address), up to a day later."""
    from datetime import timedelta
    from dcs_lso.hub.db import PlayerSeen
    with hub.sessions.begin() as s:
        for row in s.query(PlayerSeen):
            row.connected, row.last_seen = False, row.last_seen - timedelta(hours=2)  # left two hours ago
    assert post(client_at(hub, HOME), hook_upload(), None).status_code == 200
    with hub.sessions.begin() as s:
        for row in s.query(PlayerSeen):
            row.last_seen = row.last_seen - timedelta(days=2)
    assert post(client_at(hub, HOME), hook_upload(), None).status_code == 401  # too long ago
