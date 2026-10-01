import json
import zipfile
from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.acmi.writer import escape, slice_lines
from dcs_lso.detect import find_passes
from dcs_lso.slices import slice_objects, write_pass_slice

FIXTURES = Path(__file__).parent / "fixtures"
PASSES = sorted((FIXTURES / "passes").glob("*.json"))

SOURCE = """FileType=text/acmi/tacview
FileVersion=2.2
0,ReferenceLongitude=35
0,ReferenceLatitude=37
0,Comments=a\\, b\\
second line
0,AuthenticationKey=abc|def
#1
101,T=1|2|0|||10|100|200|11,Name=CVN_75
201,T=1.1|2.1|500|0|1|90|300|400|91,Name=FA-18C_hornet,Pilot=Me
301,T=5|5|9000,Name=Tu-95MS
#2
201,T=|2.2|450
301,T=5.1|5|9000
0,Event=Message|301|hello
#3
201,AOA=8.1
#4
201,T=|2.3|400
"""


def test_slice_lines_snapshot_then_deltas():
    out = list(slice_lines(SOURCE.splitlines(True), start=1.5, end=3.5, object_ids={0x101, 0x201}))
    assert out[:2] == ["FileType=text/acmi/tacview", "FileVersion=2.2"]
    assert "0,Comments=a\\, b\\\nsecond line" in out  # multi-line value re-escaped intact
    assert not any("AuthenticationKey" in line for line in out)
    assert not any(line.startswith("301") or "Event=" in line for line in out)
    i = out.index("#2")
    # Full state of each included object as of the slice start (with the reference removed again)...
    assert out[i + 1] == "101,T=1|2|0|||10|100|200|11,Name=CVN_75"
    assert out[i + 2] == "201,T=1.1|2.1|500|0|1|90|300|400|91,Name=FA-18C_hornet,Pilot=Me"
    # ...then the original lines from that frame on, and nothing past the end.
    assert out[i + 3:] == ["201,T=|2.2|450", "#3", "201,AOA=8.1"]


def test_escape():
    assert escape("a,b\\c\nd") == "a\\,b\\\\c\\\nd"


def test_ai_trap_slice_round_trips_exactly(tmp_path):
    source = FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"
    recording = load_recording(source)
    (original,) = find_passes(recording)
    acmi, meta = write_pass_slice(source, recording, original, tmp_path)
    assert zipfile.is_zipfile(acmi)
    (again,) = find_passes(load_recording(acmi))
    assert (again.outcome, again.start_time, again.end_time) == (original.outcome, original.start_time, original.end_time)
    assert again.samples == original.samples
    sidecar = json.loads(meta.read_text())
    assert sidecar["pass"]["carrier_unit"] == "Naval-1-1"
    assert set(sidecar["objects"]) == slice_objects(recording, original) == {0x101, 0x201}


@pytest.mark.parametrize("meta", PASSES, ids=lambda p: p.stem)
def test_pass_fixtures(meta):
    expected = json.loads(meta.read_text())["pass"]
    passes = list(find_passes(load_recording(meta.with_suffix("").with_suffix(".zip.acmi"))))
    (p,) = [p for p in passes if p.aircraft_id == expected["aircraft_id"]]
    assert p.outcome.value == expected["outcome"]
    assert p.start_time == expected["start_time"] and p.end_time == expected["end_time"]
    assert not p.samples[0].aoa_derived  # player passes: AOA recorded


def test_pass_fixture_set():
    outcomes = sorted(json.loads(m.read_text())["pass"]["outcome"] for m in PASSES)
    assert outcomes == ["bolter", "bolter", "trap", "trap", "trap"]
