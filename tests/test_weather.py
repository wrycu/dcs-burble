"""The wind and weather a pass was flown in: from the server hook, onto the pass, the trap card and the pass page."""

import math
from pathlib import Path

from fastapi.testclient import TestClient

from burble.agent.service import HookFeed
from burble.cards.svg import KT, wind_text
from burble.geometry import DeckWind, WindProfile
from burble.hub.app import create_app
from burble.hub.pages import weather_text
from burble.hub.service import Hub
from burble.slices import slice_recording

FIXTURE = Path(__file__).parent / "fixtures" / "live" / "tomcat-trap-server.zip.acmi"


def test_wind_over_the_angled_deck():
    # Steaming north at 15 m/s into a 10 m/s northerly: 25 m/s straight down the axial deck, which is 9 degrees
    # starboard of the angled deck (it points to port).
    north = WindProfile(((10.0, 0.0, -10.0),), turbulence=1.5)
    w = DeckWind.at(north, (0.0, 15.0), heading=0.0, deck_angle=9.0)
    assert math.isclose(w.speed, 25.0) and math.isclose(w.off_axis, 9.0)
    assert math.isclose(w.crosswind, 25.0 * math.sin(math.radians(9.0)))
    assert math.isclose(w.wind_speed, 10.0) and math.isclose(w.wind_from, 0.0, abs_tol=1e-9)
    # Wind from the port bow (north-west, heading north): turned onto the angled deck.
    nw = WindProfile(((10.0, 10.0 * math.sin(math.radians(45)), -10.0 * math.cos(math.radians(45))),))
    w = DeckWind.at(nw, (0.0, 10.0), heading=0.0, deck_angle=9.0)
    assert w.off_axis < 0 and math.isclose(w.wind_from, 315.0)
    assert WindProfile.from_dict(north.to_dict()) == north


def _hooks(tmp_path) -> HookFeed:
    log_file = tmp_path / "dcs.log"
    prefix = "2026-10-07 20:{:02d}:00.000 INFO    BURBLE (Main): BURBLE "
    log_file.write_text("\n".join([
        prefix.format(0) + '{"event":"handler_installed","t":0}',
        prefix.format(0) + '{"event":"weather","t":0,"dynamic":false,"ground_turbulence":12,"temperature":26,'
                           '"qnh_mmhg":755,"visibility_m":80000,"clouds":{"preset":"Preset6","base_m":2500,'
                           '"thickness_m":1150,"density":5,"precipitation":0}}',
        # The carrier's unit name in this recording is "CVN-75 Harry S. Truman"; 8 m/s from the south-west.
        prefix.format(1) + '{"event":"wind","t":60,"carrier":"CVN-75 Harry S. Truman","type":"CVN_75",'
                           '"levels":[{"alt":10,"east":5.657,"north":5.657},{"alt":50,"east":6,"north":6}],'
                           '"turbulence":1.2}',
    ]) + "\n")
    return HookFeed(log_file, follow=False)


def test_backfill_carries_the_wind_and_weather_to_the_card(tmp_path):
    [(acmi, meta)] = slice_recording(FIXTURE, tmp_path / "out", hooks=_hooks(tmp_path))
    assert meta["wind"]["turbulence"] == 1.2 and meta["weather"]["clouds"]["base_m"] == 2500
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "data")
    source = hub.authenticate(hub.add_source("server1"))
    pass_id = hub.ingest(source.id, acmi.read_bytes(), meta).pass_id
    page = TestClient(create_app(hub)).get(f"/passes/{pass_id}").text
    assert "Wind over deck" in page and "wind 16 kt from 225°" in page and "turbulence ±2 kt" in page
    assert "clouds from 8,202 ft, visibility 43 nm, 26 °C, QNH 29.72 inHg" in page


def test_wind_text_without_wind_or_turbulence():
    from burble.detect.passes import Outcome, PassResult
    p = PassResult(1, "CVN_75", 2, "FA-18C_hornet", "Goose", Outcome.TRAP, 0.0, 1.0)
    assert wind_text(p) == "Wind not recorded (no burble server hook)"
    p.deck_wind = DeckWind(25 * KT, 0.4, 0.0, 0.0, None)
    assert wind_text(p) == "Wind over deck 25 kt, straight down the angled deck · winds calm"
    p.deck_wind = DeckWind(25 * KT, -12.0, 10 * KT, 300.0, 0.1)
    assert wind_text(p) == ("Wind over deck 25 kt, 12° port of the angled deck (5 kt across) · wind 10 kt from 300° "
                            "· smooth air, no turbulence")
    assert weather_text({"clouds": {}, "fog": {"visibility_m": 926}}) == "clear skies, fog (visibility 0.5 nm)"
