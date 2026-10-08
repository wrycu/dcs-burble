"""Signing in on the website: a session (cookie) stands for the pilot's password, except for changing it."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dcs_lso.hub.app import SESSION_COOKIE, create_app
from dcs_lso.hub.db import WebSession
from dcs_lso.hub.service import Hub, SignedIn


@pytest.fixture
def hub(tmp_path):
    return Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")


def signed_in(hub, name="Goose", password="goose password"):
    client = TestClient(create_app(hub))
    r = client.post("/signin", data={"name": name, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def test_joining_signs_you_in_and_signing_out_ends_it(hub):
    client = TestClient(create_app(hub))
    assert ">Sign in</a>" in client.get("/").text
    r = client.post("/join", data={"name": "Goose", "password": "goose password", "confirm": "goose password"},
                    follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    board = client.get("/").text
    assert "Signed in as" in board and 'href="/pilots/Goose/settings"' in board
    assert client.post("/pilots/Goose/settings/modex", data={"modex": "305"}, follow_redirects=False).status_code == 303
    client.post("/signout")
    assert ">Sign in</a>" in client.get("/").text
    r = client.post("/pilots/Goose/settings/modex", data={"modex": "306"}, follow_redirects=False)
    assert "error=" in r.headers["location"]  # no longer signed in


def test_signed_in_as_one_pilot_isnt_another(hub):
    hub.register_pilot("Goose", "goose password")
    hub.register_pilot("Iceman", "iceman password")
    client = signed_in(hub)
    page = client.get("/pilots/Iceman/settings").text
    assert "Sign in as Iceman" in page and "signed in as Goose" in page
    r = client.post("/pilots/Iceman/settings/tokens", data={"label": "x"})
    assert r.status_code == 403 and "<code>" not in r.text
    with pytest.raises(PermissionError):
        hub.create_pilot_token("Iceman", SignedIn("Goose"))


def test_sign_in_with_another_name_and_no_name_probing(hub):
    hub.register_pilot("Goose", "goose password")
    hub.claim_alias("Goose", "goose password", "VF-1 | Goose")
    assert hub.sign_in("VF-1 | Goose", "goose password")[0] == "Goose"
    # A wrong password, and no such pilot: the same answer, so names can't be probed.
    for name, password in (("Goose", "wrong!!!"), ("Nobody", "whatever1")):
        with pytest.raises(PermissionError, match="don't match"):
            hub.sign_in(name, password)


def test_changing_the_password_signs_out_everywhere_else(hub):
    hub.register_pilot("Goose", "goose password")
    here, there = signed_in(hub), signed_in(hub)
    r = here.post("/pilots/Goose/settings/password", data={"new": "new password", "confirm": "new password"},
                  follow_redirects=False)
    assert "error=" in r.headers["location"]  # the current password is still needed
    r = here.post("/pilots/Goose/settings/password",
                  data={"current": "goose password", "new": "new password", "confirm": "new password"},
                  follow_redirects=False)
    assert "Password%20saved" in r.headers["location"]
    assert "Signed in as" in here.get("/").text and "Signed in as" not in there.get("/").text
    hub.reset_pilot_password("Goose")  # the admin's reset ends the rest
    assert "Signed in as" not in here.get("/").text


def test_setting_the_first_password_signs_you_in(hub):
    hub.add_pilot_token("Maverick")  # on the board, no password yet
    client = TestClient(create_app(hub))
    assert "Set a password" in client.get("/pilots/Maverick/settings").text
    client.post("/pilots/Maverick/settings/password", data={"new": "maverick pw", "confirm": "maverick pw"})
    assert "Create a pilot token" in client.get("/pilots/Maverick/settings").text


def test_sessions_expire_and_cross_site_forms_are_refused(hub):
    hub.register_pilot("Goose", "goose password")
    client = signed_in(hub)
    r = client.post("/pilots/Goose/settings/modex", data={"modex": "305"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/pilots/Goose/settings/modex", data={"modex": "305"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/pilots/Goose/settings/modex", data={"modex": "305"}, headers={"sec-fetch-site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    with hub.sessions.begin() as s:
        for row in s.query(WebSession):
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert "Signed in as" not in client.get("/").text


def test_signin_goes_back_only_to_this_site(hub):
    hub.register_pilot("Goose", "goose password")
    client = TestClient(create_app(hub))
    for target, expected in (("/upload", "/upload"), ("//evil.example/", "/pilots/Goose"),
                             ("https://evil.example/", "/pilots/Goose")):
        r = client.post("/signin", data={"name": "Goose", "password": "goose password", "next": target},
                        follow_redirects=False)
        assert r.headers["location"] == expected


def test_signed_in_uploads_need_no_password(hub):
    """A token-less upload of a protected pilot's passes: their sign-in stands for the password."""
    from types import SimpleNamespace

    hub.register_pilot("Goose", "goose password")
    with hub.sessions.begin() as s:
        mine = SimpleNamespace(id=1, pilot="Goose", status="", message=None)
        assert hub._authorize(s, mine, SignedIn("Goose")) and mine.status == "queued"
        theirs = SimpleNamespace(id=2, pilot="Goose", status="", message=None)
        assert not hub._authorize(s, theirs, SignedIn("Iceman")) and theirs.status == "needs_password"


def test_names_sign_in_in_any_case_and_show_as_registered(hub):
    assert hub.register_pilot("GooseMav", "goose password") == "GooseMav"
    hub.claim_alias("GooseMav", "goose password", "VF-1 | Goose")
    for name in ("goosemav", "GOOSEMAV", " gOOseMaV ", "vf-1 | GOOSE"):
        assert hub.sign_in(name, "goose password")[0] == "GooseMav"
    client = signed_in(hub, "goosemav")
    assert "Signed in as" in client.get("/").text and 'href="/pilots/GooseMav/settings"' in client.get("/").text
    with pytest.raises(PermissionError, match="don't match"):
        hub.sign_in("goosemav", "wrong password")


def test_a_name_in_another_case_cant_be_registered_or_claimed(hub):
    hub.register_pilot("Goose", "goose password")
    for name in ("goose", "GOOSE"):
        with pytest.raises(PermissionError, match="already taken"):
            hub.register_pilot(name, "other password")
    hub.claim_alias("Goose", "goose password", "VF-1 | Goose")
    with pytest.raises(PermissionError, match="already taken"):
        hub.register_pilot("vf-1 | goose", "other password")
    hub.register_pilot("Iceman", "iceman password")
    with pytest.raises(PermissionError, match="another pilot"):
        hub.claim_alias("Iceman", "iceman password", "GOOSE")
    # A pilot only on the board (from DCS, no password) in another case can't take a password either.
    with hub.sessions.begin() as s:
        hub._resolve_pilot(s, "goose")
    with pytest.raises(PermissionError, match="already been claimed"):
        hub.set_pilot_password("goose", "a new password")


def test_joining_as_an_unclaimed_pilot_in_another_case_finds_them(hub):
    with hub.sessions.begin() as s:
        hub._resolve_pilot(s, "Maverick")  # flew here, never joined
    with pytest.raises(FileExistsError, match="Maverick"):
        hub.register_pilot("maverick", "maverick password")
