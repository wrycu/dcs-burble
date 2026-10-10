"""Pilots with the admin role manage the hub on the website (/admin), signed in."""

import json

import pytest
from fastapi.testclient import TestClient

from burble.cli import main
from burble.hub.app import create_app
from burble.hub.service import Hub


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")
    h.register_pilot("Viper", "viper password")
    h.register_pilot("Goose", "goose password")
    h.set_admin("Viper", True)
    return h


def client_for(hub, name, password):
    client = TestClient(create_app(hub))
    assert client.post("/signin", data={"name": name, "password": password}, follow_redirects=False).status_code == 303
    return client


def test_only_admins_see_and_use_the_admin_page(hub):
    viper = client_for(hub, "Viper", "viper password")
    goose = client_for(hub, "Goose", "goose password")
    assert '<a href="/admin">Admin</a>' in viper.get("/").text
    assert '<a href="/admin">Admin</a>' not in goose.get("/").text
    assert viper.get("/admin").status_code == 200
    page = goose.get("/admin")
    assert page.status_code == 403 and "Admins only" in page.text and "signed in as <strong>Goose" in page.text
    assert goose.post("/admin/agents", data={"name": "rogue"}).status_code == 403
    assert TestClient(create_app(hub)).get("/admin", follow_redirects=False).headers["location"].startswith("/signin")
    assert hub.server_agents() == []


def test_an_admin_makes_another_admin_and_cant_drop_their_own_role(hub):
    viper = client_for(hub, "Viper", "viper password")
    r = viper.post("/admin/pilots/Goose/admin", data={"admin": "1"}, follow_redirects=False)
    assert "done=" in r.headers["location"] and hub.is_admin("Goose")
    r = viper.post("/admin/pilots/Viper/admin", data={"admin": "0"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and hub.is_admin("Viper")
    goose = client_for(hub, "Goose", "goose password")
    goose.post("/admin/pilots/Viper/admin", data={"admin": "0"})
    assert not hub.is_admin("Viper")
    assert viper.get("/admin").status_code == 403  # checked on every request


def test_only_a_pilot_with_a_password_can_be_admin_and_a_reset_drops_it(hub):
    with hub.sessions.begin() as s:
        hub._resolve_pilot(s, "Iceman")  # on the board from DCS, no password
    with pytest.raises(ValueError, match="no password"):
        hub.set_admin("Iceman", True)
    hub.set_admin("Goose", True)
    viper = client_for(hub, "Viper", "viper password")
    viper.post("/admin/pilots/Goose/reset-password")
    # Otherwise whoever set Goose's password next would be an admin.
    assert not hub.is_admin("Goose")
    hub.set_pilot_password("Goose", "someone else")
    assert not hub.is_admin("Goose")


def test_admin_adds_an_agent_and_sets_its_config(hub):
    viper = client_for(hub, "Viper", "viper password")
    page = viper.post("/admin/agents", data={"name": "server1"}).text
    token = page.split('class="token-once"')[1].split("<code>")[1].split("</code>")[0]
    assert hub.authenticate(token).name == "server1"
    assert viper.post("/admin/agents", data={"name": "server1"}).status_code == 400  # already there
    config = {"callouts": {"srs": "127.0.0.1:5002"}}
    viper.post("/admin/agents/server1/config", data={"config": json.dumps(config)})
    assert hub.server_agents()[0].config == config
    r = viper.post("/admin/agents/server1/config", data={"config": "{nope"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and hub.server_agents()[0].config == config


def test_admin_removes_pilots_aliases_and_creates_pilot_tokens(hub):
    hub.claim_alias("Goose", "goose password", "VF-1 | Goose")
    hub.register_pilot("Junk", "junk password")
    viper = client_for(hub, "Viper", "viper password")
    page = viper.get("/admin", params={"q": "vf-1"}).text
    assert ">Goose</a>" in page and ">Junk</a>" not in page
    viper.post("/admin/aliases/remove", data={"alias": "VF-1 | Goose"})
    assert hub.pilot_aliases("Goose") == []
    viper.post("/admin/pilots/Junk/remove")
    assert all(p.name != "Junk" for p in hub.admin_pilots())
    r = viper.post("/admin/pilots/Viper/remove", follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = viper.post("/admin/pilots/Goose/tokens", data={"label": "PC"}).text
    token = page.split('class="token-once"')[1].split("<code>")[1].split("</code>")[0]
    assert hub.authenticate(token).pilot_id is not None


def test_cli_set_admin(hub, tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'lso.db'}"
    args = ["hub", "--data-dir", str(tmp_path / "data"), "--database-url", db, "set-admin"]
    assert main([*args, "Goose"]) == 0 and hub.is_admin("Goose")
    assert main([*args, "Goose", "--remove"]) == 0 and not hub.is_admin("Goose")
    assert main([*args, "Nobody"]) == 1


CSS = """/* the board's colours */
:root { --bg: #101820; --card: #18242f; --grade-ok: #00c853; --font: "Roboto Condensed"; }
@media (prefers-color-scheme: dark) { :root { --card: #0a0f14; } }
@media (prefers-color-scheme: light) { :root { --card: #ffffff; } }
table.board td.pilot { text-transform: uppercase; }
"""


def test_admins_set_custom_css_for_every_page(hub):
    viper = client_for(hub, "Viper", "viper password")
    goose = client_for(hub, "Goose", "goose password")
    assert goose.post("/admin/appearance", data={"css": "body { color: red }"}).status_code == 403
    assert hub.custom_css() == ""
    r = viper.post("/admin/appearance", data={"css": CSS + "</style><script>alert(1)</script>"},
                   follow_redirects=False)
    assert "done=" in r.headers["location"]
    page = goose.get("/").text  # every page, whoever's looking
    assert "text-transform: uppercase" in page and "--grade-ok: #00c853" in page
    assert "<script>" not in page and "\\3c /style>" in page  # can't end the <style> early
    assert "var(--grade-ok, " in page  # the board's grade colours follow the variables
    assert "--grade-ok: #00c853" in viper.get("/admin").text  # shown back to edit
    r = viper.post("/admin/appearance", data={"css": "x" * 20_001}, follow_redirects=False)
    assert "error=" in r.headers["location"] and "too%20long" in r.headers["location"]
    assert "text-transform" in hub.custom_css()  # kept
    viper.post("/admin/appearance", data={"css": ""})
    assert hub.custom_css() == "" and "<style></style>" not in goose.get("/").text


def test_theme_variables_for_the_discord_images():
    from burble.cards.theme import Theme, theme_from_css
    t = theme_from_css(CSS)
    assert (t.bg, t.panel, t.text) == ("#101820", "#0a0f14", Theme().text)  # dark mode wins; light-only ignored
    assert t.grades == {"OK": "#00c853"} and t.font.startswith('"Roboto Condensed", ')
    bad = theme_from_css(':root { --bg: url(x.png); --text: "><svg onload=1>; --grade-cut: rgb(255, 0, 0); }')
    assert (bad.bg, bad.text, bad.grades) == (Theme().bg, Theme().text, {"C": "rgb(255, 0, 0)"})
    assert theme_from_css(None) == Theme() and theme_from_css("not css {{{") == Theme()
