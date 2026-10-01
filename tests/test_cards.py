import xml.etree.ElementTree as ET
from pathlib import Path

from dcs_lso.acmi import load_recording
from dcs_lso.cards import CardEntry, render_card, render_index
from dcs_lso.cli import main
from dcs_lso.dcslog import attach_dcs_grades, load_debrief
from dcs_lso.detect import find_passes
from dcs_lso.grading import grade_pass

FIXTURES = Path(__file__).parent / "fixtures"
SVG = "{http://www.w3.org/2000/svg}"


def ai_card() -> str:
    recording = load_recording(FIXTURES / "ai_hornet_trap_cvn75.zip.acmi")
    (p,) = find_passes(recording)
    attach_dcs_grades([p], recording, load_debrief(FIXTURES / "ai_hornet_trap_cvn75.debrief.log"))
    return render_card(p, grade_pass(p), "testing")


def test_card_is_valid_svg_with_grade_and_tracks():
    root = ET.fromstring(ai_card())
    assert root.tag == f"{SVG}svg"
    text = " ".join(t.text or "" for t in root.iter(f"{SVG}text"))
    assert "C" in text and "LNFIW" in text and "DCS LSO: GRADE:C : LNFIW  WIRE# 3" in text and "wire #3" in text
    tracks = [e for e in root.iter(f"{SVG}polyline") if "tc-track" in e.get("class", "")]
    assert len(tracks) >= 2  # side and top views (split into AOA-coloured runs)
    assert any("tc-wire-caught" in e.get("class", "") for e in root.iter(f"{SVG}line"))
    # Plot areas are clipped with clipPath (nested <svg> viewports didn't render in browsers).
    assert not [e for e in root.iter(f"{SVG}svg") if e is not root]
    assert {c.get("id") for c in root.iter(f"{SVG}clipPath")} == {"tc-side", "tc-top"}


def test_card_supports_dark_mode():
    assert "prefers-color-scheme: dark" in ai_card()


def test_index_lists_cards_and_escapes():
    html = render_index([CardEntry("a.svg", "<svg id='x'></svg>", "Pilot <1>", "C", "C : LNFIW", 0.0, "trap",
                                   "rec.acmi", 264.0, None)], "1")
    assert "<svg id='x'></svg>" in html and 'href="a.svg"' in html
    assert "Pilot &lt;1&gt;" in html and "grading v1" in html


def test_cards_command_writes_svgs_and_index(tmp_path):
    passes = sorted((FIXTURES / "passes").glob("*.zip.acmi"))
    assert main(["cards", *map(str, passes), "-o", str(tmp_path)]) == 0
    svgs = sorted(tmp_path.glob("*.svg"))
    assert len(svgs) == len(passes)
    index = (tmp_path / "index.html").read_text()
    for svg in svgs:
        ET.fromstring(svg.read_text())
        assert svg.name in index
    # Inlined cards need distinct clipPath ids.
    ids = [f'id="c{i}-side"' for i in range(len(passes))]
    assert all(index.count(i) == 1 for i in ids)
