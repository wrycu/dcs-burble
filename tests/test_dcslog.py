from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.dcslog import LsoGrade, attach_dcs_grades, load_debrief
from dcs_lso.dcslog.luatable import as_list, parse_assignments
from dcs_lso.detect import find_passes

FIXTURES = Path(__file__).parent / "fixtures"


def test_lua_table_literals():
    data = parse_assignments(
        'a = 1\n'
        'b = { [1] = { x = "q\\"uote", y = -2.5e1, }, -- trailing comment\n'
        '      [2] = { flag = true, none = nil }, name = "n" }\n'
        'c = {}\n'
    )
    assert data["a"] == 1
    assert as_list(data["b"]) == [{"x": 'q"uote', "y": -25.0}, {"flag": True, "none": None}]
    assert data["b"]["name"] == "n"
    assert data["c"] == {}


@pytest.mark.parametrize(
    ("comment", "grade", "wire", "remarks"),
    [
        ("LSO: GRADE:C : LNFIW  WIRE# 3", "C", 3, "LNFIW"),
        ("GRADE:--- : _SLOX_ WIRE# 3 _EGIW_", "---", 3, "_SLOX_ _EGIW_"),
        ("GRADE: ---: _SLOX_ LURX LULX WIRE #2 _EGIW_", "---", 2, "_SLOX_ LURX LULX _EGIW_"),
        ("GRADE:OWO : _LOIC_ _LOAR_", "OWO", None, "_LOIC_ _LOAR_"),
    ],
)
def test_lso_grade_parsing(comment, grade, wire, remarks):
    parsed = LsoGrade.parse(comment)
    assert (parsed.grade, parsed.wire, parsed.remarks) == (grade, wire, remarks)


def test_debrief_events():
    debrief = load_debrief(FIXTURES / "ai_hornet_trap_cvn75.debrief.log")
    assert debrief.mission_time == pytest.approx(322.722)
    (mark,) = debrief.landing_marks()
    assert mark.time == pytest.approx(294.655)
    assert mark.place == "Naval-1-1"
    assert mark.initiator_object_id == 16777728
    assert mark.comment == "LSO: GRADE:C : LNFIW  WIRE# 3"


def test_dcs_grade_attached_to_pass():
    recording = load_recording(FIXTURES / "ai_hornet_trap_cvn75.zip.acmi")
    passes = list(find_passes(recording))
    attach_dcs_grades(passes, recording, load_debrief(FIXTURES / "ai_hornet_trap_cvn75.debrief.log"))
    (p,) = passes
    assert p.wire == 3
    assert p.dcs_grade.grade == "C"
    assert p.dcs_grade.remarks == "LNFIW"
