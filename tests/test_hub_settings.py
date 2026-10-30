"""The hub settings admins change on /admin (hub/settings.py): kept in the database, in effect when saved."""

import httpx
import pytest
from fastapi.testclient import TestClient

from burble.hub.app import create_app
from burble.hub.discord import Discord
from burble.hub.service import Hub


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    h.register_pilot("Boss", "boss password")
    h.set_admin("Boss", True)
    return h


def signed_in(hub, name="Boss", password="boss password") -> TestClient:
    client = TestClient(create_app(hub))
    assert client.post("/signin", data={"name": name, "password": password}, follow_redirects=False).status_code == 303
    return client


def test_defaults_until_saved_then_kept(hub, tmp_path):
    assert hub.pilot_hook_accept == "ours" and not hub.require_upload_token and hub.other_servers == "shown"
    assert hub.set_settings({"pilot_hook_accept": "any", "require_upload_token": "on",
                             "public_url": " https://lso.example.com/ "}) == ["pilot_hook_accept",
                                                                              "require_upload_token", "public_url"]
    assert hub.set_settings({"pilot_hook_accept": "any"}) == []  # unchanged
    again = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")  # a restart, or a command on the machine
    assert again.pilot_hook_accept == "any" and again.require_upload_token
    assert again.setting("public_url") == "https://lso.example.com"


@pytest.mark.parametrize("values,says", [
    ({"pilot_hook_accept": "everyone"}, "should be one of"),
    ({"discord_board_webhook": "discord.com/api/webhooks/1"}, "starting https://"),
    ({"nope": "1"}, "no hub setting"),
])
def test_a_setting_that_wont_do_saves_nothing(hub, values, says):
    with pytest.raises(ValueError, match=says):
        hub.set_settings({"other_servers": "hidden", **values})
    assert hub.other_servers == "shown"


def test_admins_change_them_on_the_admin_page(hub):
    client = signed_in(hub)
    page = client.get("/admin").text
    assert "Hub settings" in page and 'name="pilot_hook_accept"' in page and 'name="discord_board_webhook"' in page
    r = client.post("/admin/settings", data={"pilot_hook_accept": "any", "require_upload_token": "on",
                                             "other_servers": "shown", "public_url": "", "default_voice": "",
                                             "discord_board_webhook": "", "discord_traps_webhook": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and "Hub%20settings%20saved" in r.headers["location"]
    assert hub.pilot_hook_accept == "any" and hub.require_upload_token
    assert '<option value="any" selected>' in client.get("/admin").text
    r = client.post("/admin/settings", data={"public_url": "lso.example.com"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and hub.setting("public_url") == ""
    hub.register_pilot("Pilot", "pilot password")
    assert signed_in(hub, "Pilot", "pilot password").post("/admin/settings", data={"pilot_hook_accept": "ours"},
                                                          follow_redirects=False).status_code == 403
    assert hub.pilot_hook_accept == "any"


def test_discord_picks_up_a_webhook_once_its_set(hub):
    sent = []
    discord = Discord(hub, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: sent.append(str(r.url)) or httpx.Response(200, json={"id": "1"}))), start=False)
    assert not discord.enabled
    discord.step(None, now=1000.0)
    assert sent == []
    hub.set_settings({"discord_board_webhook": "https://discord.example/api/webhooks/1/x",
                      "public_url": "https://lso.example.com"})
    assert discord.enabled and discord.public_url == "https://lso.example.com"
    discord.step(None, now=2000.0)
    assert sent and sent[0].startswith("https://discord.example/api/webhooks/1/x")
