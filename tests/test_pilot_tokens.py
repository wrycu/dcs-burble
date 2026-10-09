"""Pilot tokens (tied to one pilot), aliases (other in-game names), and the pilot uploader sending only its
own jet."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from burble.acmi import load_recording
from burble.acmi.stream import serve_recording
from burble.agent.service import Agent, AgentConfig
from burble.detect import find_passes
from burble.hub.app import create_app
from burble.hub.service import Hub, IngestError
from burble.slices import sidecar, slice_objects
from test_backfill import two_pilots

FIXTURES = Path(__file__).parent / "fixtures"
OWN = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"  # Wrycu's own recording (AOA), with the carrier
OTHER = FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi"


def report(path: Path, pilot: str) -> tuple[bytes, dict]:
    r = load_recording(path)
    (p,) = find_passes(r)
    meta = sidecar(r, p, "x", slice_objects(r, p))
    meta["pass"]["pilot"] = pilot
    return path.read_bytes(), meta


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.add_source("server1")  # source 1: a server agent
    h.ingest(1, *report(OTHER, "Wrycu"))  # puts Wrycu on the board
    return h


def board_pilots(hub) -> dict[str, int]:
    rows = TestClient(create_app(hub)).get("/api/v1/passes", params={"days": 0}).json()
    out: dict[str, int] = {}
    for r in rows:
        out[r["pilot"]] = out.get(r["pilot"], 0) + 1
    return out


def test_creating_a_pilot_token_needs_the_password(hub):
    with pytest.raises(PermissionError):
        hub.create_pilot_token("Wrycu", "anything")  # no password set yet
    hub.set_pilot_password("Wrycu", "first password")
    with pytest.raises(PermissionError):
        hub.create_pilot_token("Wrycu", "wrong")
    token = hub.create_pilot_token("Wrycu", "first password", "my PC")
    source = hub.authenticate(token)
    assert source is not None and source.kind == "pilot" and source.label == "my PC"
    assert [t.label for t in hub.pilot_tokens("Wrycu")] == ["my PC"]


def test_uploads_with_a_pilot_token_are_the_pilots_whatever_the_name(hub):
    token = hub.add_pilot_token("Wrycu", "my PC")
    source_id = hub.authenticate(token).id
    hub.ingest(source_id, *report(OWN, "CVW-17 | Wrycu"))
    assert board_pilots(hub) == {"Wrycu": 2}
    assert [(a.name, a.claimed) for a in hub.pilot_aliases("Wrycu")] == [("CVW-17 | Wrycu", False)]
    # A server agent's report under that name now counts as Wrycu's too.
    hub.add_source("server2")
    hub.ingest(3, *report(OWN, "CVW-17 | Wrycu"))
    assert set(board_pilots(hub)) == {"Wrycu"}
    # DCS's default name is fine with a token (e.g. single player): the token says who it is.
    hub.ingest(source_id, *report(OTHER, "New callsign"))
    assert "New callsign" not in {a.name for a in hub.pilot_aliases("Wrycu")}


def test_a_token_never_takes_another_pilots_name(hub):
    hub.ingest(1, *report(OWN, "Maverick"))  # Maverick: a pilot on the board
    token = hub.add_pilot_token("Wrycu")
    hub.ingest(hub.authenticate(token).id, *report(OTHER, "Maverick"))
    assert hub.pilot_aliases("Wrycu") == []  # not taken over without a claim
    assert board_pilots(hub)["Maverick"] == 1


def test_revoked_and_unbound_tokens_are_refused(hub):
    hub.set_pilot_password("Wrycu", "first password")
    token = hub.create_pilot_token("Wrycu", "first password")
    (t,) = hub.pilot_tokens("Wrycu")
    with pytest.raises(PermissionError):
        hub.revoke_pilot_token("Wrycu", "wrong", t.id)
    hub.revoke_pilot_token("Wrycu", "first password", t.id)
    assert hub.authenticate(token) is None
    # An old-style pilot token that isn't tied to a pilot.
    old = hub.add_source("someone's PC", kind="pilot")
    with pytest.raises(IngestError, match="isn't tied to a pilot"):
        hub.ingest(hub.authenticate(old).id, *report(OWN, "Wrycu"))


def test_claiming_a_name(hub):
    hub.ingest(1, *report(OWN, "CVW-17 | Wrycu"))  # a server report under another of Wrycu's names
    hub.set_pilot_password("Wrycu", "first password")
    with pytest.raises(PermissionError):
        hub.claim_alias("Wrycu", "wrong", "CVW-17 | Wrycu")
    assert hub.claim_alias("Wrycu", "first password", "CVW-17 | Wrycu") == 1
    assert board_pilots(hub) == {"Wrycu": 2}
    # Taken names can't be claimed: another pilot's alias, or a pilot with a password.
    hub.add_source("server2")  # (the same pass from server1 again would be a duplicate)
    hub.ingest(2, *report(OWN, "Maverick"))
    hub.set_pilot_password("Maverick", "maverick password")
    with pytest.raises(PermissionError):
        hub.claim_alias("Maverick", "maverick password", "CVW-17 | Wrycu")
    with pytest.raises(PermissionError):
        hub.claim_alias("Wrycu", "first password", "Maverick")
    with pytest.raises(ValueError):
        hub.claim_alias("Wrycu", "first password", "New callsign")
    # An admin undoes a claim: the passes reported under it go back to a pilot of that name.
    assert hub.remove_alias("CVW-17 | Wrycu") == 1
    assert board_pilots(hub) == {"Wrycu": 1, "CVW-17 | Wrycu": 1, "Maverick": 1}


def test_settings_page_tokens_and_names(hub):
    hub.set_pilot_password("Wrycu", "first password")
    client = TestClient(create_app(hub))
    page = client.get("/pilots/Wrycu/settings").text
    assert "Sign in as Wrycu" in page and "Create a pilot token" not in page  # not signed in
    r = client.post("/pilots/Wrycu/settings/tokens", data={"label": "x"})
    assert r.status_code == 403 and "<code>" not in r.text
    r = client.post("/signin", data={"name": "Wrycu", "password": "wrong"})
    assert r.status_code == 403 and "don&#x27;t match" in r.text
    r = client.post("/signin", data={"name": "Wrycu", "password": "first password",
                                     "next": "/pilots/Wrycu/settings"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pilots/Wrycu/settings"
    page = client.get("/pilots/Wrycu/settings").text
    assert "Create a pilot token" in page and "Claim name" in page and 'name="password"' not in page
    r = client.post("/pilots/Wrycu/settings/tokens", data={"label": "my PC"})  # signed in: no password
    assert r.status_code == 200 and "copy it now" in r.text
    token = r.text.split("<code>")[1].split("</code>")[0]
    assert hub.authenticate(token) is not None
    page = client.get("/pilots/Wrycu/settings").text
    assert "my PC" in page and token not in page  # listed, never shown again
    (t,) = hub.pilot_tokens("Wrycu")
    r = client.post(f"/pilots/Wrycu/settings/tokens/{t.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and hub.authenticate(token) is None
    r = client.post("/pilots/Wrycu/settings/aliases", data={"alias": "Wrycu 2"}, follow_redirects=False)
    assert "now%20one%20of%20your%20names" in r.headers["location"]
    assert "Wrycu 2" in client.get("/pilots/Wrycu/settings").text
    # The API, for tools that set up the pilot hook: with the password, no sign-in.
    api = TestClient(create_app(hub))
    r = api.post("/api/v1/pilots/Wrycu/tokens", data={"password": "first password", "label": "hook"})
    assert r.status_code == 201 and hub.authenticate(r.json()["token"]) is not None
    assert api.post("/api/v1/pilots/Wrycu/tokens", data={"label": "hook"}).status_code == 403


def test_pilot_uploader_sends_only_its_own_jet(tmp_path):
    # Maverick's jet is in Wrycu's recording (another player), at the rate the server sees it.
    path = two_pilots(OWN, tmp_path / "session.zip.acmi")

    async def run():
        server = await serve_recording(path, port=0, speed=0)
        async with server:
            agent = Agent(AgentConfig(work_dir=tmp_path / "agent", tacview_port=server.sockets[0].getsockname()[1],
                                      mode="pilot"))
            await agent.run_session()
        return agent

    agent = asyncio.run(run())
    assert {item.meta()["pass"]["pilot"] for item in agent.outbox.pending()} == {"Wrycu"}


def test_joining_the_board_before_flying_here(hub):
    client = TestClient(create_app(hub))
    assert "Join this board" in client.get("/").text and "<form" in client.get("/join").text
    r = client.post("/join", data={"name": " Goose ", "password": "goose password", "confirm": "goose password"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pilots/Goose/settings?done=")
    assert client.get("/pilots/Goose").status_code == 200  # a pilot page, with no passes yet
    token = hub.create_pilot_token("Goose", "goose password", "my PC")  # tokens straight away
    hub.ingest(hub.authenticate(token).id, *report(OWN, "Goose"))
    assert board_pilots(hub)["Goose"] == 1
    # Taken, unclaimed and invalid names.
    r = client.post("/join", data={"name": "Goose", "password": "another one", "confirm": "another one"})
    assert r.status_code == 400 and "already taken" in r.text
    r = client.post("/join", data={"name": "Wrycu", "password": "first password", "confirm": "first password"})
    assert r.status_code == 409 and "/pilots/Wrycu/settings" in r.text  # on the board: set the password there
    for name, password, confirm in (("New callsign", "long enough", "long enough"), ("Iceman", "short", "short"),
                                    ("Iceman", "long enough", "different!!")):
        assert client.post("/join", data={"name": name, "password": password, "confirm": confirm}).status_code == 400
    hub.add_pilot_token("Wrycu")
    hub.ingest(hub.authenticate(hub.add_pilot_token("Wrycu")).id, *report(OTHER, "Wrycu (2)"))  # an alias of Wrycu's
    assert client.post("/api/v1/pilots", data={"name": "Wrycu (2)", "password": "long enough"}).status_code == 409
    assert client.post("/api/v1/pilots", data={"name": "Iceman", "password": "long enough"}).json() == {"pilot": "Iceman"}


def test_board_can_show_pilots_with_no_passes(hub):
    hub.register_pilot("Goose", "goose password")  # joined, not flown here yet
    client = TestClient(create_app(hub))
    board = client.get("/", params={"days": 0}).text
    assert "Wrycu" in board and ">Goose</a>" not in board and "Show pilots with no passes" in board
    board = client.get("/", params={"days": 0, "pilot": "", "source": "", "empty": "1"}).text  # as the form sends it
    assert ">Goose</a>" in board and 'href="/pilots/Goose"' in board and 'name="empty" value="1" checked' in board
    only = client.get("/", params={"days": 0, "empty": 1, "pilot": "Goose"}).text
    assert ">Goose</a>" in only and ">Wrycu</a>" not in only


def test_removing_a_pilot(hub):
    hub.register_pilot("Junk", "junk password")
    token = hub.create_pilot_token("Junk", "junk password")
    hub.remove_pilot("Junk")
    assert hub.authenticate(token) is None  # its tokens stop working
    with pytest.raises(LookupError):
        hub.pilot_tokens("Junk")
    with pytest.raises(ValueError, match="has 1 passes"):
        hub.remove_pilot("Wrycu")  # has a landing: refused
    hub.register_pilot("Junk", "junk password 2")  # the name is free again


def test_the_hub_command_refuses_an_empty_data_folder(tmp_path, capsys):
    from burble.cli import main
    with pytest.raises(SystemExit, match="no hub in"):
        main(["hub", "--data-dir", str(tmp_path / "typo"), "regrade"])
    assert not (tmp_path / "typo").exists()
    assert main(["hub", "--data-dir", str(tmp_path / "new"), "--create", "regrade"]) == 0
    assert main(["hub", "--data-dir", str(tmp_path / "new"), "remove-pilot", "Nobody"]) == 1
