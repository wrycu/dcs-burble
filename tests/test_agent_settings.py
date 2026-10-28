"""A settings editor for server agents (PLAN #43): the common settings as a form on /admin, checked before saving (the
raw JSON too), the agent saying which voices it has, and every key documented (`burble hub config-reference`)."""

import asyncio
import json
from dataclasses import fields

import httpx
import pytest
from fastapi.testclient import TestClient

from burble import agentconfig as ac
from burble.agent.callouts import CalloutSettings
from burble.agent.livefeed import DEFAULT_RADIUS_NM, live_settings
from burble.agent.service import RETENTION_DEFAULT_DAYS, Agent, AgentConfig
from burble.callouts.rules import NM, Call, Thresholds
from burble.hub.app import create_app
from burble.hub.service import Hub
from burble.srs import Modulation


@pytest.fixture
def hub(tmp_path):
    h = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")
    h.register_pilot("Viper", "viper password")
    h.register_pilot("Goose", "goose password")
    h.set_admin("Viper", True)
    h.token = h.add_source("server1")
    return h


def client_for(hub, name, password):
    client = TestClient(create_app(hub))
    assert client.post("/signin", data={"name": name, "password": password}, follow_redirects=False).status_code == 303
    return client


def default_form() -> dict[str, list[str]]:
    """The form as the page shows it for an empty configuration (every default, every call)."""
    form = {s.key: ["1"] for s in ac.SETTINGS if s.kind == "bool" and s.default}
    form["call"] = [c.value for c in ac.CHOOSABLE_CALLS]
    return form


# -- the reference matches the agent -------------------------------------------------------------------------------

def test_the_defaults_are_the_agents():
    agent = CalloutSettings()
    for s in ac.SETTINGS:
        if s.path[0] == "callouts":
            value = getattr(agent.srs, s.path[2]) if s.path[1] == "srs" else getattr(agent, s.path[1])
            assert (value.name if isinstance(value, Modulation) else value) == s.default, s.key
    assert live_settings({}) == (True, DEFAULT_RADIUS_NM * NM)
    assert ac.get({}, ("live",)) is None and {s.key: s.default for s in ac.SETTINGS if s.path[0] == "live"} == {
        "live.enabled": True, "live.radius_nm": DEFAULT_RADIUS_NM}
    assert {s.path[1]: s.default for s in ac.SETTINGS if s.path[0] == "retention"} == {
        f"{k}_days": v for k, v in RETENTION_DEFAULT_DAYS.items()}


def test_every_call_and_threshold_is_covered():
    assert set(ac.CHOOSABLE_CALLS) | set(ac.ALWAYS_CALLS) == set(Call)
    assert len(ac.CHOOSABLE_CALLS) == len(set(ac.CHOOSABLE_CALLS))
    # Only the welcome's wire variants and the dig go along unseen (a new call must be put in a group).
    assert set(ac.ALWAYS_CALLS) == {c for c in Call if "wire" in c.name.lower()} | {Call.ROUGH_LANDING}
    shown = {t.name for t in ac.thresholds()}
    assert shown | ac.HIDDEN_THRESHOLDS == {f.name for f in fields(Thresholds)}
    assert all(t.note for t in ac.thresholds())  # a new threshold needs a note
    assert set(ac.THRESHOLD_NOTES) == shown


def test_the_reference_lists_every_key():
    text = ac.reference_markdown()
    for s in ac.SETTINGS:
        assert f"`{s.key}`" in text
    for t in ac.thresholds():
        assert f"`{t.name}`" in text
    assert "`callouts.carriers`" in text and "`you're low`" in text
    assert "| `groove_start_m` | 1389 m (0.75 nm) |" in text


# -- checking ----------------------------------------------------------------------------------------------------

def test_problems_are_what_the_agent_couldnt_read():
    good = {"callouts": {"enabled": True, "srs": {"host": "10.0.0.5", "port": 5002, "coalition": 1},
                         "modulation": "FM", "carriers": {"CVN-75": {"frequency_mhz": 251.0}}, "case": "iii",
                         "calls": ["power", "wave off"], "thresholds": {"spacing_s": 1.5}},
            "live": {"radius_nm": 5}, "retention": {"archives_days": 0}, "something_else": [1, 2]}
    assert ac.problems(good) == [] and ac.problems(None) == []
    CalloutSettings.from_config(good)  # the agent reads it
    for bad, says in (
            ([], "a JSON object"),
            ({"callouts": {"srs": "127.0.0.1:5002"}}, "callouts.srs: should be an object"),
            ({"callouts": {"modulation": "XM"}}, "callouts.modulation"),
            ({"callouts": {"enabled": "yes"}}, "true or false"),
            ({"callouts": {"srs": {"port": 50.5}}}, "a whole number"),
            ({"callouts": {"case": "II"}}, "callouts.case"),
            ({"callouts": {"carriers": {"CVN-75": {"frequency": 127.5}}}}, "frequency_mhz"),
            ({"callouts": {"carriers": {"CVN-75": {"frequency_mhz": 127.5, "modulation": "am"}}}}, "AM or FM"),
            ({"callouts": {"calls": ["powre"]}}, "no such call \"powre\""),
            ({"callouts": {"thresholds": {"spacing": 1}}}, "no threshold 'spacing'"),
            ({"callouts": {"thresholds": {"spacing_s": "1"}}}, "should be a number"),
            ({"retention": {"sent_days": -1}}, "at least 0")):
        found = ac.problems(bad)
        assert found and says in "; ".join(found), (bad, found)


# -- the form ------------------------------------------------------------------------------------------------------

def test_the_form_at_its_defaults_saves_nothing():
    assert ac.apply_form(None, default_form()) is None
    assert ac.apply_form({"other": 1}, default_form()) == {"other": 1}  # keys the form doesn't cover are kept


def test_the_form_puts_in_what_was_changed():
    existing = {"callouts": {"enabled": True, "voice": "clips-amy", "thresholds": {"aoa_fast": 7.0}, "mystery": 1},
                "retention": {"sent_days": 5}}
    form = default_form() | {
        "callouts.enabled": ["1"], "callouts.srs.host": ["10.0.0.5"], "callouts.srs.coalition": ["1"],
        "callouts.frequency_mhz": ["251"], "callouts.modulation": ["FM"], "callouts.voice": [""],
        "callouts.case": ["III"], "callouts.record_radio": ["1"], "retention.sent_days": [""],
        "retention.archives_days": ["60"], "live.radius_nm": ["5.5"],
        "carriers": ["3"], "carrier.0.unit": ["CVN-75 Truman"], "carrier.0.frequency_mhz": ["127.6"],
        "carrier.0.modulation": ["AM"], "carrier.1.unit": [""], "carrier.1.frequency_mhz": ["300"],
        "carrier.2.unit": ["CVN-71"], "carrier.2.frequency_mhz": ["305"], "carrier.2.modulation": ["FM"],
        "call": [c.value for c in ac.CHOOSABLE_CALLS if c is not Call.KEEP_IT_COMING],
        "threshold.spacing_s": ["1.5"], "threshold.repeat_s": ["2.5"], "threshold.keep_coming_max": ["3"],
    }
    del form["live.enabled"]  # unchecked
    config = ac.apply_form(existing, form)
    assert config["callouts"]["srs"] == {"host": "10.0.0.5", "coalition": 1}
    assert config["callouts"]["carriers"] == {"CVN-75 Truman": {"frequency_mhz": 127.6},
                                              "CVN-71": {"frequency_mhz": 305.0, "modulation": "FM"}}
    assert "voice" not in config["callouts"] and config["callouts"]["mystery"] == 1
    # A threshold at its default is left out; one the form doesn't show is kept.
    assert config["callouts"]["thresholds"] == {"aoa_fast": 7.0, "spacing_s": 1.5, "keep_coming_max": 3}
    assert config["retention"] == {"archives_days": 60} and config["live"] == {"enabled": False, "radius_nm": 5.5}
    assert ac.problems(config) == []
    settings = CalloutSettings.from_config(config)
    assert settings.enabled and settings.record_radio and settings.case == "III"
    assert settings.modulation is Modulation.FM and settings.frequency_mhz == 251.0 and settings.srs.coalition == 1
    assert Call.KEEP_IT_COMING not in settings.calls and Call.TRAPPED_WIRE_2 in settings.calls  # with the welcome
    assert settings.thresholds.spacing_s == 1.5
    assert live_settings(config) == (False, 5.5 * NM)


@pytest.mark.parametrize("field,value,says", [
    ("callouts.srs.port", "abc", "isn't a number"),
    ("callouts.srs.port", "50.5", "whole number"),
    ("callouts.frequency_mhz", "0", "at least 1"),
    ("callouts.case", "II", "isn't one of the choices"),
    ("threshold.spacing_s", "soon", "spacing_s"),
    ("carrier.0.frequency_mhz", "x", "frequency isn't a number"),
])
def test_the_form_refuses_what_wont_do(field, value, says):
    form = default_form() | {"carriers": ["1"], "carrier.0.unit": ["CVN-75"], "carrier.0.frequency_mhz": ["127.5"]}
    form[field] = [value]
    with pytest.raises(ValueError, match=says):
        ac.apply_form(None, form)


def test_a_carrier_may_have_just_a_voice():
    form = default_form() | {"carriers": ["2"], "carrier.0.unit": ["CVN-75"], "carrier.0.voice": ["bm_george"],
                             "carrier.0.modulation": ["AM"], "carrier.1.unit": ["CVN-71"],
                             "carrier.1.frequency_mhz": ["305"], "carrier.1.voice": ["am_adam"]}
    config = ac.apply_form(None, form)
    assert config["callouts"]["carriers"] == {"CVN-75": {"voice": "bm_george"},
                                              "CVN-71": {"frequency_mhz": 305.0, "voice": "am_adam"}}
    assert ac.problems(config) == []
    settings = CalloutSettings.from_config(config)
    assert settings.carriers == {"CVN-71": (305.0, Modulation.AM)}
    assert settings.carrier_voices == {"CVN-75": "bm_george", "CVN-71": "am_adam"}
    with pytest.raises(ValueError, match="a frequency or a voice"):
        ac.apply_form(None, form | {"carrier.0.voice": [""]})
    assert ac.problems({"callouts": {"carriers": {"CVN-75": {}}}}) == [
        "callouts.carriers.CVN-75: should have a frequency_mhz (MHz) or a voice"]
    assert ac.problems({"callouts": {"carriers": {"CVN-75": {"voice": 3}}}}) == [
        "callouts.carriers.CVN-75.voice: should be a clip set's name (it's 3)"]


# -- on the hub --------------------------------------------------------------------------------------------------

def test_only_admins_edit_settings(hub):
    visitor = TestClient(create_app(hub))
    r = visitor.get("/admin/agents/server1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin")
    goose = client_for(hub, "Goose", "goose password")
    assert goose.get("/admin/agents/server1").status_code == 403
    assert goose.post("/admin/agents/server1/settings", data={"callouts.enabled": "1"}).status_code == 403
    assert hub.server_agent("server1").config is None
    viper = client_for(hub, "Viper", "viper password")
    assert viper.get("/admin/agents/nobody").status_code == 404
    assert 'href="/admin/agents/server1"' in viper.get("/admin").text


def test_editing_on_the_page(hub):
    hub.set_config("server1", {"callouts": {"carriers": {"CVN-75": {"frequency_mhz": 127.6}}}, "mystery": True})
    viper = client_for(hub, "Viper", "viper password")
    page = viper.get("/admin/agents/server1").text
    assert 'name="carrier.0.unit" value="CVN-75"' in page and "&quot;mystery&quot;: true" in page
    assert "hasn't said which voices it has yet" in page and '<input name="callouts.voice"' in page
    assert page.count('name="call"') == len(ac.CHOOSABLE_CALLS) and "0 changed" in page
    form = {"callouts.enabled": "1", "live.enabled": "1", "callouts.record_radio": "1", "lights.enabled": "1",
            "callouts.srs.host": "10.0.0.5", "carriers": "3",
            "carrier.0.unit": "CVN-75", "carrier.0.frequency_mhz": "127.6", "carrier.0.modulation": "AM",
            "call": [c.value for c in ac.CHOOSABLE_CALLS], "threshold.ball_wait_s": "4"}
    r = viper.post("/admin/agents/server1/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/agents/server1?done=")
    assert hub.server_agent("server1").config == {
        "callouts": {"enabled": True, "srs": {"host": "10.0.0.5"}, "carriers": {"CVN-75": {"frequency_mhz": 127.6}},
                     "thresholds": {"ball_wait_s": 4}}, "mystery": True}
    page = viper.get("/admin/agents/server1").text
    assert "1 changed" in page, page[page.find("Call thresholds"):][:300]
    assert 'value="4"' in page and "details open" in page
    # A mistake: nothing saved, and the page comes back with what was typed.
    r = viper.post("/admin/agents/server1/settings", data=form | {"callouts.srs.port": "lots"})
    assert r.status_code == 400 and "isn&#x27;t a number" in r.text and 'value="lots"' in r.text
    assert "port" not in hub.server_agent("server1").config["callouts"]["srs"]


def test_the_raw_json_is_checked_too(hub):
    viper = client_for(hub, "Viper", "viper password")
    r = viper.post("/admin/agents/server1/config", data={"config": json.dumps({"callouts": {"modulation": "XM"}})})
    assert r.status_code == 400 and "callouts.modulation" in r.text and "XM" in r.text
    assert hub.server_agent("server1").config is None
    r = viper.post("/admin/agents/server1/config", data={"config": json.dumps({"callouts": {"listen": True}})},
                   follow_redirects=False)
    assert r.status_code == 303 and hub.server_agent("server1").config == {"callouts": {"listen": True}}
    with pytest.raises(ValueError, match="no such call"):
        hub.set_config("server1", {"callouts": {"calls": ["nope"]}})


def test_the_agent_says_which_voices_it_has(hub, tmp_path):
    voices = tmp_path / "voices"
    for name in ("clips-ryan", "clips-amy"):
        (voices / name).mkdir(parents=True)
        (voices / name / "manifest.json").write_text("{}")
    hub.set_config("server1", {"callouts": {"listen": True, "voice": "clips-joe"}})
    app = create_app(hub)

    async def run(config):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub",
                                     headers={"Authorization": f"Bearer {hub.token}"}) as client:
            agent = Agent(config, client=client)
            await agent.refresh_config()
            return agent, await agent.report_info()  # already told: not again

    agent, again = asyncio.run(run(AgentConfig(work_dir=tmp_path / "edge", voice_dir=voices)))
    assert not again
    info = hub.server_agent("server1").agent_info
    assert info["voices"] == ["clips-amy", "clips-ryan"] and not info["listen_model"] and not info["one_voice"]
    page = client_for(hub, "Viper", "viper password").get("/admin/agents/server1").text
    assert '<select name="callouts.voice">' in page and '<option value="clips-amy">clips-amy</option>' in page
    assert "clips-joe (not on the agent)" in page and "wasn't started with --listen-model" in page
    assert '<option value="each carrier">A different one for each carrier</option>' in page
    assert '<select name="carrier.0.voice" aria-label="Voice"><option value="" selected>The LSO&#x27;s voice' in page
    asyncio.run(run(AgentConfig(work_dir=tmp_path / "edge", voice_dir=voices / "clips-ryan")))
    assert hub.server_agent("server1").agent_info["voices"] == ["clips-ryan"]
    assert "one clip set (clips-ryan)" in client_for(hub, "Viper", "viper password").get("/admin/agents/server1").text
    # A pilot token can't.
    pilot_token = hub.add_pilot_token("Goose", "PC")
    r = TestClient(app).post("/api/v1/agent", json={"voices": []}, headers={"Authorization": f"Bearer {pilot_token}"})
    assert r.status_code == 403
