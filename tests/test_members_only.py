"""A members-only board (PLAN #45): optional per hub. The board, passes, pilots' pages, and the read API need an
approved pilot signed in (the API also takes a token); signing in, joining, uploads, and Discord work as before."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from burble.acmi import load_recording
from burble.detect import find_passes
from burble.hub.app import create_app
from burble.hub.db import Upload
from burble.hub.service import Hub
from burble.slices import sidecar, slice_objects

FIXTURES = Path(__file__).parent / "fixtures"
PASS = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.agent_token = h.add_source("server1")
    r = load_recording(PASS)
    (p,) = find_passes(r)
    h.ingest(1, PASS.read_bytes(), sidecar(r, p, "x", slice_objects(r, p)))
    h.register_pilot("Boss", "boss password")
    h.set_admin("Boss", True)
    return h


def signed_in(hub, name, password) -> TestClient:
    client = TestClient(create_app(hub))
    assert client.post("/signin", data={"name": name, "password": password},
                       follow_redirects=False).status_code == 303
    return client


PAGES = ["/", "/passes/1", "/passes/1/card.svg", "/passes/1/replay", "/passes/1/acmi", "/pilots/Wrycu",
         "/pilots/Wrycu/overlay.svg"]
API = ["/api/v1/passes", "/api/v1/pilots/Wrycu/trends"]


def test_open_by_default(hub):
    client = TestClient(create_app(hub))
    for path in PAGES + API:
        assert client.get(path, follow_redirects=False).status_code == 200, path


def test_members_only_needs_a_signed_in_approved_pilot(hub):
    hub.set_members_only(True)
    visitor = TestClient(create_app(hub))
    for path in PAGES:
        r = visitor.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/signin?next="), path
    assert visitor.get("/?days=7", follow_redirects=False).headers["location"] == "/signin?next=%2F%3Fdays%3D7"
    for path in API:
        assert visitor.get(path).status_code == 401, path
    # Still open: signing in (saying why), joining, uploading, a pilot's settings (to claim their name).
    assert "This board is for its members" in visitor.get("/signin").text
    for path in ("/join", "/upload", "/pilots/Wrycu/settings", "/healthz", "/static/live.js"):
        assert visitor.get(path, follow_redirects=False).status_code == 200, path
    member = signed_in(hub, "Boss", "boss password")
    for path in PAGES + API:
        assert member.get(path, follow_redirects=False).status_code == 200, path


def test_the_api_takes_a_token(hub):
    hub.set_members_only(True)
    client = TestClient(create_app(hub))
    assert client.get("/api/v1/passes", headers={"Authorization": f"Bearer {hub.agent_token}"}).status_code == 200
    assert client.get("/api/v1/passes", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    # A token doesn't open the website's pages.
    r = client.get("/", headers={"Authorization": f"Bearer {hub.agent_token}"}, follow_redirects=False)
    assert r.status_code == 303


def test_a_pilot_waiting_for_approval_doesnt_see_it(hub):
    hub.set_members_only(True)
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    waiting = signed_in(hub, "Goose", "goose password")
    r = waiting.get("/", follow_redirects=False)
    assert r.status_code == 403 and "Waiting for approval" in r.text
    assert waiting.get("/api/v1/passes").status_code == 401
    hub.approve_pilot("Goose")
    assert waiting.get("/").status_code == 200


def test_upload_results_need_membership_or_the_uploaders_key(hub):
    with hub.sessions.begin() as s:
        s.add(Upload(id=7, source_id=1, filename="mine.acmi", size=1, status="done", key="secret",
                     results=[{"pilot": "Wrycu", "pass_id": 1, "grade": "C", "text": "C : LOX"}]))
    hub.set_members_only(True)
    visitor = TestClient(create_app(hub))
    assert visitor.get("/uploads/7", follow_redirects=False).status_code == 303
    assert visitor.get("/api/v1/recordings/7").status_code == 401
    assert "C : LOX" in visitor.get("/uploads/7", params={"key": "secret"}).text  # the uploader
    assert visitor.get("/api/v1/recordings/7", params={"key": "secret"}).json()["results"]
    assert visitor.get("/uploads/7", params={"key": "wrong"}, follow_redirects=False).status_code == 303
    assert "C : LOX" in signed_in(hub, "Boss", "boss password").get("/uploads/7").text


def test_admins_turn_it_on_and_off(hub):
    admin = signed_in(hub, "Boss", "boss password")
    page = admin.get("/admin").text
    assert "<strong>Open:</strong>" in page and "Make it members-only" in page
    admin.post("/admin/members-only", data={"on": "1"})
    assert hub.members_only()
    page = admin.get("/admin").text
    assert "<strong>Members only:</strong>" in page and "turn on approving new pilots" in page
    hub.set_join_approval(True)
    assert "turn on approving new pilots" not in admin.get("/admin").text
    admin.post("/admin/members-only", data={"on": "0"})
    assert not hub.members_only()
    assert signed_in(hub, "Boss", "boss password").post("/admin/members-only", data={"on": "1"}).status_code == 200
    hub.register_pilot("Goose", "goose password")
    assert signed_in(hub, "Goose", "goose password").post("/admin/members-only", data={"on": "1"}).status_code == 403
