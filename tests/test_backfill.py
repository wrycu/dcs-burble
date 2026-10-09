"""Backfill: whole Tacview recordings uploaded to hub, sliced and merged with known landings."""

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from burble.acmi import load_recording
from burble.hub.app import create_app
from burble.hub.service import Hub
from burble.dcslog import Debrief, DcsEvent, track_dcs_grades
from burble.detect import find_passes
from burble.detect.approaches import find_approaches
from burble.slices import sidecar, slice_objects, slice_recording

FIXTURES = Path(__file__).parent / "fixtures"
AI_TRAP = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
SERVER = FIXTURES / "wires" / "server-dcs-wire-2.zip.acmi"  # the server's copy of a trap (DCS: wire 2)
PILOT = FIXTURES / "wires" / "pilot-dcs-wire-2.zip.acmi"  # the same trap in the pilot's own recording


def own_jet(path: Path) -> int:
    (jet,) = [o.id for o in load_recording(path).objects.values() if o.name == "FA-18C_hornet"]
    return jet


def client_debrief(path: Path, offset: float, comment: str = "LSO: GRADE:C : _EGTL_  3PTSIW  WIRE# 2[BC]") -> Debrief:
    """A client's debrief.log for its own jet: mission times run `offset` s ahead of the recording's."""
    recording = load_recording(path)
    jet = own_jet(path)
    dcs_id = 0x1000000 | (jet - 1)  # DCS object id behind Tacview id `jet`
    (approach,) = find_approaches(recording, jet)
    return Debrief(None, [
        DcsEvent("under control", recording.objects[jet].samples[0].time + offset, initiator_object_id=dcs_id),
        DcsEvent("landing quality mark", approach.end_time - 2.0 + offset, place="CVN-75 Harry S. Truman",
                 initiator_unit_type="FA-18C_hornet", initiator_object_id=dcs_id, comment=comment),
    ])


def with_recording_time(path: Path, out: Path, recording_time: str) -> Path:
    """A copy of a recording that looks like a different recording of the same session (e.g. the
    server's own Tacview file of a pass its agent already sent)."""
    with zipfile.ZipFile(path) as z:
        (name,) = z.namelist()
        text = z.read(name).decode("utf-8-sig")
    lines = [f"0,RecordingTime={recording_time}" if line.startswith("0,RecordingTime=") else line
             for line in text.splitlines()]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return out


@pytest.fixture
def hub(tmp_path):
    c = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    c.add_source("server1")
    c.add_pilot_token("Wrycu", "pilot")
    return c


def landings(hub):
    return TestClient(create_app(hub)).get("/api/v1/passes", params={"days": 0}).json()


def test_slice_recording_finds_passes_and_own_tracks(tmp_path):
    # A recording with the carrier: one pass, and no second (track) report of the same approach.
    assert [m["kind"] for _, m in slice_recording(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi",
                                                  tmp_path / "a")] == ["pass"]
    assert [m["kind"] for _, m in slice_recording(AI_TRAP, tmp_path / "b")] == ["pass"]
    # A client's recording (own jet only): a track report.
    ((acmi, meta),) = slice_recording(PILOT, tmp_path / "c")
    assert meta["kind"] == "track" and meta["pass"]["aoa_recorded"] and acmi.exists()


def test_client_debrief_grades_are_matched_across_the_clock_offset():
    recording = load_recording(PILOT)
    approaches = list(find_approaches(recording, own_jet(PILOT)))
    # The client joined 129 s into the mission: its recording's clock is 129 s behind mission time.
    (grade,) = track_dcs_grades(recording, approaches, client_debrief(PILOT, 129.0)).values()
    assert grade.wire == 2
    # Another aircraft's grade isn't taken.
    other = Debrief(None, [DcsEvent(e.type, e.time, e.place, None, e.initiator_unit_type, 0x1000999, None, e.comment)
                           for e in client_debrief(PILOT, 129.0).events])
    assert track_dcs_grades(recording, approaches, other) == {}


def test_backfill_merges_and_never_duplicates(hub, tmp_path):
    # The agent sent the pass live, without DCS's grade (no comms).
    server = load_recording(SERVER)
    (p,) = find_passes(server)
    hub.ingest(1, SERVER.read_bytes(), sidecar(server, p, "live", slice_objects(server, p)))
    # The server's own Tacview file of the same session, backfilled: same landing, nothing new.
    copy = with_recording_time(SERVER, tmp_path / "server-file.zip.acmi", "2026-10-02T19:39:00Z")
    (r,) = hub.ingest_recording(1, copy)
    assert r["kind"] == "pass" and r["grade"] and len(landings(hub)) == 1
    # The pilot's own recording and debrief.log: merged into the same landing, bringing DCS's wire.
    (r,) = hub.ingest_recording(2, PILOT, client_debrief(PILOT, 129.0))
    assert r["kind"] == "track" and r["grade"]
    (landing,) = landings(hub)
    assert landing["wire"] == 2 and landing["dcs_grade"].startswith("LSO: GRADE:C")
    assert sorted(x["source"] for x in landing["reports"]) == ["Wrycu: pilot", "server1", "server1"]
    # Uploading the same file again changes nothing.
    (again,) = hub.ingest_recording(2, PILOT)
    assert again["created"] is False and len(landings(hub)) == 1


def test_upload_api(hub, tmp_path):
    token = hub.add_source("uploader")  # a server agent token: imports everything
    client = TestClient(create_app(hub))
    assert client.get("/upload").status_code == 200
    assert client.post("/api/v1/recordings", headers={"Authorization": "Bearer wrong"},
                       files={"recording": ("x.acmi", b"x")}).status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/v1/recordings", headers=auth, files={"recording": ("x.txt", b"x")}).status_code == 400

    r = client.post("/api/v1/recordings", headers=auth, files={"recording": (AI_TRAP.name, AI_TRAP.read_bytes())})
    assert r.status_code == 202
    body = r.json()
    deadline = time.monotonic() + 30
    while (status := client.get(body["status_url"]).json())["status"] in ("queued", "processing"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert status["status"] == "done"
    (result,) = status["results"]
    assert result["kind"] == "pass" and result["created"] and result["grade"]
    page = client.get(body["page"]).text
    assert f'href="/passes/{result["pass_id"]}"' in page
    assert not list(hub.uploads_dir.glob("*.acmi"))  # the recording isn't kept, only its slices

    broken = client.post("/api/v1/recordings", headers=auth, files={"recording": ("bad.zip.acmi", io.BytesIO(b"not a zip"))})
    while (status := client.get(broken.json()["status_url"]).json())["status"] in ("queued", "processing"):
        time.sleep(0.05)
    assert status["status"] == "failed" and "could not read" in status["message"]


def test_interrupted_uploads_are_marked_failed(tmp_path):
    db, data = f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub"
    c = Hub(db, data)
    c.add_source("s")
    upload_id, _ = c.add_upload(1, "session.zip.acmi", 123, choose_pilot=False)
    c.upload_path(upload_id, "session.zip.acmi").write_bytes(b"x")
    Hub(db, data)  # restarted before the upload was processed
    status = TestClient(create_app(c)).get(f"/api/v1/recordings/{upload_id}").json()
    assert status["status"] == "failed" and "upload again" in status["message"]
    assert not c.upload_path(upload_id, "session.zip.acmi").exists()



OWN = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"  # recorded on Wrycu's PC: his jet has AOA


def two_pilots(path: Path, out: Path, other: str = "Maverick", other_own: bool = False) -> Path:
    """`path` with every Hornet line copied to a second Hornet flown by `other` along the same path. With
    `other_own`, the copy keeps its AOA (a second own pilot); otherwise it's just another aircraft."""
    import re
    with zipfile.ZipFile(path) as z:
        (name,) = z.namelist()
        text = z.read(name).decode("utf-8-sig")
    ids = {line.split(",", 1)[0] for line in text.splitlines() if "FA-18C_hornet" in line}
    lines = []
    for line in text.splitlines():
        lines.append(line)
        head = line.split(",", 1)[0]
        if head in ids:
            copy = format(int(head, 16) + 0x7000, "x") + line[len(head):]
            copy = re.sub(r",Pilot=[^,]*", f",Pilot={other}", copy)
            if not other_own:
                copy = re.sub(r",AOA=[^,]*", "", copy)
            lines.append(copy)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return out


def wait(client, status_url: str) -> dict:
    deadline = time.monotonic() + 30
    while (status := client.get(status_url).json())["status"] in ("inspecting", "queued", "processing"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return status


def upload(client, path: Path, **headers) -> dict:
    r = client.post("/api/v1/recordings", headers=headers, files={"recording": (path.name, path.read_bytes())})
    assert r.status_code == 202, r.text
    return r.json()


def test_the_own_pilot_is_imported_without_asking(hub):
    client = TestClient(create_app(hub))
    status = wait(client, upload(client, OWN)["status_url"])
    assert status["status"] == "done" and status["pilots"] == ["Wrycu"] and status["pilot"] == "Wrycu"
    (result,) = status["results"]
    assert result["created"] and result["grade"]
    assert landings(hub)[0]["source"] == "uploads"


def test_a_recording_without_an_own_pilot_needs_a_token(hub):
    # Like a dedicated server's recording: nobody flew on the PC that made it (here: AI only).
    client = TestClient(create_app(hub))
    status = wait(client, upload(client, AI_TRAP)["status_url"])
    assert status["status"] == "done" and status["results"] == [] and "token" in status["message"]
    assert landings(hub) == []


def test_other_aircraft_in_the_recording_are_not_imported(hub, tmp_path):
    from burble.slices import own_pilots
    path = two_pilots(OWN, tmp_path / "hosted.zip.acmi")  # Maverick: another player in the host's recording
    assert own_pilots(load_recording(path)) == ["Wrycu"]
    client = TestClient(create_app(hub))
    status = wait(client, upload(client, path)["status_url"])
    assert [r["pilot"] for r in status["results"]] == ["Wrycu"]
    assert [x["pilot"] for x in landings(hub)] == ["Wrycu"]


def test_with_several_own_pilots_the_uploader_picks(hub, tmp_path):
    path = two_pilots(OWN, tmp_path / "session.zip.acmi", other_own=True)
    client = TestClient(create_app(hub))
    body = upload(client, path)
    status = wait(client, body["status_url"])
    assert status["status"] == "choose_pilot" and status["pilots"] == ["Maverick", "Wrycu"]
    # Only the uploader's link offers the choice.
    assert "<select" in client.get(body["page"]).text
    assert "<select" not in client.get(f"/uploads/{body['upload_id']}").text
    choose = body["choose_url"]
    assert client.post(choose, data={"key": "wrong", "pilot": "Maverick"}).status_code == 403
    assert client.post(choose, data={"key": body["key"], "pilot": "Goose"}).status_code == 400
    assert client.post(choose, data={"key": body["key"], "pilot": "Maverick"}).status_code == 202
    status = wait(client, body["status_url"])
    (result,) = status["results"]
    assert status["status"] == "done" and result["pilot"] == "Maverick"
    assert [x["pilot"] for x in landings(hub)] == ["Maverick"]


def test_the_page_form_picks_the_pilot(hub, tmp_path):
    client = TestClient(create_app(hub))
    body = upload(client, two_pilots(OWN, tmp_path / "session.zip.acmi", other_own=True))
    wait(client, body["status_url"])
    r = client.post(f"/uploads/{body['upload_id']}/pilot", data={"key": body["key"], "pilot": "Wrycu"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == body["page"]
    assert wait(client, body["status_url"])["results"][0]["pilot"] == "Wrycu"


def test_a_server_can_require_a_token(tmp_path):
    strict = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub", require_upload_token=True)
    token = strict.add_source("server1")
    client = TestClient(create_app(strict))
    r = client.post("/api/v1/recordings", files={"recording": (AI_TRAP.name, AI_TRAP.read_bytes())})
    assert r.status_code == 401
    assert "Upload token <input" in client.get("/upload").text and "Upload token (optional)" not in client.get("/upload").text
    body = upload(client, AI_TRAP, Authorization=f"Bearer {token}")
    assert wait(client, body["status_url"])["status"] == "done"


def test_an_upload_waits_a_day_for_its_pilot(tmp_path):
    from datetime import UTC, datetime, timedelta

    from burble.hub.db import Upload
    db, data = f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub"
    c = Hub(db, data)
    c.add_source("s")
    fresh, _ = c.add_upload(1, "a.zip.acmi", 1, choose_pilot=True)
    old, _ = c.add_upload(1, "b.zip.acmi", 1, choose_pilot=True)
    with c.sessions.begin() as s:
        for upload_id in (fresh, old):
            s.get(Upload, upload_id).status = "choose_pilot"
        s.get(Upload, old).created_at = datetime.now(UTC) - timedelta(days=2)
    c.upload_path(old, "b.zip.acmi").write_bytes(b"x")
    Hub(db, data)  # restarted
    client = TestClient(create_app(c))
    assert client.get(f"/api/v1/recordings/{fresh}").json()["status"] == "choose_pilot"
    status = client.get(f"/api/v1/recordings/{old}").json()
    assert status["status"] == "failed" and "within a day" in status["message"]
    assert not c.upload_path(old, "b.zip.acmi").exists()


def test_upload_leftovers_are_cleaned_at_startup(tmp_path):
    db, data = f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub"
    c = Hub(db, data)
    c.add_source("s")
    waiting, _ = c.add_upload(1, "a.zip.acmi", 1, choose_pilot=True)
    from burble.hub.db import Upload
    with c.sessions.begin() as s:
        s.get(Upload, waiting).status = "choose_pilot"
    c.upload_path(waiting, "a.zip.acmi").write_bytes(b"x")  # waiting for its pilot: kept
    (c.uploads_dir / "incoming-deadbeef.acmi").write_bytes(b"x")  # a transfer cut off by a crash
    (c.uploads_dir / "999.zip.acmi").write_bytes(b"x")  # belongs to no upload in progress
    (c.uploads_dir / "999.debrief.log").write_bytes(b"x")
    Hub(db, data)
    assert sorted(p.name for p in c.uploads_dir.iterdir()) == [f"{waiting}.zip.acmi"]


NEW_CALLSIGN = FIXTURES / "passes" / "20260928-025423_New_callsign_86s.zip.acmi"  # flown under DCS's default name


def test_dcs_default_pilot_name_is_refused(hub):
    client = TestClient(create_app(hub))
    body = upload(client, NEW_CALLSIGN)
    status = wait(client, body["status_url"])
    assert status["status"] == "failed" and "default pilot name" in status["message"]
    assert "Logbook" in client.get(body["page"]).text  # says how to fix it
    assert landings(hub) == []


def test_default_name_is_never_offered_as_a_pilot(hub, tmp_path):
    # Two own pilots, one of them "New callsign": the real one is imported without asking.
    path = two_pilots(OWN, tmp_path / "session.zip.acmi", other="New callsign", other_own=True)
    client = TestClient(create_app(hub))
    status = wait(client, upload(client, path)["status_url"])
    assert status["status"] == "done" and status["pilot"] == "Wrycu"
    assert [x["pilot"] for x in landings(hub)] == ["Wrycu"]


def test_default_name_passes_are_skipped_in_a_token_upload(hub, tmp_path):
    token = hub.add_source("uploader", kind="server")
    path = two_pilots(OWN, tmp_path / "server.zip.acmi", other="New callsign")
    client = TestClient(create_app(hub))
    status = wait(client, upload(client, path, Authorization=f"Bearer {token}")["status_url"])
    results = {r["pilot"]: r for r in status["results"] if r["kind"] == "pass"}
    assert results["Wrycu"]["created"] and "default pilot name" in results["New callsign"]["error"]
    assert [x["pilot"] for x in landings(hub)] == ["Wrycu"]
