import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.central.app import create_app
from dcs_lso.central.service import Central
from dcs_lso.cli import main
from dcs_lso.detect import find_passes
from dcs_lso.grading import grade_pass

FIXTURES = Path(__file__).parent / "fixtures"
PASS_FILES = sorted((FIXTURES / "passes").glob("*.zip.acmi"))


@pytest.fixture
def central(tmp_path):
    return Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")


@pytest.fixture
def client(central):
    return TestClient(create_app(central))


@pytest.fixture
def token(central):
    return central.add_source("test-server")


def upload(client, token, acmi: Path, sidecar: dict | None = None, rename_default: bool = True):
    sidecar = sidecar or json.loads(acmi.with_suffix("").with_suffix(".json").read_text())
    if rename_default and sidecar.get("pass", {}).get("pilot") == "New callsign":
        sidecar["pass"]["pilot"] = "Maverick"  # DCS's default name is refused (see below)
    return client.post("/api/v1/passes", headers={"Authorization": f"Bearer {token}"},
                       files={"slice": (acmi.name, acmi.read_bytes(), "application/zip")},
                       data={"sidecar": json.dumps(sidecar)})


def test_upload_requires_valid_token(client):
    acmi = PASS_FILES[0]
    assert upload(client, "nope", acmi).status_code == 401
    r = client.post("/api/v1/passes", files={"slice": ("x", b"x")}, data={"sidecar": "{}"})
    assert r.status_code == 401


def test_upload_grades_and_is_idempotent(client, token):
    for acmi in PASS_FILES:
        r = upload(client, token, acmi)
        assert r.status_code == 201, r.text
        (expected,) = [grade_pass(p) for p in find_passes(load_recording(acmi))]
        assert r.json()["text"] == expected.text
    again = upload(client, token, PASS_FILES[0])
    assert again.status_code == 200 and again.json()["created"] is False
    listed = client.get("/api/v1/passes", params={"days": 0}).json()
    assert len(listed) == len(PASS_FILES)
    assert sorted(p["outcome"] for p in listed) == ["bolter", "bolter", "trap", "trap", "trap"]


def test_bad_sidecar_is_rejected(client, token):
    r = upload(client, token, PASS_FILES[0], {"pass": {}})
    assert r.status_code == 400


def test_default_pilot_name_is_refused(client, token):
    (acmi,) = [f for f in PASS_FILES if "New_callsign" in f.name]
    r = upload(client, token, acmi, rename_default=False)
    assert r.status_code == 400 and "default pilot name" in r.json()["detail"]
    assert client.get("/api/v1/passes", params={"days": 0}).json() == []


def test_board_and_pass_pages(client, token):
    for acmi in PASS_FILES:
        upload(client, token, acmi)
    board = client.get("/", params={"days": 0}).text
    assert "Greenie Board" in board and "Wrycu" in board and "Maverick" in board
    assert board.count('href="/passes/') == len(PASS_FILES)
    # "---" is a real grade (No Grade); the board must not show it as a bare "---".
    assert ">NG</a>" in board and "No Grade" in board and ">---</a>" not in board
    only = client.get("/", params={"days": 0, "pilot": "Maverick"}).text
    assert only.count('href="/passes/') == 1

    pass_id = upload(client, token, PASS_FILES[-1]).json()["pass_id"]
    page = client.get(f"/passes/{pass_id}").text
    assert "<svg" in page and "clipPath" in page and "Download ACMI" in page
    acmi = client.get(f"/passes/{pass_id}/acmi")
    assert acmi.status_code == 200 and acmi.content == PASS_FILES[-1].read_bytes()
    assert client.get("/passes/9999").status_code == 404


def test_dcs_grade_from_sidecar_is_shown(client, token, tmp_path):
    from dcs_lso.dcslog import attach_dcs_grades, load_debrief
    from dcs_lso.slices import write_pass_slice

    source = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
    recording = load_recording(source)
    passes = list(find_passes(recording))
    attach_dcs_grades(passes, recording, load_debrief(FIXTURES / "ai_hornet_trap_cvn75.debrief.log"))
    acmi, _ = write_pass_slice(source, recording, passes[0], tmp_path)
    pass_id = upload(client, token, acmi).json()["pass_id"]
    (row,) = client.get("/api/v1/passes", params={"days": 0}).json()
    assert row["wire"] == 3 and row["dcs_grade"] == "LSO: GRADE:C : LNFIW  WIRE# 3" and row["grade"] == "C"
    assert "LNFIW  WIRE# 3" in client.get(f"/passes/{pass_id}").text


def test_regrade_adds_new_version_and_keeps_slices(central, client, token, monkeypatch):
    for acmi in PASS_FILES:
        upload(client, token, acmi)
    slices = sorted((central.store.root).rglob("*.zip.acmi"))
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in slices]
    assert central.regrade() == (0, len(PASS_FILES))
    monkeypatch.setattr("dcs_lso.grading.grade.GRADING_VERSION", "test-2")
    monkeypatch.setattr("dcs_lso.central.service.GRADING_VERSION", "test-2")
    assert central.regrade() == (len(PASS_FILES), 0)
    rows = client.get("/api/v1/passes", params={"days": 0}).json()
    assert {r["grading_version"] for r in rows} == {"test-2"}
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in slices] == before


def test_upload_command(central, client, token, monkeypatch, capsys):
    app = client.app

    def fake_client(*, base_url, headers, timeout):
        return TestClient(app, base_url=base_url, headers=headers)

    monkeypatch.setattr(httpx, "Client", fake_client)
    source = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
    # The sibling ai_hornet_trap_cvn75.debrief.log is picked up automatically.
    assert main(["upload", str(source), "--url", "http://testserver", f"--token={token}"]) == 0
    assert "(new)" in capsys.readouterr().out
    assert main(["upload", str(source), "--url", "http://testserver", f"--token={token}"]) == 0
    assert "(already uploaded)" in capsys.readouterr().out
    (row,) = client.get("/api/v1/passes", params={"days": 0}).json()
    assert row["wire"] == 3
