"""The Forrestal (CV-59): its own deck geometry, and a pass on it detected, graded and drawn.

The recording is a real Nimitz-class trap (Wrycu on the Truman) with the carrier renamed to the
Forrestal and raised so its (lower) deck is where the Nimitz's was: the same flight, measured against
the Forrestal's deck.
"""

import zipfile
from pathlib import Path

import pytest

from burble.acmi import load_recording
from burble.cards import render_card
from burble.detect import find_passes
from burble.geometry import CARRIERS, DeckFrame
from burble.geometry.data import FA18C, FORRESTAL, NIMITZ

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"


def test_deck_geometry():
    assert CARRIERS["Forrestal"] is FORRESTAL
    frame, nimitz = DeckFrame(FORRESTAL, FA18C), DeckFrame(NIMITZ, FA18C)
    # The aim point is halfway between wires 2 and 3, wires about 10.4 m apart (the Nimitz's 12.5 m).
    assert frame.wire_along[1] == pytest.approx(-frame.wire_along[2], abs=0.05)
    gaps = [a - b for a, b in zip(frame.wire_along, frame.wire_along[1:])]
    assert all(9.5 < g < 11.0 for g in gaps)
    # The wires cross the landing area square to its centerline (the deck angle is right).
    for (pa, pl), (sa, sl) in frame.wire_ends:
        assert abs(pa - sa) < 1.0 and pl < 0 < sl
    # Ramp to wire 1 as on the Nimitz.
    assert FORRESTAL.ramp_along_m - frame.wire_along[0] == pytest.approx(
        NIMITZ.ramp_along_m - nimitz.wire_along[0], abs=1.0)


@pytest.fixture
def recording(tmp_path) -> Path:
    with zipfile.ZipFile(SOURCE) as z:
        (name,) = z.namelist()
        text = z.read(name).decode("utf-8-sig")
    assert "Name=CVN_75" in text
    raise_m = NIMITZ.deck_altitude - FORRESTAL.deck_altitude
    lines, first = [], True
    for line in text.splitlines():
        head, _, rest = line.partition(",")
        if head == "102":  # the carrier: altitude is the third T= field (blank = unchanged, 0 at first)
            fields = rest.split(",")
            for i, f in enumerate(fields):
                if f.startswith("T="):
                    t = f[2:].split("|")
                    if len(t) > 2 and (t[2] or first):
                        t[2] = f"{float(t[2] or 0.0) + raise_m:.2f}"
                    first = False
                    fields[i] = "T=" + "|".join(t)
            line = f"{head}," + ",".join(fields)
        lines.append(line)
    text = "\n".join(lines) + "\n"
    out = tmp_path / "forrestal.zip.acmi"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text.replace("Name=CVN_75", "Name=Forrestal"))
    return out


def test_pass_on_the_forrestal(recording):
    from burble.grading import grade_pass
    (p,) = find_passes(load_recording(recording))
    assert p.carrier_type == "Forrestal" and p.outcome.value == "trap"
    assert p.wire_estimate is None  # arrest runout not measured on its gear
    grade = grade_pass(p)
    assert grade.text
    card = render_card(p, grade)
    assert "<svg" in card and "est.</text>" not in card  # no wire marked (no estimate)
    (nimitz,) = find_passes(load_recording(SOURCE))
    assert nimitz.wire_estimate is not None
