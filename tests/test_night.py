"""Day or night passes: the sun's elevation at the carrier, and the night dot on the greenie board."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.central.app import create_app
from dcs_lso.central.service import Central
from dcs_lso.detect import find_passes
from dcs_lso.slices import sidecar, slice_objects
from dcs_lso.sun import is_night, sun_elevation

FIXTURES = Path(__file__).parent / "fixtures"
DAY = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"  # Syria, 08:00 local start (05:00Z)


def test_sun_elevation():
    # London at the June solstice: about 62 deg at noon, below the horizon at midnight.
    assert sun_elevation(51.5, 0.0, datetime(2016, 6, 21, 12, tzinfo=UTC)) == pytest.approx(62.0, abs=0.5)
    assert sun_elevation(51.5, 0.0, datetime(2016, 6, 21, 0, tzinfo=UTC)) < -10
    # Longitude matters: noon in UTC is about sunset 90 deg east... and well before sunrise 135 deg west.
    assert sun_elevation(0.0, 90.0, datetime(2016, 3, 20, 12, tzinfo=UTC)) < 2
    assert sun_elevation(0.0, -135.0, datetime(2016, 3, 20, 12, tzinfo=UTC)) < -40


def at_reference_time(path: Path, out: Path, reference: str) -> Path:
    with zipfile.ZipFile(path) as z:
        (name,) = z.namelist()
        text = z.read(name).decode("utf-8-sig")
    assert "ReferenceTime=2016-06-21T05:00:00Z" in text
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text.replace("ReferenceTime=2016-06-21T05:00:00Z", f"ReferenceTime={reference}"))
    return out


def test_pass_is_day_or_night(tmp_path):
    for path, night in ((DAY, False), (at_reference_time(DAY, tmp_path / "night.zip.acmi", "2016-06-21T18:00:00Z"), True)):
        r = load_recording(path)
        (p,) = find_passes(r)
        assert is_night(r, p.carrier_id, p.end_time) is night  # 18:00Z + ~1 h: about 22:00 in Syria


def test_board_marks_night_passes(tmp_path):
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    central.add_source("server1")
    central.add_source("server2")  # the same pass twice would be one pass from one source
    night = at_reference_time(DAY, tmp_path / "night.zip.acmi", "2016-06-21T18:00:00Z")
    for source_id, path in ((1, DAY), (2, night)):
        r = load_recording(path)
        (p,) = find_passes(r)
        meta = sidecar(r, p, "x", slice_objects(r, p))
        meta["pass"]["pilot"] = "Night Owl" if path == night else "Day Bird"
        central.ingest(source_id, path.read_bytes(), meta)
    board = TestClient(create_app(central)).get("/", params={"days": 0}).text
    assert board.count(' night" href=') == 1 and "Night pass" in board
    assert "· night ·" in board  # in the cell's tooltip
    client = TestClient(create_app(central))
    cards = {pilot: client.get(f"/passes/{i}/card.svg").text
             for i, pilot in ((1, "Day Bird"), (2, "Night Owl"))}
    assert ">Night<tspan" in cards["Night Owl"] and ">Night<tspan" not in cards["Day Bird"]


def test_existing_passes_are_backfilled(tmp_path):
    url, data = f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central"
    central = Central(url, data)
    central.add_source("server1")
    night = at_reference_time(DAY, tmp_path / "night.zip.acmi", "2016-06-21T18:00:00Z")
    r = load_recording(night)
    (p,) = find_passes(r)
    pass_id = central.ingest(1, night.read_bytes(), sidecar(r, p, "x", slice_objects(r, p))).pass_id
    from dcs_lso.central.db import Pass
    with central.sessions.begin() as s:
        s.get(Pass, pass_id).night = None  # as stored before day/night was recorded
    with Central(url, data).sessions() as s:  # a restart works it out
        assert s.get(Pass, pass_id).night is True
