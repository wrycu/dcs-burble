"""Per-pilot upload passwords, and changing the side number with one."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.central.app import create_app
from dcs_lso.central.passwords import FailureLimiter, hash_password, verify_password
from dcs_lso.central.service import Central
from dcs_lso.detect import find_passes
from dcs_lso.slices import sidecar, slice_objects

FIXTURES = Path(__file__).parent / "fixtures"
OWN = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"  # Wrycu's own recording (his jet has AOA)
OTHER = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"


def test_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("correct horse", None) and not verify_password("", stored)
    assert not verify_password("correct horse", "garbage")
    assert hash_password("correct horse") != stored  # salted


def test_failure_limiter():
    limiter = FailureLimiter(limit=3, window_s=60)
    for _ in range(3):
        assert not limiter.blocked("Wrycu", now=0)
        limiter.failed("Wrycu", now=0)
    assert limiter.blocked("Wrycu", now=1) and not limiter.blocked("Maverick", now=1)
    assert not limiter.blocked("Wrycu", now=61)  # the window passed


@pytest.fixture
def central(tmp_path):
    c = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    c.add_source("server1")
    r = load_recording(OTHER)  # puts Wrycu on the board
    (p,) = find_passes(r)
    c.ingest(1, OTHER.read_bytes(), sidecar(r, p, "x", slice_objects(r, p)))
    return c


def test_setting_and_changing_a_password(central):
    with pytest.raises(LookupError):
        central.set_pilot_password("Nobody", "long enough")
    with pytest.raises(ValueError):
        central.set_pilot_password("Wrycu", "short")
    central.set_pilot_password("Wrycu", "first password")  # claims the name
    with pytest.raises(PermissionError):
        central.set_pilot_password("Wrycu", "hijacked!!")  # changing needs the current one
    with pytest.raises(PermissionError):
        central.set_pilot_password("Wrycu", "hijacked!!", current="wrong guess")
    central.set_pilot_password("Wrycu", "second password", current="first password")
    central.reset_pilot_password("Wrycu")  # admin
    central.set_pilot_password("Wrycu", "third password")


def test_changing_the_side_number_needs_the_password(central):
    with pytest.raises(PermissionError):
        central.set_pilot_modex("Wrycu", "anything", "305")  # no password set yet
    central.set_pilot_password("Wrycu", "first password")
    with pytest.raises(PermissionError):
        central.set_pilot_modex("Wrycu", "wrong", "305")
    with pytest.raises(ValueError):
        central.set_pilot_modex("Wrycu", "first password", "3o5")
    central.set_pilot_modex("Wrycu", "first password", "305")
    client = TestClient(create_app(central))
    assert client.get("/api/v1/pilots/Wrycu/trends").json()["modex"] == "305"


def wait(client, status_url):
    deadline = time.monotonic() + 30
    while (status := client.get(status_url).json())["status"] in ("inspecting", "queued", "processing"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return status


def upload(client, path, **form):
    r = client.post("/api/v1/recordings", data=form, files={"recording": (path.name, path.read_bytes())})
    assert r.status_code == 202, r.text
    return r.json()


def test_upload_of_a_protected_pilot_needs_the_password(central):
    central.set_pilot_password("Wrycu", "first password")
    client = TestClient(create_app(central))
    body = upload(client, OWN)
    status = wait(client, body["status_url"])
    assert status["status"] == "needs_password" and status["pilot"] == "Wrycu"
    page = client.get(body["page"]).text
    assert 'action="/uploads/' in page and "has set a password" in page
    assert "has set a password" not in client.get(f"/uploads/{body['upload_id']}").text  # only the uploader's link
    url = f"/api/v1/recordings/{body['upload_id']}/password"
    assert client.post(url, data={"key": "wrong", "password": "first password"}).status_code == 403
    assert client.post(url, data={"key": body["key"], "password": "nope"}).status_code == 202
    assert wait(client, body["status_url"])["message"] == "That password isn't right."
    assert client.post(url, data={"key": body["key"], "password": "first password"}).status_code == 202
    status = wait(client, body["status_url"])
    assert status["status"] == "done" and status["results"][0]["pilot"] == "Wrycu"


def test_password_given_with_the_upload(central):
    central.set_pilot_password("Wrycu", "first password")
    client = TestClient(create_app(central))
    status = wait(client, upload(client, OWN, password="first password")["status_url"])
    assert status["status"] == "done" and status["results"]


def test_too_many_wrong_passwords_end_the_upload(central):
    central.set_pilot_password("Wrycu", "first password")
    client = TestClient(create_app(central))
    body = upload(client, OWN)
    wait(client, body["status_url"])
    url = f"/api/v1/recordings/{body['upload_id']}/password"
    for _ in range(5):
        client.post(url, data={"key": body["key"], "password": "guess"})
    status = client.get(body["status_url"]).json()
    assert status["status"] == "failed" and "too many" in status["message"]


def test_unprotected_pilots_upload_as_before(central):
    client = TestClient(create_app(central))
    assert wait(client, upload(client, OWN)["status_url"])["status"] == "done"


def test_settings_page(central):
    client = TestClient(create_app(central))
    page = client.get("/pilots/Wrycu").text
    assert "no upload password" in page and 'href="/pilots/Wrycu/settings"' in page
    assert "Set a password" in client.get("/pilots/Wrycu/settings").text
    r = client.post("/pilots/Wrycu/settings/password", data={"new": "first password", "confirm": "first passwor"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    r = client.post("/pilots/Wrycu/settings/password", data={"new": "first password", "confirm": "first password"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("done=Password%20saved.")
    assert "Change password" in client.get("/pilots/Wrycu/settings").text
    assert "uploads password-protected" in client.get("/pilots/Wrycu").text
    r = client.post("/pilots/Wrycu/settings/modex", data={"password": "first password", "modex": "305"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("done=Side%20number%20saved.")
    r = client.post("/pilots/Wrycu/settings/modex", data={"password": "wrong", "modex": "306"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert client.get("/pilots/Nobody/settings").status_code == 404
