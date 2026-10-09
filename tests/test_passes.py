"""End-to-end checks against a real recording: an AI FA-18C trapping on a stationary CVN-75."""

from pathlib import Path

import pytest

from burble.acmi import Transform, load_recording
from burble.detect import Outcome, find_passes
from burble.geometry import FA18C, NIMITZ, CarrierPose, DeckFrame

FIXTURE = Path(__file__).parent / "fixtures" / "ai_hornet_trap_cvn75.zip.acmi"
NM = 1852.0


@pytest.fixture(scope="module")
def passes():
    return list(find_passes(load_recording(FIXTURE)))


def test_single_trap_detected(passes):
    assert len(passes) == 1
    p = passes[0]
    assert p.outcome is Outcome.TRAP
    assert p.wire is None  # only DCS's LSO provides the wire
    assert p.carrier_type == "CVN_75" and p.aircraft_type == "FA-18C_hornet"


def test_lineup_converges_on_landing_area_centerline(passes):
    samples = passes[0].samples
    in_close = [s for s in samples if 0 < s.along < 0.25 * NM]
    assert in_close and all(abs(s.lateral) < 1.0 for s in in_close)


def test_parked_hook_sits_on_deck(passes):
    last = passes[0].samples[-1]
    assert last.ground_speed < 2.0
    assert last.hook_height == pytest.approx(0.0, abs=0.5)


def test_glideslope_tracking_is_tight(passes):
    # DCS's AI flies about 3.55 deg (3.4-3.8): within 2.4 m of the 3.5 deg glideslope from 3/4 nm to the ramp.
    groove = [s for s in passes[0].samples if 0 < s.along < 0.75 * NM]
    assert all(abs(s.glideslope_deviation) < 2.5 for s in groove)


def test_aoa_is_derived_without_recorded_aoa(passes):
    groove = [s for s in passes[0].samples if 0 < s.along < 0.75 * NM]
    assert all(s.aoa_derived for s in groove)
    mean = sum(s.aoa for s in groove) / len(groove)
    assert 5.0 < mean < 9.0


def test_wire_positions_are_ordered_and_spaced():
    frame = DeckFrame(NIMITZ, FA18C)
    along = frame.wire_along
    assert list(along) == sorted(along, reverse=True)
    spacing = [a - b for a, b in zip(along, along[1:])]
    assert all(11.0 < s < 14.0 for s in spacing)
    # Aim point sits between wires 2 and 3.
    assert along[1] > 0 > along[2]


def test_deck_frame_uses_grid_heading_axes():
    # A hook placed exactly on the aim point of a carrier heading east should read as zero offset.
    frame = DeckFrame(NIMITZ, FA18C)
    carrier = CarrierPose(u=0.0, v=0.0, alt=0.0, heading=90.0)
    x, z = frame.aim
    # Carrier-local -> world for heading 90: u = z, v = -x.
    hy, hz = FA18C.hook
    plane = Transform(u=z - hz, v=-x, alt=NIMITZ.deck_altitude - hy, pitch=0.0, heading=90.0)
    pos = frame.position(carrier, plane)
    assert pos.along == pytest.approx(0.0, abs=1e-6)
    assert pos.lateral == pytest.approx(0.0, abs=1e-6)
    assert pos.hook_height == pytest.approx(0.0, abs=1e-6)
