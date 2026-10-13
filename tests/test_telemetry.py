"""Usage reports (PLAN #17): the hub's daily report (counts only), sending it, the admins' notice and switch, and
the receiver that runs on its own (`burble telemetry serve`) with its stats for a hub's Usage page."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from burble.cli import main
from burble.hub.db import Pass
from burble.hub.telemetry import DEFAULT_URLS, REPORT_PATH, Settings, Telemetry, build_report, fetch_usage
from burble.telemetry.app import create_app as receiver_app
from burble.telemetry.store import Store, TooSoon
from test_admins import client_for
from test_pilot_hook import HOME, UCID, carrier_upload, client_at, hook_upload, make_hub, post, server_report


@pytest.fixture
def hub(tmp_path):
    h = make_hub(tmp_path / "hub", pilot_hook_accept="any")
    h.set_config("server1", {"callouts": {"enabled": True}}, kind="server")
    h.register_pilot("Viper", "viper password")
    h.set_admin("Viper", True)
    return h


def landings(hub):
    """A landing the server agent and the pilot hook both reported (with live calls), and one only the pilot hook
    reported, with the carrier (a second pilot, another mission, two days before). Returns the first one's time."""
    data, meta = server_report()
    meta["calls"] = [{"time": 1.0, "along": 900.0, "call": "you're high"}]
    first = hub.ingest(1, data, meta)
    (report,) = post(client_at(hub, HOME), hook_upload(), hub.token).json()["reports"]
    with hub.sessions.begin() as s:
        landing = s.get(Pass, first.pass_id)
        assert report["pass_id"] == first.pass_id  # merged
        when = landing.occurred_at.replace(tzinfo=UTC)
    goose = hub.add_pilot_token("Goose", "pilot hook")
    (other,) = post(client_at(hub, "10.0.0.2"), carrier_upload(pilot="Goose", ucid="b" * 32, mission="Other"),
                    goose).json()["reports"]
    with hub.sessions.begin() as s:
        s.get(Pass, other["pass_id"]).occurred_at = when - timedelta(days=2)
    return when


def test_the_report_is_counts_only(hub):
    when = landings(hub)
    report = build_report(hub, now=when + timedelta(hours=1))
    assert set(report) == {"schema", "install", "version", "days", "pilots_30d", "agents", "pilot_hooks"}
    assert len(report["days"]) == 30 and report["days"][-1]["day"] == when.date().isoformat()
    busy = {d["day"]: d for d in report["days"] if d["landings"]}
    today, earlier = busy[when.date().isoformat()], busy[(when - timedelta(days=2)).date().isoformat()]
    assert today == {"day": today["day"], "landings": 1, "pilots": 1, "server_only": 0, "pilot_only": 0, "both": 1,
                     "live_calls": 1}
    assert (earlier["landings"], earlier["pilot_only"], earlier["live_calls"]) == (1, 1, 0)
    assert report["pilots_30d"] == 2
    assert report["agents"] == {"live_calls": 1, "collect_only": 0, "versions": {report["version"]: 1}}
    assert report["pilot_hooks"] == {"count": 2, "versions": {"1": 2}}  # Wrycu's and Goose's (by token)
    text = json.dumps(report)
    for secret in ("Wrycu", "Goose", UCID, HOME, "server1", "10.0.0.2", "192.168.1.238"):
        assert secret not in text
    assert build_report(hub)["install"] == report["install"]  # made once
    # Older than 30 days: left out.
    assert sum(d["landings"] for d in build_report(hub, now=when + timedelta(days=40))["days"]) == 0


def sender(hub, answers: dict[str, int]):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), json.loads(request.content)))
        code = answers.get(request.url.host, 201)
        if code == 0:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(code, text="nope" if code >= 300 else "")

    return Telemetry(hub, client=httpx.Client(transport=httpx.MockTransport(handler)), start=False), sent


def test_sent_to_each_address_and_off_sends_nothing(hub):
    t, sent = sender(hub, {"down.example": 0, "fussy.example": 400})
    assert t.settings.enabled() and t.settings.urls() == DEFAULT_URLS
    assert t.send() == {DEFAULT_URLS[0]: True}
    assert sent[0][0] == DEFAULT_URLS[0] + REPORT_PATH and sent[0][1]["schema"] == 1
    t.settings.set_urls(["https://down.example/", "https://fussy.example", "https://ours.example"])
    sent.clear()
    assert t.send() == {"https://down.example": False, "https://fussy.example": False, "https://ours.example": True}
    status = t.settings.status()
    assert status["https://ours.example"]["ok"] and "not reached" in status["https://down.example"]["message"]
    assert "refused (400)" in status["https://fussy.example"]["message"]
    t.settings.set_enabled(False)
    sent.clear()
    assert t.send() == {} and sent == []
    with pytest.raises(ValueError):
        t.settings.set_urls(["telemetry.example.com"])
    assert t.settings.set_urls(None) == DEFAULT_URLS


def test_admins_see_the_notice_until_they_choose(hub):
    viper = client_for(hub, "Viper", "viper password")
    page = viper.get("/admin").text
    assert "sends anonymous usage counts to the burble project" in page and "Keep sending" in page
    assert 'id="usage-reports"' in page and 'id="whats-sent"' in page and "What's never sent" in page and "pilot names, UCIDs" in page
    viper.post("/admin/telemetry", data={"on": "1"})
    page = viper.get("/admin").text
    assert "Keep sending" not in page and "<strong>On</strong>" in page and Settings(hub).enabled()
    viper.post("/admin/telemetry", data={"on": "0"})
    assert "<strong>Off</strong>" in viper.get("/admin").text and not Settings(hub).enabled()
    viper.post("/admin/telemetry/urls", data={"urls": "https://a.example\nhttps://b.example/"})
    assert Settings(hub).urls() == ["https://a.example", "https://b.example"]
    viper.post("/admin/telemetry/urls", data={"default": "1"})
    assert Settings(hub).urls() == DEFAULT_URLS


def report(install: str, days: dict[str, int], **extra) -> dict:
    return {"schema": 1, "install": install, "version": "0.1.0",
            "days": [{"day": d, "landings": n, "pilots": 1, "server_only": n, "pilot_only": 0, "both": 0,
                      "live_calls": 0} for d, n in days.items()],
            "pilots_30d": 1, "agents": {"live_calls": 1, "collect_only": 0, "versions": {"0.1.0": 1}},
            "pilot_hooks": {"count": 2, "versions": {"8": 2}}, **extra}


A, B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def test_the_receiver_keeps_each_day_once_and_the_whole_history(tmp_path):
    store = Store(tmp_path)
    t0 = datetime(2026, 10, 12, 12, tzinfo=UTC)
    store.receive(report(A, {"2026-10-10": 3, "2026-10-11": 2}), now=t0)
    with pytest.raises(TooSoon):
        store.receive(report(A, {"2026-10-11": 4}), now=t0 + timedelta(minutes=30))
    # A day later: the same day reported again (a late upload) replaces it, a new day is added.
    store.receive(report(A, {"2026-10-11": 4, "2026-10-12": 1}), now=t0 + timedelta(days=1))
    store.receive(report(B, {"2026-10-12": 5}), now=t0 + timedelta(days=1))
    stats = store.usage(now=t0 + timedelta(days=1, hours=1))
    assert stats["installs"] == {"ever": 2, "last_7d": 2, "last_30d": 2}
    per = {i["install"]: i for i in stats["per_install"]}
    assert per[A]["landings"] == 3 + 4 + 1 and per[B]["landings"] == 5
    assert stats["totals"]["landings"] == 13 and stats["totals"]["tracks"]["server_only"] == 13
    assert stats["versions"]["pilot_hook"] == {"8": 4} and stats["totals"]["agents"]["live_calls"] == 2
    assert sum(w["landings"] for w in stats["weekly"]) == 13
    # Months later nothing new: still counted in the history, not as reporting lately.
    later = store.usage(now=t0 + timedelta(days=90))
    assert later["installs"] == {"ever": 2, "last_7d": 0, "last_30d": 0} and later["totals"]["landings"] == 13


@pytest.fixture
def receiver(tmp_path):
    store = Store(tmp_path / "receiver")
    store.set_password("receiver password")
    return store, TestClient(receiver_app(store))


def test_the_receiver_checks_reports_and_guards_its_stats(receiver):
    store, client = receiver
    assert client.post("/api/v1/telemetry", json=report(A, {"2026-10-12": 1})).status_code == 201
    assert client.post("/api/v1/telemetry", json=report(A, {"2026-10-12": 1})).status_code == 429
    assert client.post("/api/v1/telemetry", json={"schema": 1, "install": "x"}).status_code == 400
    assert client.post("/api/v1/telemetry", content=b"not json").status_code == 400
    bad = report(B, {"2026-10-12": -1})
    assert client.post("/api/v1/telemetry", json=bad).status_code == 400
    assert client.post("/api/v1/telemetry", content=b"{" + b" " * 70_000 + b"}").status_code == 413
    # Unknown fields aren't kept.
    assert client.post("/api/v1/telemetry", json=report(B, {"2026-10-12": 1}, pilot="Wrycu")).status_code == 201
    assert "Wrycu" not in json.dumps(store.usage())

    assert client.get("/api/v1/usage").status_code == 401
    assert client.get("/api/v1/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
    key = store.new_key("lso.wrycu.com")
    assert client.get("/api/v1/usage", headers={"Authorization": f"Bearer {key}"}).json()["installs"]["ever"] == 2
    assert client.get("/api/v1/usage", headers={"Authorization": "Bearer receiver password"}).status_code == 200
    store.revoke_key("lso.wrycu.com")
    assert client.get("/api/v1/usage", headers={"Authorization": f"Bearer {key}"}).status_code == 401

    assert client.get("/").status_code == 401
    basic = base64.b64encode(b"admin:receiver password").decode()
    page = client.get("/", headers={"Authorization": f"Basic {basic}"})
    assert page.status_code == 200 and "Each hub" in page.text and "Landings per week" in page.text


def test_a_hub_shows_a_receivers_stats(hub, receiver, monkeypatch):
    store, client = receiver
    client.post("/api/v1/telemetry", json=report(A, {datetime.now(UTC).date().isoformat(): 7}))
    key = store.new_key("hub")
    viper = client_for(hub, "Viper", "viper password")
    assert "No usage receiver connected" in viper.get("/admin/usage").text
    viper.post("/admin/telemetry/view", data={"url": "http://testserver", "key": key})
    assert 'href="/admin/usage">Usage</a>' in viper.get("/admin").text
    assert fetch_usage(hub, client=client)["totals"]["landings"] == 7
    monkeypatch.setattr("burble.hub.app.fetch_usage", lambda h: fetch_usage(h, client=client))
    page = viper.get("/admin/usage").text
    assert "From <code>http://testserver</code>" in page and "Each hub" in page and "11111111" in page
    Settings(hub).set_view("http://testserver", "wrong key")
    assert "refused the API key or password" in viper.get("/admin/usage").text
    viper.post("/admin/telemetry/view", data={"clear": "1"})
    assert Settings(hub).view() is None
    assert client_for(hub, "Viper", "viper password").get("/admin/usage").status_code == 200
    hub.register_pilot("Goose", "goose password")
    assert client_for(hub, "Goose", "goose password").get("/admin/usage").status_code == 403


def test_cli(hub, tmp_path, capsys):
    data = str(tmp_path / "hub" / "hub")
    db = f"sqlite:///{tmp_path / 'hub' / 'lso.db'}"
    args = ["hub", "--data-dir", data, "--database-url", db, "telemetry"]
    assert main([*args, "off"]) == 0 and not Settings(hub).enabled()
    assert "usage reports: off" in capsys.readouterr().out
    assert main([*args, "urls", "https://ours.example"]) == 0 and Settings(hub).urls() == ["https://ours.example"]
    capsys.readouterr()
    assert main([*args, "show"]) == 0 and json.loads(capsys.readouterr().out)["schema"] == 1
    receiver_dir = str(tmp_path / "receiver")
    assert main(["telemetry", "--data-dir", receiver_dir, "new-key", "hub"]) == 0
    key = capsys.readouterr().out.strip().splitlines()[-1]
    assert Store(receiver_dir).allowed(key)
    assert main(["telemetry", "--data-dir", receiver_dir, "revoke-key", "hub"]) == 0
    assert not Store(receiver_dir).allowed(key)
