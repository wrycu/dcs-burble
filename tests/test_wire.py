"""Wire estimated from the stop point, against DCS's own wire on the same traps."""

from pathlib import Path

import pytest

from dcs_lso.acmi import Recording, load_recording
from dcs_lso.acmi.reader import ObjectTrack
from dcs_lso.callouts.sim import thin
from dcs_lso.detect import find_passes
from dcs_lso.grading import grade_pass

FIXTURES = Path(__file__).parent / "fixtures"


def thinned(recording: Recording, hz: float) -> Recording:
    """Every aircraft at `hz`, like a dedicated server's recording."""
    objects = {i: ObjectTrack(t.id, t.props, thin(t.samples, hz)) if "Air" in t.tags else t
               for i, t in recording.objects.items()}
    return Recording(recording.globals, objects, recording.first_frame)


@pytest.mark.parametrize("wire", [1, 2, 3])
@pytest.mark.parametrize("hz", [None, 4.8])
def test_estimate_matches_dcs_wire(wire, hz):
    recording = load_recording(FIXTURES / "wires" / f"dcs-wire-{wire}.zip.acmi")
    (p,) = find_passes(thinned(recording, hz) if hz else recording)
    assert p.outcome.value == "trap" and p.wire_estimate == wire
    assert grade_pass(p).wire_estimate == wire  # kept with the grade


def test_no_estimate_without_a_trap():
    for name in ("20260927-204347_Wrycu_3209s", "20260927-204347_Wrycu_3348s"):  # bolters
        (p,) = find_passes(load_recording(FIXTURES / "passes" / f"{name}.zip.acmi"))
        assert p.outcome.value == "bolter" and p.wire_estimate is None


def test_no_estimate_when_the_stop_point_isnt_at_a_wire():
    # The AI trap's carrier is never reported moving, and its stop point is nowhere near any wire's
    # runout: better no estimate than a wrong one (DCS says wire 3).
    (p,) = find_passes(load_recording(FIXTURES / "ai_hornet_trap_cvn75.zip.acmi"))
    assert p.outcome.value == "trap" and p.wire_estimate is None
