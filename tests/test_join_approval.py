"""Admins approving new pilots (PLAN #44): optional per hub. While waiting, a pilot can sign in and see that they're
waiting, nothing more; refusing frees the name."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from burble.acmi import load_recording
from burble.detect import find_passes
from burble.hub.app import create_app
from burble.hub.service import Hub, SignedIn
from burble.slices import sidecar, slice_objects

FIXTURES = Path(__file__).parent / "fixtures"
PASS = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"


def report(pilot: str) -> tuple[bytes, dict]:
    r = load_recording(PASS)
    (p,) = find_passes(r)
    meta = sidecar(r, p, "x", slice_objects(r, p))
    meta["pass"]["pilot"] = pilot
    return PASS.read_bytes(), meta


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.add_source("server1")
    h.ingest(1, *report("Wrycu"))  # Wrycu is on the board, unclaimed
    h.register_pilot("Boss", "boss password")
    h.set_admin("Boss", True)
    return h


def signed_in(hub, name, password) -> TestClient:
    client = TestClient(create_app(hub))
    r = client.post("/signin", data={"name": name, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def test_off_by_default_and_joining_works_as_before(hub):
    assert not hub.join_approval()
    hub.register_pilot("Goose", "goose password")
    assert not hub.is_pending("Goose")
    assert hub.create_pilot_token("Goose", "goose password")


def test_a_waiting_pilot_signs_in_and_nothing_more(hub):
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    assert hub.is_pending("Goose") and [p.name for p in hub.pending_pilots()] == ["Goose"]
    for attempt in (lambda: hub.create_pilot_token("Goose", "goose password"),
                    lambda: hub.claim_alias("Goose", "goose password", "VF-1 | Goose"),
                    lambda: hub.set_pilot_modex("Goose", SignedIn("Goose"), "305")):
        with pytest.raises(PermissionError, match="hasn't approved"):
            attempt()
    hub.set_pilot_password("Goose", "new goose password", "goose password")  # their own password: yes
    hub.sign_in("Goose", "new goose password")
    client = signed_in(hub, "Goose", "new goose password")
    page = client.get("/pilots/Goose/settings").text
    assert "Waiting for approval" in page and "Create a pilot token" not in page and "Claim a name" not in page
    # Their password doesn't protect uploads of the name yet (nor stop anyone flying under it).
    with hub.sessions.begin() as s:
        upload = SimpleNamespace(id=1, pilot="Goose", status="", message=None)
        assert hub._authorize(s, upload, None) and upload.status == "queued"
    # Approved: everything works.
    hub.approve_pilot("Goose")
    assert not hub.is_pending("Goose") and hub.pending_pilots() == []
    assert hub.create_pilot_token("Goose", "new goose password")
    with hub.sessions.begin() as s:
        upload = SimpleNamespace(id=2, pilot="Goose", status="", message=None)
        assert not hub._authorize(s, upload, None) and upload.status == "needs_password"


def test_claiming_a_name_on_the_board_waits_too(hub):
    hub.set_join_approval(True)
    hub.set_pilot_password("Wrycu", "wrycu password")
    assert hub.is_pending("Wrycu")
    client = TestClient(create_app(hub))
    assert "uploads password-protected" not in client.get("/pilots/Wrycu").text


def test_refusing_frees_the_name(hub):
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    hub.set_pilot_password("Wrycu", "wrycu password")
    assert hub.refuse_pilot("Goose")  # no passes: removed
    assert hub.register_pilot("Goose", "someone else") == "Goose"  # anyone can ask for it again
    assert not hub.refuse_pilot("Wrycu")  # on the board: unclaimed again, passes kept
    assert not hub.is_pending("Wrycu") and [p.name for p in hub.admin_pilots("Wrycu")] == ["Wrycu"]
    assert not hub.admin_pilots("Wrycu")[0].has_password
    with pytest.raises(PermissionError):
        hub.sign_in("Wrycu", "wrycu password")
    with pytest.raises(ValueError, match="isn't waiting"):
        hub.approve_pilot("Boss")


def test_turning_it_off_doesnt_approve_those_waiting(hub):
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    hub.set_join_approval(False)
    assert hub.is_pending("Goose")
    hub.register_pilot("Iceman", "iceman password")
    assert not hub.is_pending("Iceman")


def test_making_a_waiting_pilot_an_admin_approves_them(hub):
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    assert not hub.is_admin("Goose")
    hub.set_admin("Goose", True)
    assert hub.is_admin("Goose") and not hub.is_pending("Goose")


def test_admins_approve_and_refuse_on_the_admin_page(hub):
    admin = signed_in(hub, "Boss", "boss password")
    assert "<strong>Off:</strong>" in admin.get("/admin").text
    r = admin.post("/admin/join-approval", data={"on": "1"}, follow_redirects=False)
    assert r.status_code == 303 and hub.join_approval()
    joiner = TestClient(create_app(hub))
    assert "An admin approves new pilots" in joiner.get("/join").text
    r = joiner.post("/join", data={"name": "Goose", "password": "goose password", "confirm": "goose password"},
                    follow_redirects=False)
    assert r.status_code == 303 and "approve" in r.headers["location"]
    joiner.post("/join", data={"name": "Iceman", "password": "iceman password", "confirm": "iceman password"})
    page = admin.get("/admin").text
    assert '<a href="/admin">Admin (2)</a>' in page
    assert "/admin/pilots/Goose/approve" in page and "/admin/pilots/Iceman/refuse" in page
    assert '<span class="tag">waiting</span>' in page
    assert "Admin (" not in joiner.get("/").text  # only admins see the count
    r = admin.post("/admin/pilots/Goose/approve", follow_redirects=False)
    assert "Goose%20approved" in r.headers["location"] and not hub.is_pending("Goose")
    r = admin.post("/admin/pilots/Iceman/refuse", follow_redirects=False)
    assert "removed" in r.headers["location"] and "Iceman" not in [p.name for p in hub.admin_pilots()]
    assert '<a href="/admin">Admin</a>' in admin.get("/admin").text
    # Not an admin: no.
    pilot = signed_in(hub, "Goose", "goose password")
    assert pilot.post("/admin/join-approval", data={"on": "0"}).status_code == 403 and hub.join_approval()


def test_a_waiting_pilot_cant_watch_live_or_be_given_a_token(hub):
    hub.set_join_approval(True)
    hub.register_pilot("Goose", "goose password")
    client = signed_in(hub, "Goose", "goose password")
    assert '<a href="/live">Live</a>' not in client.get("/").text
    r = client.get("/live", follow_redirects=False)
    assert r.status_code == 403 and "Waiting for approval" in r.text
    assert client.get("/live/server1/CVN-75/events").status_code == 403
    with pytest.raises(ValueError, match="approve them first"):
        hub.add_pilot_token("Goose")
    admin_page = signed_in(hub, "Boss", "boss password").get("/admin").text
    assert "/admin/pilots/Goose/tokens" not in admin_page and "/admin/pilots/Goose/reset-password" not in admin_page
    hub.approve_pilot("Goose")
    assert '<a href="/live">Live</a>' in client.get("/").text and client.get("/live").status_code == 200


# -- deleting pilots ---------------------------------------------------------------------------------------------

def test_deleting_a_pilot_deletes_their_landings(tmp_path):
    from test_merge import pilot_report, server_report

    from burble.hub.db import Grade, Pass, Pilot, PilotAlias, Slice, WebSession

    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.add_source("server1")
    h.add_pilot_token("Wrycu", "pc")
    h.ingest(1, *server_report("trap"))
    h.ingest(2, *pilot_report("trap"))  # merged into the server's report: one landing, two reports
    h.ingest(1, *report("Iceman"))  # someone else's
    h.set_pilot_password("Wrycu", "wrycu password")
    h.claim_alias("Wrycu", "wrycu password", "VF-1 | Wrycu")
    h.start_session("Wrycu")
    with h.sessions() as s:
        wrycu = s.scalar(select(Pilot).where(Pilot.name == "Wrycu"))
        reports = list(s.scalars(select(Pass).where(Pass.pilot_id == wrycu.id)))
        assert len(reports) == 2 and sum(r.merged_into_id is None for r in reports) == 1
        files = [h.store.path(s.get(Slice, r.slice_id).sha256) for r in reports]
    assert all(f.exists() for f in files)
    changed = []
    h.listeners.append(changed.append)

    assert h.delete_pilot("Wrycu") == 1
    with h.sessions() as s:
        assert [p.name for p in s.scalars(select(Pilot))] == ["Iceman"]
        assert s.scalar(select(func.count()).select_from(Pass)) == 1  # Iceman's
        assert s.scalar(select(func.count()).select_from(Grade)) == 1
        assert s.scalar(select(func.count()).select_from(Slice)) == 1
        assert not list(s.scalars(select(PilotAlias))) and not list(s.scalars(select(WebSession)))
    assert not any(f.exists() for f in files) and changed == [None]
    with h.sessions() as s:
        from burble.hub.db import Source
        (token,) = s.scalars(select(Source).where(Source.kind == "pilot", Source.label == "pc"))
        assert token.revoked_at is not None and token.pilot_id is None
    # They can come back: a new pass under the name puts them on the board again.
    h.ingest(1, *server_report("trap"))
    assert "Wrycu" in [p.name for p in h.admin_pilots()]


def test_admins_cant_be_deleted_and_the_admin_page_deletes(hub):
    with pytest.raises(ValueError, match="is an admin"):
        hub.delete_pilot("Boss")
    admin = signed_in(hub, "Boss", "boss password")
    page = admin.get("/admin").text
    assert "/admin/pilots/Wrycu/remove" in page and "/admin/pilots/Boss/remove" not in page
    assert "Delete Wrycu and their 1 landing" in page
    r = admin.post("/admin/pilots/Wrycu/remove", follow_redirects=False)
    assert "Wrycu%20and%201%20landing%20deleted" in r.headers["location"]
    assert "Wrycu" not in [p.name for p in hub.admin_pilots()]
    r = admin.post("/admin/pilots/Boss/remove", follow_redirects=False)
    assert "error=" in r.headers["location"] and hub.is_admin("Boss")


def test_names_with_quotes_dont_break_the_delete_prompt(hub):
    from test_merge import server_report

    data, meta = server_report("bolter")  # another pass than the fixture's
    meta["pass"]["pilot"] = "O'Neil \"Jack\""
    hub.ingest(1, data, meta)
    page = signed_in(hub, "Boss", "boss password").get("/admin").text
    assert "Delete O\\'Neil &quot;Jack&quot; and their 1 landing" in page
