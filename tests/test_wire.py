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


SERVER = FIXTURES / "wires" / "server-dcs-wire-2.zip.acmi"
PILOT = FIXTURES / "wires" / "pilot-dcs-wire-2.zip.acmi"


def test_no_estimate_from_a_servers_copy_of_a_clients_jet():
    # DCS: wire 2. The server smooths the jet through the arrestment and overshoots the stop by ~12 m,
    # which would read as wire 3.
    (p,) = find_passes(load_recording(SERVER))
    assert p.outcome.value == "trap" and p.samples[0].aoa_derived and p.wire_estimate is None


def test_estimate_from_the_pilots_own_track_against_the_servers_carrier(tmp_path):
    """The same wire-2 trap as a merged landing: the pilot's track, the server's carrier."""
    from dcs_lso.central.db import Pass
    from dcs_lso.central.service import Central
    from dcs_lso.detect.approaches import find_approaches
    from dcs_lso.slices import sidecar, slice_objects, track_sidecar

    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    central.add_source("server1")
    central.add_source("pilot", kind="pilot")
    server = load_recording(SERVER)
    (sp,) = find_passes(server)
    central.ingest(1, SERVER.read_bytes(), sidecar(server, sp, "s", slice_objects(server, sp)))
    pilot = load_recording(PILOT)
    (jet,) = [o.id for o in pilot.objects.values() if o.name == "FA-18C_hornet"]
    (approach,) = find_approaches(pilot, jet)
    result = central.ingest(2, PILOT.read_bytes(), track_sidecar(pilot, approach, "c"))
    with central.sessions() as s:
        landing = central.load_pass(s.get(Pass, result.pass_id))
    assert landing.track_source == "pilot" and landing.wire_estimate == 2
