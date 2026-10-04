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
    assert "C" in text and "DCS LSO: GRADE:C : LNFIW  WIRE# 3" in text and "wire #3" in text
    # Our remarks are in plain English on the card (DCS's own line stays as DCS wrote it).
    assert "Landed nose first" in text and "A little fast" not in text and "Fast" in text
    tracks = [e for e in root.iter(f"{SVG}polyline") if "tc-track" in e.get("class", "")]
    assert len(tracks) >= 2  # side and top views (split into AOA-coloured runs)
    assert any("tc-wire-caught" in e.get("class", "") for e in root.iter(f"{SVG}line"))
    assert "Cut · 0 pts" in text
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


def test_no_grade_is_labelled_not_blank():
    from dcs_lso.acmi import load_recording
    (p,) = find_passes(load_recording(FIXTURES / "passes" / "20260928-025423_New_callsign_86s.zip.acmi"))
    root = ET.fromstring(render_card(p, grade_pass(p)))
    texts = [t.text or "" for t in root.iter(f"{SVG}text")]
    assert "NG" in texts and "---" not in texts
    assert any("No Grade · 2 pts" in t for t in texts)
    assert any(t.startswith("wire #") and "(est.)" in t for t in texts)  # no DCS wire: the estimate, marked


def test_lineup_calls_go_on_the_lineup_plot():
    from dcs_lso.cards.svg import SIDE_H, SIDE_TOP, TOP_H, TOP_TOP
    recording = load_recording(FIXTURES / "ai_hornet_trap_cvn75.zip.acmi")
    (p,) = find_passes(recording)
    calls = [{"time": 1.0, "along": 900.0, "call": "you're high"},
             {"time": 2.0, "along": 700.0, "call": "a little come left"}]
    root = ET.fromstring(render_card(p, grade_pass(p), calls=calls))
    y = {t.text: float(t.get("y")) for t in root.iter(f"{SVG}text") if "tc-call-label" in t.get("class", "")}
    assert SIDE_TOP - 30 < y["You're high"] < SIDE_TOP + SIDE_H
    assert TOP_TOP - 30 < y["A little come left"] < TOP_TOP + TOP_H


def test_lineup_view_shows_the_deck_and_wires():
    (p,) = find_passes(load_recording(FIXTURES / "wires" / "dcs-wire-2.zip.acmi"))
    root = ET.fromstring(render_card(p, grade_pass(p)))
    wires = [e for e in root.iter(f"{SVG}line") if e.get("class", "").startswith("tc-wire")]
    assert len(wires) == 8  # 4 in the glideslope view, 4 across the deck in the lineup view
    assert sum(e.get("class") == "tc-wire-caught" for e in wires) == 2  # the (estimated) caught wire, in both
    decks = [e for e in root.iter() if e.get("class") == "tc-deck"]
    assert len(decks) == 2


def test_card_zooms_into_a_stretch_of_the_approach():
    (p,) = find_passes(load_recording(FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"))
    calls = [{"time": 0.0, "along": 250.0, "call": "power"}, {"time": 1.0, "along": 900.0, "call": "you're high"}]
    full = render_card(p, grade_pass(p), calls=calls, zoom_hint=True)
    root = ET.fromstring(full)
    assert (float(root.get("data-near")), float(root.get("data-far"))) == (-40.0, 1481.6)
    assert "0.5 nm" in full and "drag across a chart to zoom" in full
    zoomed = render_card(p, grade_pass(p), calls=calls, view=(-40.0, 300.0), zoom_hint=True)
    root = ET.fromstring(zoomed)
    assert (float(root.get("data-near")), float(root.get("data-far"))) == (-40.0, 300.0)
    assert "200 ft" in zoomed and "0.5 nm" not in zoomed and "zoomed in" in zoomed
    # Only the calls in view get a marker (all are still listed below the card).
    assert zoomed.count('class="tc-call"') == 1 and "re high (0.49 nm)" in zoomed
    assert "drag across" not in render_card(p, grade_pass(p))  # offline cards: no hint
