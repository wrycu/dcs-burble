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
from dcs_lso.hub.db import Pass
from dcs_lso.hub.pilothook import HookUploadError, parse_upload
from dcs_lso.hub.service import Hub
from dcs_lso.slices import sidecar, slice_objects

WIRES = Path(__file__).parent / "fixtures" / "wires"
SERVER = WIRES / "server-dcs-wire-2.zip.acmi"  # the server's recording: starts at mission start
PILOT = WIRES / "pilot-dcs-wire-2.zip.acmi"  # the pilot's own recording of the same trap
JOINED_S = 174.0  # the pilot's recording starts at 05:02:54Z, the mission at 05:00:00Z


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
            "ucid": "fa2691780c6ae51b644a3a84aea04ceb", "server": "192.168.1.238:10308",
            "csv": "\n".join(lines), **extra}


def server_report() -> tuple[bytes, dict]:
    r = load_recording(SERVER)
    (p,) = find_passes(r)
    return SERVER.read_bytes(), sidecar(r, p, "s", slice_objects(r, p))


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.add_source("server1")
    h.token = h.add_pilot_token("Wrycu", "pilot hook")
    return h


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
    client = TestClient(create_app(hub))
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


def test_pilot_hook_needs_a_pilot_token(hub):
    client = TestClient(create_app(hub))
    assert post(client, hook_upload(), None).status_code == 401
    assert post(client, hook_upload(), "wrong").status_code == 401
    server_token = hub.add_source("server2")
    r = post(client, hook_upload(), server_token)
    assert r.status_code == 400 and "pilot token" in r.text
    assert post(client, hook_upload(token=hub.token), None).status_code == 200  # the token in the body


def test_invalid_uploads(hub):
    client = TestClient(create_app(hub))
    assert post(client, hook_upload(aircraft="A-10C"), hub.token).status_code == 400
    body = hook_upload()
    body["csv"] = body["csv"].replace("aoa,", "angle,", 1)
    assert "missing columns" in post(client, body, hub.token).text
    assert client.post("/api/v1/pilot-hook/approaches", content=b"not json",
                       headers={"Authorization": f"Bearer {hub.token}"}).status_code == 400
    with pytest.raises(HookUploadError):
        parse_upload({"pilot": "Wrycu", "aircraft": "FA-18C_hornet", "mission": "m", "csv": "t,x\n1,2"})


def test_upload_is_credited_to_the_tokens_pilot(hub):
    client = TestClient(create_app(hub))
    hub.ingest(1, *server_report())
    post(client, hook_upload(pilot="CVW-17 | Wrycu"), hub.token)
    assert [a.name for a in hub.pilot_aliases("Wrycu")] == ["CVW-17 | Wrycu"]
