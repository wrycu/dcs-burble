"""The mission's fog, old (`enable_fog`) and DCS 2.9.10's (`fog2`), for Case III and the pass page."""

from burble.acmi import ObjectTrack
from burble.agent.callouts import recovery_case
from burble.hub.pages import weather_text
from burble.weather import fog

# The hook's weather event for the mission of 2026-11-08 (keys as Lua sent them), but with clear skies.
FOG2 = {"clouds": {}, "visibility_m": 80000,
        "fog2": {"mode": 4, "keys": {"1": {"time": 0, "visibility_m": 3657.6, "thickness_m": 690}}}}


def test_fog2_in_manual_mode():
    assert fog(FOG2) == {"visibility_m": 3657.6, "thickness_m": 690}
    two = {"fog2": {"mode": 4, "keys": [{"time": 0, "visibility_m": 8000, "thickness_m": 300},
                                        {"time": 3600, "visibility_m": 2000, "thickness_m": 500}]}}
    assert fog(two)["visibility_m"] == 2000  # the thickest key
    assert fog({"fog2": {"mode": 2, "keys": FOG2["fog2"]["keys"]}}) is None  # other modes aren't read
    assert fog({"fog2": {"mode": 4, "keys": {}}}) is None
    assert fog({"fog": {"visibility_m": 1500}}) == {"visibility_m": 1500}  # the older fog first
    assert fog(None) is None


def test_fog2_makes_it_case_iii_and_shows_on_the_pass_page():
    carrier = ObjectTrack(1)
    assert recovery_case("auto", lambda c, t: False, lambda: FOG2, carrier, 0.0) == (True, "fog 2.0 nm")
    assert weather_text(FOG2) == "clear skies, fog (visibility 2.0 nm), visibility 43 nm"
