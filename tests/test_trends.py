"""Meta grading: themes across a pilot's recent passes."""

from pathlib import Path

from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.central.app import create_app
from dcs_lso.central.service import Central
from dcs_lso.detect import find_passes
from dcs_lso.grading.trends import TrendPass, trends
from dcs_lso.slices import sidecar, slice_objects

FIXTURES = Path(__file__).parent / "fixtures"
ON_SPEED = (7.4, 8.8)


def tp(remarks=(), positions=None, outcome="trap", points=3.0, wire=None, grade="(OK)"):
    return TrendPass(grade, points, outcome, tuple(remarks), positions or {}, ON_SPEED, wire)


def texts(result):
    return [t.text for t in result.themes]


def test_repeated_fault_across_positions_with_severity():
    passes = [tp([("LUL", "IC", 3 if i < 3 else 2), ("LUL", "IM", 1)]) for i in range(10)]
    passes += [tp() for _ in range(2)]
    (theme,) = trends(passes).themes
    assert theme.kind == "fault" and theme.count == 10 and theme.of == 12
    assert theme.text == "Lined up left in the middle and in close: 10 of 12 passes (3 of them lined up well left)"


def test_occasional_faults_are_not_themes():
    passes = [tp([("HI", "X", 2)]) if i < 2 else tp() for i in range(12)]  # 2 of 12: below 40% and 3
    assert trends(passes).themes == []


def test_leaning_below_the_remark_thresholds():
    low = {pos: (-0.3, 0.0, 8.0) for pos in ("IC", "AR")}
    passes = [tp(positions={**low, "X": (0.05, 0.0, 8.0)}) for _ in range(10)]
    assert texts(trends(passes)) == ["Tends to be a little low in close and at the ramp (-0.3° on average)"]


def test_speed_wires_outcomes_and_trend():
    passes = []
    for i in range(12):
        passes.append(tp(positions={"IM": (0.0, 0.0, 7.0), "IC": (0.0, 0.0, 7.1)},
                         outcome="bolter" if i in (1, 5, 9) else "trap",
                         points=4.0 if i < 6 else 2.0, wire=(2 if i % 2 else 1) if i not in (1, 5, 9) else None))
    result = texts(trends(passes))
    assert "Tends to fly a little fast: below on-speed AOA in 12 of 12 passes" in result
    assert "Bolters: 3 of 12 passes" in result
    assert any(t.startswith("Landing short: 9 of 9 traps on the 1 or 2 wire") for t in result)
    assert result[-1] == "Improving: 4.0 points on average over the last 6 passes, up from 2.0"  # the trend comes last


def test_strengths_never_contradict_a_theme():
    passes = [tp(positions={"IM": (0.0, 0.0, 7.0), "IC": (0.0, 0.0, 7.0)}) for _ in range(8)]
    result = trends(passes)
    assert any(t.kind == "speed" for t in result.themes)  # fast, though never enough for a remark
    assert result.strengths == ["Glideslope control: no remarks in 8 of 8 passes", "Lineup: no remarks in 8 of 8 passes"]


def test_from_stored_grade_details():
    detail = {"grade": "C", "points": 0.0, "wire_estimate": 2,
              "remarks": [{"code": "LO", "position": "IC", "severity": "lot", "text": "_LOIC_"}],
              "positions": [{"position": "IC", "samples": 9, "glideslope_deg": -1.4, "lineup_deg": 0.1,
                             "lateral_m": 0.5, "aoa": 8.0}]}
    p = TrendPass.from_detail(detail, "trap", ON_SPEED, wire=None)
    assert p.remarks == (("LO", "IC", 3),) and p.positions["IC"] == (-1.4, 0.1, 8.0) and p.wire == 2
    assert TrendPass.from_detail(detail, "trap", ON_SPEED, wire=3).wire == 3  # DCS's wire wins


def test_pilot_page_and_api(tmp_path):
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    central.add_source("s")
    for f in sorted((FIXTURES / "passes").glob("20260927-*.zip.acmi")):
        r = load_recording(f)
        (p,) = find_passes(r)
        central.ingest(1, f.read_bytes(), sidecar(r, p, f.name, slice_objects(r, p)))
    client = TestClient(create_app(central))
    body = client.get("/api/v1/pilots/Wrycu/trends").json()
    assert body["passes"] == 4 and len(body["pass_ids"]) == 4 and body["analysis"]
    assert {t["kind"] for t in body["results"]} <= {"outcome", "wires"}
    # When the pilot was first and last seen, across all their landings.
    assert body["landings"] == 4 and body["first_seen"] < body["last_seen"]
    page = client.get("/pilots/Wrycu").text
    assert "<h2" in page and ">Analysis</h2>" in page and ">Results</h2>" in page
    assert body["analysis"][0]["text"] in page
    assert "Traps overlaid" in page and page.count('class="ov-track') == 8  # 4 passes, 2 views
    # Hovering a pass's grade square previews its trap card, fetched on demand; the overlay's lines don't.
    ids = body["pass_ids"]
    assert all(page.count(f'data-pass="{i}"') == 1 for i in ids)
    card = client.get(f"/passes/{ids[0]}/card.svg")
    assert card.status_code == 200 and card.headers["content-type"].startswith("image/svg+xml")
    assert card.text.startswith("<svg") and f'id="hover{ids[0]}-side"' in card.text  # ids unique on the page
    assert f"4 landings · first seen {body['first_seen'][:10]} · last seen {body['last_seen'][:10]}" in page
    # Looking at fewer passes doesn't change when the pilot was seen.
    assert client.get("/api/v1/pilots/Wrycu/trends", params={"passes": 3}).json()["first_seen"] == body["first_seen"]
    assert client.get("/pilots/Wrycu", params={"passes": 3}).status_code == 200
    assert client.get("/pilots/Nobody").status_code == 404
    assert 'href="/pilots/Wrycu"' in client.get("/", params={"days": 0}).text



def test_overlay_card(tmp_path):
    import xml.etree.ElementTree as ET

    from dcs_lso.cards.overlay import OverlayPass, render_overlay
    from dcs_lso.grading import grade_pass
    items = []
    for f in sorted((FIXTURES / "passes").glob("20260927-*.zip.acmi")):
        (p,) = find_passes(load_recording(f))
        items.append(OverlayPass(p, grade_pass(p).grade.value, f"/passes/{len(items) + 1}", f.name))
    root = ET.fromstring(render_overlay(items))
    svg = "{http://www.w3.org/2000/svg}"
    tracks = [e for e in root.iter(f"{svg}polyline") if "ov-track" in e.get("class", "")]
    assert len(tracks) == 2 * len(items)  # glideslope and lineup for each pass
    assert sum("latest" in t.get("class") for t in tracks) == 2  # the newest, in both views
    links = {a.get("href") for a in root.iter(f"{svg}a")}
    assert links == {f"/passes/{i}" for i in range(1, len(items) + 1)}


def test_overlay_zoom(tmp_path):
    import re
    import xml.etree.ElementTree as ET
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    central.add_source("s")
    for f in sorted((FIXTURES / "passes").glob("20260927-*.zip.acmi")):
        r = load_recording(f)
        (p,) = find_passes(r)
        central.ingest(1, f.read_bytes(), sidecar(r, p, f.name, slice_objects(r, p)))
    client = TestClient(create_app(central))
    page = client.get("/pilots/Wrycu").text
    assert 'data-src="/pilots/Wrycu/overlay.svg?passes=12"' in page and "zoom-reset" in page
    full = ET.fromstring(client.get("/pilots/Wrycu/overlay.svg").text)
    assert (float(full.get("data-near")), float(full.get("data-far"))) == (-40.0, 1481.6)
    # The last 0.25 nm: the view the page asks for after a drag, with distances in round feet.
    svg = client.get("/pilots/Wrycu/overlay.svg", params={"near": 463, "far": -40}).text  # either order
    zoomed = ET.fromstring(svg)
    assert (float(zoomed.get("data-near")), float(zoomed.get("data-far"))) == (-40.0, 463.0)
    assert "250 ft" in svg and "0.5 nm" not in svg and "zoomed in" in svg
    assert len(re.findall(r'class="ov-track', svg)) == 8
    # Out-of-range and too-narrow requests are kept sensible.
    wide = ET.fromstring(client.get("/pilots/Wrycu/overlay.svg", params={"near": -500, "far": 9999}).text)
    assert (float(wide.get("data-near")), float(wide.get("data-far"))) == (-40.0, 1481.6)
    narrow = ET.fromstring(client.get("/pilots/Wrycu/overlay.svg", params={"near": 100, "far": 101}).text)
    assert float(narrow.get("data-far")) - float(narrow.get("data-near")) >= 15
