from pathlib import Path

import pytest

from burble.acmi import load_recording
from burble.detect import find_passes
from burble.grading import GRADING_VERSION, Grade, Position, Remark, Severity, grade_pass
from burble.grading.grade import POINTS, _trap_grade

FIXTURES = Path(__file__).parent / "fixtures"


def graded(path: Path):
    (p,) = find_passes(load_recording(path))
    return grade_pass(p)


def test_remark_text():
    assert Remark("LO", Position.IC, Severity.LITTLE).text == "(LOIC)"
    assert Remark("LUR", Position.X, Severity.NORMAL).text == "LURX"
    assert Remark("HI", Position.AR, Severity.LOT).text == "_HIAR_"


@pytest.mark.parametrize(("remarks", "grade"), [
    ([], Grade.PERFECT),
    ([Remark("HI", Position.X, Severity.LITTLE)], Grade.OK),
    ([Remark("HI", Position.X, Severity.NORMAL)], Grade.FAIR),
    ([Remark("HI", Position.X, Severity.LOT)], Grade.NO_GRADE),
    ([Remark("LO", Position.IC, Severity.LOT)], Grade.CUT),
    ([Remark("LUL", Position.AR, Severity.LOT)], Grade.CUT),
    ([Remark("LNF", Position.IW, Severity.NORMAL)], Grade.CUT),
])
def test_trap_grade_rules(remarks, grade):
    assert _trap_grade(remarks) is grade


def test_ai_trap_landed_nose_first_is_a_cut():
    # DCS's LSO: "GRADE:C : LNFIW  WIRE# 3"
    g = graded(FIXTURES / "ai_hornet_trap_cvn75.zip.acmi")
    assert g.grade is Grade.CUT and "LNFIW" in g.text
    assert g.version == GRADING_VERSION and g.points == POINTS[Grade.CUT] == 0.0


def test_flat_low_pass_is_a_cut():
    # DCS's LSO: "GRADE:C : EGIW  WIRE# 2[BC]"; flown about 2.8 degrees low throughout.
    g = graded(FIXTURES / "passes" / "20260927-204347_Wrycu_4769s.zip.acmi")
    assert g.grade is Grade.CUT
    assert {r.text for r in g.remarks} >= {"_LOX_", "_LOIM_", "_LOIC_"}


def test_bolters_get_b_with_remarks():
    g = graded(FIXTURES / "passes" / "20260927-204347_Wrycu_3209s.zip.acmi")
    assert g.grade is Grade.BOLTER and g.points == 2.5
    assert "_HIX_" in g.text and "_LURIM_" in g.text


def test_all_positions_counted_on_full_passes():
    for path in (FIXTURES / "passes").glob("*.zip.acmi"):
        assert graded(path).not_counted == [], path.name


def test_grade_names_and_short_labels():
    from burble.grading import grade_name, grade_short
    assert grade_name("---") == "No Grade" and grade_short("---") == "NG"
    assert grade_name("C") == "Cut" and grade_short("C") == "C"
    assert grade_short("_OK_") == "OK+" and grade_name("?") == "?"


@pytest.mark.parametrize(("remark", "english"), [
    (Remark("HI", Position.X, Severity.LOT), "Well high"),
    (Remark("LO", Position.AR, Severity.LITTLE), "A little low"),
    (Remark("LUL", Position.IC, Severity.NORMAL), "Lined up left"),
    (Remark("F", Position.AR, Severity.NORMAL), "Fast"),
    (Remark("LNF", Position.IW, Severity.NORMAL), "Landed nose first"),
])
def test_remark_english(remark, english):
    assert remark.english == english


def test_remark_english_with_position():
    assert Remark("HI", Position.X, Severity.LOT).english_with_position == "Well high at the start"
    assert Remark("LNF", Position.IW, Severity.NORMAL).english_with_position == "Landed nose first in the wires"
