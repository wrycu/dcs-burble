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
          heading_error=0.0, roll=0.0, pitch_rate=0.0, gear=None, foul_deck=False) -> GrooveState:
    return GrooveState(t, along, gs, gs_rate, lineup, lineup_rate, lateral, aoa, False, heading_error, roll, 5,
                       pitch_rate=pitch_rate, gear=gear, foul_deck=foul_deck)


def test_conditions_zones():
    th = Thresholds()
    assert conditions(state(gs=0.8), th) == {Call.HIGH}
    assert conditions(state(gs=0.5), th) == {Call.LITTLE_HIGH}
    assert conditions(state(gs=-0.8, gs_rate=0.1), th) == {Call.LOW}  # low but correcting
    assert conditions(state(gs=-0.8, gs_rate=-0.1), th) == {Call.POWER}
    assert conditions(state(gs=-0.5), th) == {Call.LITTLE_LOW}
    assert conditions(state(gs=-0.5, gs_rate=-0.4), th) == {Call.POWER}  # a little low and sinking
    assert conditions(state(along=0.2 * NM, gs=-1.1), th) == {Call.POWER_X3}
    assert conditions(state(gs=0.0, gs_rate=-0.2), th) == {Call.GOING_LOW}
    assert conditions(state(gs=0.0, gs_rate=0.8), th, recent_power=True) == {Call.EASY_WITH_IT}
    assert conditions(state(lineup=-1.5), th) == {Call.RIGHT_FOR_LINEUP}
    assert conditions(state(lineup=0.7), th) == {Call.LITTLE_LEFT}
    assert conditions(state(lineup=0.0, lineup_rate=0.3), th) == {Call.DRIFTING_RIGHT}
    assert conditions(state(roll=25.0), th) == {Call.EASY_WINGS}
    assert conditions(state(aoa=7.0), th) == {Call.FAST}
    assert conditions(state(aoa=9.0), th) == {Call.SLOW}
    assert Call.WAVE_OFF in conditions(state(along=0.2 * NM, gs=-1.5), th)
    assert Call.WAVE_OFF not in conditions(state(along=0.5 * NM, gs=-1.5), th)  # too far out to wave off
    assert conditions(state(along=100.0, gs=-3.0), th) == set()  # at the ramp: too late for any call


def test_hysteresis_keeps_call_active_near_threshold():
    th = Thresholds()
    assert conditions(state(gs=0.3), th) == set()
    assert conditions(state(gs=0.3), th, frozenset({Call.LITTLE_HIGH})) == {Call.LITTLE_HIGH}
    assert conditions(state(aoa=7.6), th) == set()
    assert conditions(state(aoa=7.6), th, frozenset({Call.FAST})) == {Call.FAST}


def test_power_escalates_and_lineup_needs_three_seconds():
    engine = CalloutEngine()
    low = [state(t=i * 0.1, along=0.35 * NM - i * 7, gs=-0.8, gs_rate=-0.05) for i in range(60)]
    calls = feed(engine, low)
    assert calls[:2] == [Call.POWER, Call.POWER_X2]
    engine = CalloutEngine()
    left = [state(t=i * 0.1, along=0.35 * NM - i * 3, lineup=-1.5) for i in range(40)]
    calls = [(c, i) for i, s in enumerate(left) if (e := engine.update(s)) and (c := e.call)]
    assert calls and calls[0][0] is Call.RIGHT_FOR_LINEUP and calls[0][1] >= 30  # held 3 s first


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


def test_spacing_and_repeats_count_from_the_end_of_the_phrase():
    # High from t=0 and never correcting: HIGH is called at 0.4 s and repeated only after it has
    # finished being said (0.4 + duration) plus the repeat interval.
    th = Thresholds()
    for duration in (0.5, 2.0):
        engine = CalloutEngine(th, {Call.HIGH: duration})
        times = [e.time for i in range(100)
                 if (e := engine.update(state(t=i * 0.1, along=0.35 * NM - i * 3, gs=0.8))) is not None]
        assert len(times) >= 2
        assert times[1] - times[0] >= duration + th.repeat_s - 1e-9
        assert times[1] - times[0] < duration + th.repeat_s + 0.2



def test_foul_deck_wave_off():
    th = Thresholds()
    assert Call.WAVE_OFF_FOUL_DECK in conditions(state(along=0.3 * NM, foul_deck=True), th)
    assert Call.WAVE_OFF_FOUL_DECK in conditions(state(along=100.0, foul_deck=True), th)  # still, at the ramp
    assert Call.WAVE_OFF_FOUL_DECK not in conditions(state(along=0.5 * NM, foul_deck=True), th)  # not yet
    engine = CalloutEngine()
    calls = feed(engine, [state(t=i * 0.1, along=0.4 * NM - i * 7, foul_deck=i >= 10) for i in range(40)])
    assert calls == [Call.WAVE_OFF_FOUL_DECK]  # once, and nothing after it
    assert engine.waved_off


def test_dont_settle_or_climb_in_close():
    th = Thresholds()
    assert conditions(state(along=0.3 * NM, gs_rate=-0.2), th) == {Call.GOING_LOW}
    assert conditions(state(along=0.2 * NM, gs_rate=-0.2), th) == {Call.DONT_SETTLE}
    assert conditions(state(along=0.2 * NM, gs_rate=0.2), th) == {Call.DONT_CLIMB}


def test_keep_it_coming_when_steady_and_quiet():
    engine = CalloutEngine()
    good = [state(t=i * 0.1, along=0.6 * NM - i * 7) for i in range(120)]  # 12 s on glideslope and centerline
    calls = [(e.call, e.time) for s in good if (e := engine.update(s)) is not None]
    assert [c for c, _ in calls] == [Call.KEEP_IT_COMING, Call.KEEP_IT_COMING]  # at most two per pass
    assert calls[1][1] - calls[0][1] >= 4.0  # with quiet between them
    # Not for a pass that's off, even a little.
    engine = CalloutEngine()
    assert Call.KEEP_IT_COMING not in feed(engine, [state(t=i * 0.1, along=0.6 * NM - i * 7, gs=0.3)
                                                    for i in range(120)])


def test_keep_your_turn_in_when_overshooting():
    engine = CalloutEngine()
    # Still in the turn (not in the groove) at 1 nm, already 30 m right of centerline and heading further right.
    turning = [state(t=i * 0.1, along=1.0 * NM - i * 5, lateral=30.0, heading_error=25.0, roll=-30.0)
               for i in range(30)]
    assert feed(engine, turning) == [Call.KEEP_TURN_IN]  # once
    # Heading back toward the centerline: no call.
    engine = CalloutEngine()
    assert feed(engine, [state(t=i * 0.1, along=1.0 * NM, lateral=30.0, heading_error=-20.0, roll=-30.0)
                         for i in range(30)]) == []


def test_urgent_calls_follow_quickly_others_wait_longer():
    # "You're high" from 0.4 s (0.5 s long, so said until 0.9 s); then from 1.0 s the jet is low and sinking.
    th = Thresholds()

    def after_high(gs: float, gs_rate: float) -> tuple[Call, float]:
        engine = CalloutEngine(th, {Call.HIGH: 0.5})
        states = [state(t=i * 0.05, along=0.35 * NM - i * 3, gs=0.8) for i in range(20)]
        states += [state(t=1.0 + i * 0.05, along=0.35 * NM - 60 - i * 3, gs=gs, gs_rate=gs_rate) for i in range(60)]
        events = [e for s in states if (e := engine.update(s)) is not None]
        assert events[0].call is Call.HIGH
        return events[1].call, events[1].time - (events[0].time + 0.5)

    call, gap = after_high(gs=-0.8, gs_rate=-0.4)  # sinking low: "power" can't wait
    assert call in (Call.POWER, Call.LOW) and th.urgent_spacing_s <= gap < th.spacing_s
    call, gap = after_high(gs=-0.45, gs_rate=0.0)  # a little low: coaching, unhurried
    assert call is Call.LITTLE_LOW and gap >= th.spacing_s
