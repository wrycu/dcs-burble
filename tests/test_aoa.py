"""AOA derived from motion, checked against real AOA: the same passes recorded by a dedicated
server (no AOA, 4.8 Hz) and by the pilot's own Tacview (real AOA)."""

import math
from datetime import datetime
from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.callouts.estimator import LiveEstimator, LiveInput, derived_aoa
from dcs_lso.callouts.sim import _inputs
from dcs_lso.detect import find_passes
from dcs_lso.geometry import FA18C, WindProfile

PAIRS = Path(__file__).parent / "fixtures" / "server_vs_client"
NM = 1852.0


def real_aoa(kind: str):
    """The pilot's recorded AOA as a function of server time."""
    server = load_recording(PAIRS / f"server-{kind}.zip.acmi")
    client = load_recording(PAIRS / f"client-{kind}.zip.acmi")
    # Both count from their ReferenceTime (mission time), which differs between the two files.
    ref = {r: datetime.fromisoformat(rec.globals["ReferenceTime"]) for r, rec in (("s", server), ("c", client))}
    offset = (ref["s"] - ref["c"]).total_seconds()
    (jet,) = [o for o in client.objects.values() if o.samples and o.samples[0].aoa is not None]
    times = [s.time for s in jet.samples]
    values = [s.aoa for s in jet.samples]

    def at(server_time: float) -> float:
        t = server_time + offset
        i = max(1, min(len(times) - 1, next((k for k, x in enumerate(times) if x >= t), len(times) - 1)))
        f = (t - times[i - 1]) / (times[i] - times[i - 1])
        return values[i - 1] + f * (values[i] - values[i - 1])

    return server, at


def in_groove(along: float) -> bool:
    # From the start of the groove to just before the ramp: in the last ~150 m the pilot's final
    # corrections are faster than 4.8 Hz motion can resolve (1-2 deg RMS there; AR AOA isn't graded).
    return 150.0 < along <= 0.75 * NM


@pytest.mark.parametrize("kind", ["bolter", "trap"])
def test_grading_aoa_from_server_data_matches_the_pilots_real_aoa(kind):
    server, truth = real_aoa(kind)
    (p,) = find_passes(server)
    errors = [s.aoa - truth(s.time) for s in p.samples if in_groove(s.along) and s.aoa is not None]
    assert p.samples[0].aoa_derived and len(errors) > 50
    rms = math.sqrt(sum(e * e for e in errors) / len(errors))
    assert rms < 0.25
    assert sum(abs(e) < 0.5 for e in errors) / len(errors) >= 0.95


@pytest.mark.parametrize("kind", ["bolter", "trap"])
def test_live_aoa_from_server_data_matches_the_pilots_real_aoa(kind):
    server, truth = real_aoa(kind)
    (p,) = find_passes(server)
    estimator = LiveEstimator(FA18C.glideslope)
    errors = []
    for x in _inputs(server, p, None, strip_aoa=True):
        state = estimator.update(x)
        if in_groove(x.along) and state.aoa is not None:
            errors.append(state.aoa - truth(x.time))  # compared with the real AOA *now* (it is 1 sample late)
    rms = math.sqrt(sum(e * e for e in errors) / len(errors))
    assert rms < 0.4
    assert sum(abs(e) < 0.5 for e in errors) / len(errors) >= 0.8


def test_wind_is_taken_out_of_derived_aoa():
    # Wings level, heading north at 70 m/s through the air, 3.5 deg below the horizon relative to
    # the air, AOA 8.1 deg; a 10 m/s wind from the north (blowing south).
    airspeed, gamma, aoa = 70.0, math.radians(-3.5), 8.1
    air = (0.0, airspeed * math.cos(gamma), airspeed * math.sin(gamma))
    wind = (0.0, -10.0)
    ground = (air[0] + wind[0], air[1] + wind[1], air[2])
    pitch = math.degrees(gamma) + aoa

    def at(t: float, with_wind: bool) -> LiveInput:
        return LiveInput(time=t, along=0, lateral=0, hook_height=0, pitch=pitch, alt=100 + ground[2] * t,
                         u=ground[0] * t, v=ground[1] * t, aoa=None, heading=0.0,
                         wind=wind if with_wind else (0.0, 0.0))

    corrected = derived_aoa(at(0.0, True), at(0.21, True), at(0.42, True))
    uncorrected = derived_aoa(at(0.0, False), at(0.21, False), at(0.42, False))
    assert corrected == pytest.approx(aoa, abs=1e-6)
    assert uncorrected - aoa == pytest.approx(0.58, abs=0.01)  # a 10 m/s headwind would read as "slow"


def test_bank_is_accounted_for():
    # In a 30 deg bank, the airflow comes from below the nose in the aircraft's own frame, not
    # straight below in the world: pitch minus flight path would understate the AOA.
    from dcs_lso.geometry import body_aoa
    aoa, bank = 8.0, 30.0
    # Body frame velocity (forward, right, down) for this AOA; level flight in the world.
    a = math.radians(aoa)
    fwd, down = math.cos(a), math.sin(a)
    # Rotate body to world for heading 0 (north), pitch theta, roll phi: find theta so climb is 0.
    phi = math.radians(bank)
    theta = math.atan(down * math.cos(phi) / fwd)
    vn = fwd * math.cos(theta) + down * math.cos(phi) * math.sin(theta)
    ve = -down * math.sin(phi)
    vd = -fwd * math.sin(theta) + down * math.cos(phi) * math.cos(theta)
    assert vd == pytest.approx(0.0, abs=1e-9)
    assert body_aoa((ve, vn, -vd), 0.0, math.degrees(theta), bank) == pytest.approx(aoa)
    assert math.degrees(theta) < aoa - 1  # what pitch minus flight path would have said


def test_wind_profile_interpolates_between_altitudes():
    w = WindProfile.from_dict({"levels": [{"alt": 100, "east": 2.0, "north": -6.0},
                                          {"alt": 10, "east": 0.0, "north": -4.0}]})
    assert w.at(0) == (0.0, -4.0)
    assert w.at(55) == pytest.approx((1.0, -5.0))
    assert w.at(500) == (2.0, -6.0)
    assert WindProfile.from_dict(w.to_dict()) == w
    assert WindProfile.from_dict(None) is None
