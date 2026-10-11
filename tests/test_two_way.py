"""Two-way LSO comms (PLAN #20): Case III ("Paddles contact", "call the ball"), the talk-down after "Clara",
"Roger ball" with the wind over the deck, and the pilot's own calls on the trap card."""

import asyncio

from burble.acmi import ObjectTrack
from burble.acmi.reader import Sample
from burble.acmi.parser import Transform
from burble.agent.callouts import CalloutSettings, LiveCallouts, is_case_iii
from burble.agent.listening import Heard
from burble.callouts import Call, CalloutEngine
from burble.callouts.heard import describe, parse
from burble.callouts.voice import Clip, ClipLibrary
from burble.geometry import WindProfile
from test_callouts import NM, state


def fly(engine: CalloutEngine, start_m: float, end_m: float, speed=70.0, dt=0.25, t0=0.0, **kw) -> list:
    """Fly a steady pass from `start_m` to `end_m` (meters short of the aim point); the calls made, with where."""
    out, t, along = [], t0, start_m
    while along > end_m:
        if (e := engine.update(state(t=t, along=along, **kw))) is not None:
            out.append((e.call, round(along / NM, 2)))
        t, along = t + dt, along - speed * dt
    return out


def test_case_iii_paddles_contact_then_call_the_ball():
    calls = fly(CalloutEngine(case_iii=True), 1.5 * NM, 0.2 * NM)
    assert [c for c, _ in calls][:2] == [Call.PADDLES_CONTACT, Call.CALL_THE_BALL]
    (contact, at_contact), (_, at_ball) = calls[:2]
    assert 1.15 <= at_contact <= 1.25 and 0.65 <= at_ball <= 0.75
    # Case I: neither; the pilot calls the ball on their own.
    assert not {c for c, _ in fly(CalloutEngine(), 1.5 * NM, 0.2 * NM)} & {Call.PADDLES_CONTACT, Call.CALL_THE_BALL}


def test_case_iii_no_call_the_ball_once_the_ball_is_called():
    engine = CalloutEngine(case_iii=True)
    fly(engine, 1.5 * NM, 0.9 * NM)
    engine.heard("ball")
    assert Call.CALL_THE_BALL not in [c for c, _ in fly(engine, 0.9 * NM, 0.2 * NM, t0=100.0)]


def test_case_iii_waits_for_the_ball_call():
    # A little high all the way down: after "call the ball" the LSO waits for the answer before coaching...
    calls = fly(CalloutEngine(case_iii=True), 1.5 * NM, 0.2 * NM, gs=0.5)
    names = [c for c, _ in calls]
    at = names.index(Call.CALL_THE_BALL)
    assert names[at + 1] is Call.LITTLE_HIGH and calls[at + 1][1] <= calls[at][1] - 0.15  # ~5 s at 70 m/s
    # ...stops waiting once the ball is called...
    engine = CalloutEngine(case_iii=True)
    names = [c for c, _ in fly(engine, 1.5 * NM, 0.72 * NM, gs=0.5)]
    assert names[-1] is Call.CALL_THE_BALL
    engine.heard("ball")
    assert fly(engine, 0.72 * NM, 0.6 * NM, gs=0.5, t0=100.0)[0][0] is Call.LITTLE_HIGH
    # ...and doesn't wait to call a big deviation.
    calls = fly(CalloutEngine(case_iii=True), 1.5 * NM, 0.2 * NM, gs=-0.8, gs_rate=-0.1)
    names = [c for c, _ in calls]
    at = names.index(Call.CALL_THE_BALL)
    assert names[at + 1] is Call.POWER and calls[at + 1][1] >= calls[at][1] - 0.05


def test_clara_gets_talked_down_until_the_ball():
    engine = CalloutEngine()
    plain = [c for c, _ in fly(engine, 0.75 * NM, 0.1 * NM)]
    assert Call.ON_GLIDESLOPE not in plain  # steady, no Clara: at most "keep it coming"
    engine = CalloutEngine()
    engine.heard("clara")
    talk = [c for c, _ in fly(engine, 0.75 * NM, 0.3 * NM)]
    assert talk.count(Call.ON_GLIDESLOPE) >= 3  # every few seconds while steady
    engine.heard("ball")
    assert Call.ON_GLIDESLOPE not in [c for c, _ in fly(engine, 0.3 * NM, 0.1 * NM, t0=100.0)]


def test_case_iii_from_night_and_weather():
    assert is_case_iii(True, None)
    assert not is_case_iii(False, None)
    low = {"clouds": {"preset": "Preset20", "base_m": 250}, "visibility_m": 80000}
    assert is_case_iii(False, low)
    assert not is_case_iii(False, {**low, "clouds": {"preset": "Preset2", "base_m": 250}})  # scattered: no ceiling
    assert not is_case_iii(False, {**low, "clouds": {"preset": "Preset20", "base_m": 900}})
    assert is_case_iii(False, {"clouds": {}, "visibility_m": 80000, "fog": {"visibility_m": 1500}})
    assert is_case_iii(False, {"clouds": {"density": 8, "base_m": 200}})  # legacy clouds


def test_the_pilots_call_described():
    assert describe(parse("three zero five hornet ball five point two")) == "305, Hornet ball, 5.2"
    assert describe(parse("three zero five clara")) == "305, Clara"
    assert describe(parse("paddles")) == "Paddles"


class Said:
    def __init__(self):
        self.said = []

    async def start(self): ...

    async def say(self, call, clip, radio, issued_at):
        self.said.append((call, clip.text))

    async def close(self): ...


def test_roger_ball_with_the_wind_over_the_deck():
    """Steaming north at 15 kt into a 10 kt northerly: 25 kt down the axial deck, which is 9 degrees starboard of
    the angled deck: "Roger ball, 25 knots, axial."."""
    kt = 1852 / 3600
    plain = Clip("Roger ball.", [b"x"])
    wind = {(k, a): Clip(f"Roger ball, {k} knots" + (", axial." if a else "."), [b"x"]) for k in range(3, 61)
            for a in (False, True)}
    clips = ClipLibrary({Call.ROGER_BALL: [plain]}, "test", wind=wind, calm=Clip("Roger ball, winds calm.", [b"x"]))
    sink = Said()
    north = WindProfile(((10.0, 0.0, -10 * kt),))
    callouts = LiveCallouts(CalloutSettings(enabled=True), clips, sink, wind_for=lambda carrier: north)
    carrier = ObjectTrack(1, {"Name": "CVN_75", "Pilot": "CVN-75 Harry S. Truman"})
    carrier.samples = [Sample(t, Transform(u=0.0, v=15 * kt * t, alt=0.0, heading=0.0), None) for t in (0.0, 5.0, 10.0)]
    jet = ObjectTrack(2, {"Name": "FA-18C_hornet", "Pilot": "Wrycu"})

    async def run():
        callouts._live = {(1, 2): (carrier, jet, 10.0, 1300.0)}
        callouts.on_heard(Heard(parse("three zero five hornet ball five point two"), "Wrycu", 0, 127.5e6, 0.0))
        await asyncio.sleep(0)

    asyncio.run(run())
    assert sink.said == [(Call.ROGER_BALL, "Roger ball, 25 knots, axial.")]
    assert [c.to_dict().get("text") for c in callouts.made] == ["305, Hornet ball, 5.2", "Roger ball, 25 knots, axial"]
    # Without the hook's wind: plain "Roger ball."
    callouts.wind_for = lambda carrier: None
    callouts._answered.clear()
    asyncio.run(run())
    assert sink.said[-1] == (Call.ROGER_BALL, "Roger ball.")
    # The ship stopped in still air: "Roger ball, winds calm."
    callouts.wind_for = lambda carrier: WindProfile(((10.0, 0.0, 0.0),))
    carrier.samples = [Sample(t, Transform(u=0.0, v=0.0, alt=0.0, heading=0.0), None) for t in (0.0, 5.0, 10.0)]
    callouts._answered.clear()
    asyncio.run(run())
    assert sink.said[-1] == (Call.ROGER_BALL, "Roger ball, winds calm.")
    assert callouts.made[-1].to_dict()["text"] == "Roger ball, winds calm"
