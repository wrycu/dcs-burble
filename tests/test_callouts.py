from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.callouts import Call, CalloutEngine, GrooveState, Thresholds
from dcs_lso.callouts.rules import conditions
from dcs_lso.callouts.sim import replay
from dcs_lso.detect import find_passes

NM = 1852.0
FIXTURE = Path(__file__).parent / "fixtures" / "ai_hornet_trap_cvn75.zip.acmi"


def state(t=0.0, along=0.5 * NM, gs=0.0, gs_rate=0.0, lineup=0.0, lineup_rate=0.0, lateral=0.0, aoa=8.1,
          heading_error=0.0, roll=0.0) -> GrooveState:
    return GrooveState(t, along, gs, gs_rate, lineup, lineup_rate, lateral, aoa, False, heading_error, roll, 5)


def test_conditions_zones():
    th = Thresholds()
    assert conditions(state(gs=0.7), th) == {Call.HIGH}
    assert conditions(state(gs=-0.6, gs_rate=0.1), th) == {Call.LOW}  # low but correcting
    assert conditions(state(gs=-0.6, gs_rate=-0.1), th) == {Call.POWER}
    assert conditions(state(lineup=-1.5), th) == {Call.RIGHT_FOR_LINEUP}
    assert conditions(state(aoa=6.0), th) == {Call.FAST}
    assert Call.WAVE_OFF in conditions(state(along=0.2 * NM, gs=-1.5), th)
    assert Call.WAVE_OFF not in conditions(state(along=0.5 * NM, gs=-1.5), th)  # too far out to wave off
    assert conditions(state(along=100.0, gs=-3.0), th) == set()  # at the ramp: too late for any call


def test_hysteresis_keeps_call_active_near_threshold():
    th = Thresholds()
    assert conditions(state(gs=0.45), th) == set()
    assert conditions(state(gs=0.45), th, frozenset({Call.HIGH})) == {Call.HIGH}


def feed(engine, states):
    return [e.call for s in states if (e := engine.update(s)) is not None]


def test_hold_then_call_and_no_repeat_while_correcting():
    engine = CalloutEngine()
    # In the groove (inside 0.4 nm), high from t=0; called once it has held for 0.5 s.
    calls = feed(engine, [state(t=i * 0.1, along=0.35 * NM - i * 7, gs=0.8) for i in range(10)])
    assert calls == [Call.HIGH]
    # Still high 6 s later but correcting: no repeat.
    assert feed(engine, [state(t=6.0 + i * 0.1, along=0.3 * NM, gs=0.8, gs_rate=-0.2) for i in range(5)]) == []
    # Still high, not correcting: repeated.
    assert feed(engine, [state(t=7.0 + i * 0.1, along=0.3 * NM, gs=0.8) for i in range(10)]) == [Call.HIGH]


def test_no_calls_in_the_turn_and_nothing_after_wave_off():
    engine = CalloutEngine()
    turning = [state(t=i * 0.1, along=0.7 * NM, gs=1.0, roll=30.0, heading_error=40.0) for i in range(20)]
    assert feed(engine, turning) == []
    low = [state(t=10 + i * 0.1, along=0.2 * NM, gs=-1.5, gs_rate=-0.1) for i in range(30)]
    calls = feed(engine, low)
    assert Call.WAVE_OFF in calls and calls[-1] is Call.WAVE_OFF


def test_on_glideslope_ai_pass_gets_no_glideslope_or_lineup_calls():
    recording = load_recording(FIXTURE)
    (p,) = find_passes(recording)
    calls = {c.call for c in replay(recording, p).calls}
    assert not calls & {Call.WAVE_OFF, Call.POWER, Call.LOW, Call.HIGH, Call.RIGHT_FOR_LINEUP, Call.COME_LEFT}


@pytest.mark.parametrize("hz", [None, 2.0])
def test_live_estimate_tracks_hindsight(hz):
    recording = load_recording(FIXTURE)
    (p,) = find_passes(recording)
    r = replay(recording, p, hz=hz)
    rms, worst = r.errors["glideslope_deg"]
    assert rms < 0.05 and worst < 0.2
