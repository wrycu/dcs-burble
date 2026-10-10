"""Live LSO callouts in the server agent.

For every aircraft in a pass (as tracked by `LivePassDetector`), a `LiveEstimator` +
`CalloutEngine` decide calls from the live stream; calls are spoken through a `CallSink`
(normally the persistent SRS client) on the carrier's LSO frequency, and recorded so
they can be uploaded with the pass.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field, fields, replace
from collections.abc import Callable
from typing import Protocol

from ..acmi import ObjectTrack, Sample
from ..callouts import CallEvent, CalloutEngine, LiveEstimator, LiveInput, Thresholds
from ..callouts.heard import describe
from ..callouts.rules import WAVE_OFFS, WELCOME_WIRE, Call
from ..callouts.voice import Clip, ClipLibrary
from ..detect.wire import wire_at_stop
from ..grading import Grade
from ..geometry import CARRIERS, CarrierPose, DeckFrame, DeckWind, WindProfile
from ..srs import Modulation, Radio, SrsClient

log = logging.getLogger(__name__)

MAX_SRS_RADIOS = 10  # radios one SRS client announces
RECONNECT_S = 30.0  # back on the SRS server this soon after losing it
MATCH_FRESH_S = 5.0  # a pass the pilot's call can be from: tracked this recently...
MATCH_MAX_ALONG_M = 4000.0  # ...and no farther out than this (about 2 nm)
# A call that couldn't be spoken within this long is dropped: late calls are worse than none.
MAX_CALL_AGE_S = 1.5

# Bolter: the hook touched the landing area, and the jet is then this far past the last wire
# still holding at least this fraction of its touchdown speed (a bolter at full power holds or gains
# speed). Past where any arrestment stops, as the server sees it: a server's copy of a client's jet lags
# through the arrestment. On a wire-4 trap (2026-10-04) it held full speed until 69 m past wire 4 and
# stopped 98 m past it, so at 60 m that trap was called a bolter.
BOLTER_PAST_LAST_WIRE_M = 110.0
BOLTER_SPEED_RATIO = 0.85
# Trap: after touchdown, the deck-relative speed falls below this fraction of the touchdown
# speed (only an arresting wire stops a jet that quickly; a bolter or touch-and-go keeps it).
TRAP_SPEED_RATIO = 0.5
TOUCHDOWN_HOOK_HEIGHT_M = 0.5
KEEP_CALLS_S = 900.0
# Another aircraft counts as in the groove for this long after its last sample there.
GROOVE_BUSY_S = 2.0
# Welcomes that compliment the landing ("nice trap") only for these grades.
PRAISE_GRADES = frozenset({Grade.PERFECT, Grade.OK})
# ...and now and then (Thresholds.rough_dig_chance) add a dig about the landing for these.
ROUGH_GRADES = frozenset({Grade.NO_GRADE, Grade.CUT})
# The carrier's velocity for the wind over the deck: from its movement over about this long.
SHIP_VELOCITY_S = 10.0
KT = 1852.0 / 3600.0
# On a trap, wait this long for DCS's wire (its LSO grade arrives ~0.3 s after we detect the trap)
# so the welcome can name it; checked every WIRE_POLL_S.
WIRE_WAIT_S = 0.6
WIRE_POLL_S = 0.1
# Without DCS's wire, the welcome names our estimate from where the jet stopped (PLAN #25): stopped once it
# has made no forward progress for STOP_SETTLE_S (a server's copy overshoots, then springs back; an own jet
# is pulled back a little by the wire). Given up (the plain welcome) STOP_WAIT_S after the trap is detected.
STOP_SETTLE_S = 0.6
STOP_WAIT_S = 5.0


@dataclass(frozen=True, slots=True)
class SrsSettings:
    host: str = "127.0.0.1"
    port: int = 5002
    coalition: int = 2
    name: str = "LSO"


@dataclass(frozen=True, slots=True)
class CalloutSettings:
    """Callout configuration (normally fetched from the hub; see `from_config`)."""

    enabled: bool = False
    srs: SrsSettings = SrsSettings()
    frequency_mhz: float = 127.5  # default LSO frequency
    modulation: Modulation = Modulation.AM
    # Per-carrier overrides, keyed by the carrier's unit name: (frequency MHz, modulation).
    carriers: dict[str, tuple[float, Modulation]] = field(default_factory=dict)
    calls: frozenset[Call] = frozenset(Call)
    thresholds: Thresholds = Thresholds()
    # Which clip set to speak with, when the server agent's --voice-dir holds several: its folder name.
    voice: str | None = None
    # Listen to pilots on the LSO frequencies and answer their calls ("Roger ball"); needs --listen-model.
    listen: bool = False
    # Recovery case: "auto" (Case III at night, or with a low ceiling or poor visibility in the mission's
    # weather), or always "I" or "III". Case III: "Paddles contact" coming down final, then "call the ball".
    case: str = "auto"

    @classmethod
    def from_config(cls, config: dict) -> CalloutSettings:
        c = (config or {}).get("callouts") or {}
        srs = c.get("srs") or {}
        known = {f.name for f in fields(Thresholds)}
        overrides = {k: float(v) for k, v in (c.get("thresholds") or {}).items() if k in known}
        return cls(
            enabled=bool(c.get("enabled", False)),
            srs=SrsSettings(host=srs.get("host", "127.0.0.1"), port=int(srs.get("port", 5002)),
                            coalition=int(srs.get("coalition", 2)), name=srs.get("name", "LSO")),
            frequency_mhz=float(c.get("frequency_mhz", 127.5)),
            modulation=Modulation[c.get("modulation", "AM")],
            carriers={name: (float(v["frequency_mhz"]), Modulation[v.get("modulation", "AM")])
                      for name, v in (c.get("carriers") or {}).items()},
            calls=frozenset(Call(x) for x in c["calls"]) if "calls" in c else frozenset(Call),
            thresholds=replace(Thresholds(), **overrides),
            voice=str(c["voice"]) if c.get("voice") else None,
            listen=bool(c.get("listen", False)),
            case=str(c.get("case", "auto")).upper().replace("AUTO", "auto"),
        )

    def radio_for(self, carrier_unit: str, detected: Radio | None = None) -> Radio:
        """Where to make calls for this carrier: its setting in the config, else the frequency set for it
        in the mission (`detected`, from the hook), else the default frequency."""
        if carrier_unit in self.carriers:
            return Radio(*self.carriers[carrier_unit])
        return detected or Radio(self.frequency_mhz, self.modulation)


class CallSink(Protocol):
    async def start(self) -> None: ...

    async def say(self, call: Call, clip: Clip, radio: Radio, issued_at: float) -> None: ...

    async def close(self) -> None: ...


class SrsSink:
    """Speaks calls through one persistent SRS connection. A wave-off cuts off whatever is
    being said; other calls queue, and are dropped if they can no longer be said in time."""

    def __init__(self, settings: SrsSettings, radios: list[Radio]) -> None:
        self.settings = settings
        self.radios = radios
        self._client: SrsClient | None = None
        self._lock = asyncio.Lock()
        self._current: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        # Everything heard on our frequencies (packet, perf_counter time), e.g. for listening to pilots' calls.
        self.on_voice: Callable | None = None

    @property
    def client(self) -> SrsClient | None:
        return self._client

    async def _connected(self) -> SrsClient:
        if self._client is None or not self._client.connected:
            s = self.settings
            client = SrsClient(s.host, s.port, name=s.name, coalition=s.coalition, radios=tuple(self.radios[:10]),
                               on_voice=lambda packet, at: self.on_voice(packet, at) if self.on_voice else None)
            await client.connect()
            log.info("SRS connected: %s:%d as %r on %s", s.host, s.port, s.name,
                     ", ".join(f"{r.frequency_mhz:.3f} {r.modulation.name}" for r in self.radios))
            self._client = client
        return self._client

    async def start(self) -> None:
        """Connect ahead of the first call. SRS clients drop audio from a sender they haven't been
        told about yet (SRS 2.3.8.2 throws a NullReferenceException decoding it), so the LSO must be
        on the server well before it speaks."""
        try:
            async with self._lock:
                await self._connected()
        except OSError as exc:
            log.warning("SRS unavailable at session start (%s); retrying every %.0f s", exc, RECONNECT_S)
        if self._keepalive is None:
            self._keepalive = asyncio.get_running_loop().create_task(self._keep_connected(), name="srs-keepalive")

    async def _keep_connected(self) -> None:
        """Back on the SRS server within RECONNECT_S of losing it (e.g. the server restarted mid-mission): to be
        listed, heard by pilots' clients ahead of the next call, and to hear the pilots."""
        while True:
            await asyncio.sleep(RECONNECT_S)
            if self._client is not None and self._client.connected:
                continue
            try:
                async with self._lock:
                    await self._connected()
            except OSError as exc:
                log.debug("SRS still unavailable: %s", exc)

    async def say(self, call: Call, clip: Clip, radio: Radio, issued_at: float) -> None:
        if call in WAVE_OFFS and self._current is not None:
            self._current.cancel()
        async with self._lock:
            if call not in WAVE_OFFS and time.monotonic() - issued_at > MAX_CALL_AGE_S:
                log.info("dropped stale call %r", call.value)
                return
            try:
                client = await self._connected()
                if radio not in self.radios and len(self.radios) < MAX_SRS_RADIOS:
                    # A frequency found in the mission: announce it before talking on it (SRS clients
                    # mishandle audio from radios they haven't been told about).
                    self.radios.append(radio)
                    await client.set_radios(self.radios)
                    log.info("SRS: now also on %.3f %s", radio.frequency_mhz, radio.modulation.name)
                self._current = asyncio.current_task()
                await client.transmit(clip.frames, radios=(radio,))
            except asyncio.CancelledError:
                if call in WAVE_OFFS:
                    raise
                log.info("call %r cut off", call.value)
            except OSError as exc:
                log.warning("SRS unavailable (%s); call %r not spoken", exc, call.value)
                self._client = None
            finally:
                self._current = None

    async def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None
        if self._client is not None:
            await self._client.close()


def _norm(name: str | None) -> str:
    """A player's name for comparing (SRS's and Tacview's): case and spacing don't matter."""
    return " ".join((name or "").lower().split())


def _gear(plane: ObjectTrack) -> float | None:
    """Landing gear position from Tacview's `LandingGear` property (only exported for some aircraft,
    in practice the recording player's own)."""
    try:
        return float(plane.props["LandingGear"])
    except (KeyError, ValueError):
        return None


@dataclass(slots=True)
class _Stop:
    """Where a trapped jet's hook got farthest forward along the deck (the stop), updated sample by sample."""
    frame: DeckFrame
    own: bool  # the recording PC's own jet (recorded AOA): no server overshoot to correct
    along: float
    at: float  # sim time of the farthest forward sample
    latest: float  # sim time of the latest sample

    def update(self, t: float, along: float) -> None:
        if along < self.along:
            self.along, self.at = along, t
        self.latest = max(self.latest, t)

    @property
    def stopped(self) -> bool:
        return self.latest - self.at >= STOP_SETTLE_S


@dataclass(slots=True)
class MadeCall:
    """A call made on a pass, recorded when decided (so a pass sliced right after still has it); a welcome's
    `call` is filled in with the wire once DCS reports it. Also what the pilot said (`by` "pilot": `call` is
    "ball", "clara" or "paddles" and `text` the call as heard)."""
    aircraft_id: int
    time: float  # sim time
    along: float  # meters short of the aim point
    call: Call | str
    text: str | None = None  # what was said, when it says more than the call (e.g. "Roger ball, 25 knots.")
    by: str = "lso"

    def to_dict(self) -> dict:
        out = {"time": self.time, "along": round(self.along, 1), "call": str(self.call)}
        if self.text:
            out["text"] = self.text
        if self.by != "lso":
            out["by"] = self.by
        return out


class LiveCallouts:
    """Plugged into `LivePassDetector` as its sample listener."""

    def __init__(self, settings: CalloutSettings, clips: ClipLibrary, sink: CallSink,
                 wind_for: Callable[[str], WindProfile | None] | None = None,
                 wire_for: Callable[[int, float], int | None] | None = None) -> None:
        self.settings = settings
        self.wind_for = wind_for  # the mission's wind at a carrier (by unit name), from the hook
        # DCS's wire for an aircraft (Tacview id) since a mission time, from the hook, if known yet.
        self.wire_for = wire_for
        # Our grade of a pass in progress (carrier id, aircraft id), for the welcome; set by the agent.
        self.grade_for: Callable[[int, int], Grade | None] | None = None
        # The frequency set for a carrier (by unit name) in the mission, from the hook.
        self.carrier_radio: Callable[[str], Radio | None] | None = None
        # Is another aircraft in the landing area (carrier id, pose, frame, this aircraft's id, time)?
        self.deck_foul: Callable[[int, CarrierPose, DeckFrame, int, float], bool] | None = None
        # Did this aircraft launch from this carrier earlier (carrier id, aircraft id, before)? For "welcome home".
        self.departed_from: Callable[[int, int, float], bool] | None = None
        # A pilot's side number at a mission time (from the hook), to say before calls when the groove is busy.
        self.side_number_for: Callable[[str | None, float], str | None] | None = None
        # Is it night at this carrier at this mission time? And the mission's weather (the hook's `weather`
        # event). For deciding Case III (`case_iii`).
        self.night_at: Callable[[ObjectTrack, float], bool | None] | None = None
        self.weather: Callable[[], dict | None] | None = None
        self._groove_seen: dict[tuple[int, int], float] = {}  # (carrier, aircraft) -> last time in the groove
        self.clips = clips
        self.sink = sink
        self._engines: dict[tuple[int, int], tuple[LiveEstimator, CalloutEngine]] = {}
        self.made: list[MadeCall] = []
        self._tasks: set[asyncio.Task] = set()
        self._welcomes: dict[int, asyncio.Task] = {}  # aircraft id -> its welcome (which may wait for the wire)
        self._stops: dict[tuple[int, int], _Stop] = {}  # trapped jets, for our wire estimate
        # Per (carrier, aircraft): recent (time, along, lateral) and the deck-relative speed at touchdown.
        self._track: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        self._touchdown_speed: dict[tuple[int, int], float] = {}
        self._outcome_called: set[tuple[int, int]] = set()  # bolter or trap already called
        # Passes in progress, for matching what pilots say on the radio: (carrier, plane, sim time, along).
        self._live: dict[tuple[int, int], tuple[ObjectTrack, ObjectTrack, float, float]] = {}
        self._answered: set[tuple[int, int, Call]] = set()  # e.g. "Roger ball" once a pass

    def on_sample(self, carrier: ObjectTrack, plane: ObjectTrack, pose: CarrierPose, sample: Sample,
                  frame: DeckFrame) -> CallEvent | None:
        key = (carrier.id, plane.id)
        entry = self._engines.get(key)
        if entry is None:
            aircraft = frame.aircraft
            case_iii = self.case_iii(carrier, sample.time)
            entry = self._engines[key] = (
                LiveEstimator(aircraft.glideslope, aoa_offset=aircraft.derived_aoa_offset),
                CalloutEngine(self.settings.thresholds.for_aircraft(aircraft.on_speed_aoa), self.clips.durations(),
                              case_iii=case_iii))
            if case_iii:
                log.info("Case III for %s at %s", plane.pilot or hex(plane.id), carrier.pilot or carrier.name)
        estimator, engine = entry
        t = sample.transform
        pos = frame.position(pose, t)
        self._live[key] = (carrier, plane, sample.time, pos.along)
        heading_error = ((t.heading or 0.0) - (pose.heading - frame.carrier.deck_angle) + 180.0) % 360.0 - 180.0
        wind = self.wind_for(carrier.pilot) if self.wind_for else None
        foul = self.deck_foul(carrier.id, pose, frame, plane.id, sample.time) if self.deck_foul else False
        state = estimator.update(LiveInput(
            time=sample.time, along=pos.along, lateral=pos.lateral, hook_height=pos.hook_height,
            pitch=t.pitch or 0.0, alt=t.alt or 0.0, u=t.u or 0.0, v=t.v or 0.0, aoa=sample.aoa,
            heading_error=heading_error, roll=t.roll or 0.0, gear=_gear(plane), heading=t.heading,
            wind=wind.at(t.alt or 0.0) if wind else (0.0, 0.0), foul_deck=foul))
        event = engine.update(state)
        if engine.in_groove:
            self._groove_seen[key] = sample.time
        else:
            self._groove_seen.pop(key, None)
        if (stop := self._stops.get(key)) is not None:
            stop.update(sample.time, pos.along)
        outcome = self._outcome(key, sample.time, pos, frame)
        if outcome is Call.TRAPPED and engine.waved_off:
            outcome = Call.TRAPPED_WAVED_OFF  # landed through our wave-off: a saltier welcome
        if outcome is not None:
            event = CallEvent(sample.time, pos.along, outcome, state)
        if event is None or event.call not in self.settings.calls or event.call not in self.clips:
            return None
        made = MadeCall(plane.id, event.time, event.along, event.call)
        self.made.append(made)
        if event.call in WELCOME_WIRE:
            # Graded now, while the pass is still being tracked (the welcome may wait for the wire).
            grade = self.grade_for(carrier.id, plane.id) if self.grade_for is not None else None
            stop = self._stops[key] = _Stop(frame, sample.aoa is not None, pos.along, sample.time, sample.time)
            welcome = self._welcome(carrier, plane, event, grade, made, stop)  # may wait for the wire
        else:
            # Decided now (the groove may have changed by the time the call is spoken).
            side_number = self._side_number(carrier.id, plane, event.time) if event.call not in (Call.BOLTER,) else None
            welcome = self._say(carrier, plane, event.call, event, side_number=side_number)
        task = asyncio.get_running_loop().create_task(welcome)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if event.call in WELCOME_WIRE:
            self._welcomes[plane.id] = task
        return event

    async def _welcome(self, carrier: ObjectTrack, plane: ObjectTrack, event: CallEvent, grade: Grade | None,
                       made: MadeCall | None = None, stop: _Stop | None = None) -> None:
        """The trap welcome (plain or salty), naming the wire if DCS reports it within WIRE_WAIT_S, else our
        estimate once the jet has stopped (within STOP_WAIT_S; DCS's wire still wins if it comes meanwhile), and
        complimenting the landing only if we grade it OK or better."""
        since = event.time - 20.0  # DCS's events for this landing come after touchdown

        def dcs_wire() -> int | None:
            return self.wire_for(plane.id, since) if self.wire_for is not None else None

        wire = dcs_wire()
        deadline = time.monotonic() + WIRE_WAIT_S
        while wire is None and self.wire_for is not None and time.monotonic() < deadline:
            await asyncio.sleep(WIRE_POLL_S)
            wire = dcs_wire()
        if wire is None and stop is not None and stop.frame.aircraft.arrest_runout_m is not None \
                and stop.frame.carrier.runout_measured:
            deadline = time.monotonic() + STOP_WAIT_S - WIRE_WAIT_S
            while not stop.stopped and time.monotonic() < deadline:
                await asyncio.sleep(WIRE_POLL_S)
            wire = dcs_wire()
            if wire is None and stop.stopped:
                wire = wire_at_stop(stop.along, stop.frame, stop.own)
                log.info("wire for %s from where it stopped: %s", plane.pilot or hex(plane.id), wire or "not clear")
        call = WELCOME_WIRE[event.call].get(wire, event.call) if wire is not None else event.call
        if made is not None and call in self.clips:
            made.call = call  # e.g. "welcome aboard, two wire"
        # "Welcome home" for a jet back on the carrier it launched from, "welcome aboard" for a visitor.
        home = self.departed_from(carrier.id, plane.id, event.time) if self.departed_from is not None else None
        # A dig about a poor or cut landing, now and then (a trap through a wave-off already gets the salty one).
        dig = (event.call is Call.TRAPPED and grade in ROUGH_GRADES and Call.ROUGH_LANDING in self.clips
               and random.random() < self.settings.thresholds.rough_dig_chance)
        await self._say(carrier, plane, call if call in self.clips else event.call, event,
                        praise=grade in PRAISE_GRADES, home=home, dig=dig)

    def case_iii(self, carrier: ObjectTrack, now: float) -> bool:
        """A Case III recovery: as the config says, or (auto) at night or in the mission's poor weather."""
        if self.settings.case in ("I", "III"):
            return self.settings.case == "III"
        night = self.night_at(carrier, now) if self.night_at is not None else None
        return is_case_iii(bool(night), self.weather() if self.weather is not None else None)

    def deck_wind(self, carrier: ObjectTrack) -> DeckWind | None:
        """The wind over this carrier's angled deck now: the mission's wind (from the hook) less the carrier's
        own motion. None without the hook's wind."""
        wind = self.wind_for(carrier.pilot) if self.wind_for else None
        info = CARRIERS.get(carrier.name)
        if wind is None or info is None or len(carrier.samples) < 2:
            return None
        last = carrier.samples[-1]
        first = next((s for s in reversed(carrier.samples) if last.time - s.time >= SHIP_VELOCITY_S), carrier.samples[0])
        span = last.time - first.time
        if span <= 0:
            return None
        a, b = first.transform, last.transform
        ship = (((b.u or 0.0) - (a.u or 0.0)) / span, ((b.v or 0.0) - (a.v or 0.0)) / span)
        return DeckWind.at(wind, ship, b.heading or 0.0, info.deck_angle)

    def _side_number(self, carrier_id: int, plane: ObjectTrack, now: float) -> str | None:
        """The pilot's side number, if another aircraft is in this carrier's groove too."""
        if self.side_number_for is None or not self._groove_busy(carrier_id, plane.id, now):
            return None
        return self.side_number_for(plane.pilot, now)

    async def _say(self, carrier: ObjectTrack, plane: ObjectTrack, call: Call, event: CallEvent,
                   praise: bool = False, side_number: str | None = None, home: bool | None = None,
                   dig: bool = False) -> None:
        radio = self.settings.radio_for(carrier.pilot, self.carrier_radio(carrier.pilot) if self.carrier_radio else None)
        clip = self.clips.pick(call, praise, home)
        if dig:
            clip = self.clips.followed_by(clip, self.clips.pick(Call.ROUGH_LANDING))
        clip = self.clips.with_side_number(clip, side_number)  # whose call, if busy
        log.info("CALL %s -> %s (%.2f nm, %s): %r", call.value, plane.pilot or hex(plane.id),
                 event.along / 1852, f"{radio.frequency_mhz:.3f} {radio.modulation.name}", clip.text)
        await self.sink.say(call, clip, radio, time.monotonic())

    def _outcome(self, key: tuple[int, int], t: float, pos, frame: DeckFrame) -> Call | None:
        """Once per pass, after touchdown: BOLTER when the jet rolls past the wires at speed, TRAPPED
        when it is stopped quickly by a wire."""
        track = self._track.setdefault(key, [])
        track.append((t, pos.along, pos.lateral))
        del track[:-4]
        if len(track) < 4 or key in self._outcome_called:
            return None
        (t0, a0, l0), (t1, a1, l1) = track[0], track[-1]
        speed = ((a1 - a0) ** 2 + (l1 - l0) ** 2) ** 0.5 / (t1 - t0) if t1 > t0 else 0.0
        on_deck = pos.hook_height < TOUCHDOWN_HOOK_HEIGHT_M and abs(pos.lateral) < 25.0 and -250.0 < pos.along < 40.0
        if on_deck and key not in self._touchdown_speed:
            self._touchdown_speed[key] = speed
        touchdown = self._touchdown_speed.get(key)
        if not touchdown:
            return None
        if pos.along < min(frame.wire_along) - BOLTER_PAST_LAST_WIRE_M and speed >= BOLTER_SPEED_RATIO * touchdown:
            self._outcome_called.add(key)
            return Call.BOLTER
        if on_deck and speed < TRAP_SPEED_RATIO * touchdown:
            self._outcome_called.add(key)
            return Call.TRAPPED
        return None

    def _groove_busy(self, carrier_id: int, aircraft_id: int, now: float) -> bool:
        """Another aircraft is in this carrier's groove too (seen there in the last couple of seconds)."""
        return any(c == carrier_id and a != aircraft_id and now - seen <= GROOVE_BUSY_S
                   for (c, a), seen in self._groove_seen.items())

    def pass_ended(self, carrier_id: int, aircraft_id: int) -> None:
        key = (carrier_id, aircraft_id)
        self._groove_seen.pop(key, None)
        self._engines.pop(key, None)
        self._track.pop(key, None)
        self._touchdown_speed.pop(key, None)
        self._outcome_called.discard(key)
        self._stops.pop(key, None)  # a welcome still waiting keeps its own reference
        self._live.pop(key, None)
        self._answered = {a for a in self._answered if a[:2] != key}

    # -- what pilots say (heard on SRS: agent/listening.py) ---------------------------------------------------

    def on_heard(self, heard) -> None:
        """A pilot's call heard on an LSO frequency: recorded with their pass, and answered on that frequency
        ("Roger ball, 25 knots", "Roger, Clara", "loud and clear"). After "Clara" the LSO talks the jet down
        until the ball is called; a ball call stops a Case III "call the ball"."""
        found = self._match(heard)
        if found is not None:
            key, (carrier, plane, sim_time, along) = found
            if (entry := self._engines.get(key)) is not None:
                entry[1].heard(heard.call.call)
            if (*key, heard.call.call) not in self._answered:  # a call repeated (e.g. no answer yet) once
                self.made.append(MadeCall(plane.id, sim_time, along, heard.call.call, describe(heard.call), "pilot"))
        answer = {"ball": Call.ROGER_BALL, "clara": Call.ROGER_CLARA, "paddles": Call.LOUD_AND_CLEAR}[heard.call.call]
        if answer not in self.settings.calls or answer not in self.clips:
            return
        if found is None and answer is not Call.LOUD_AND_CLEAR:
            log.info("heard %r from %s, but no jet in the groove on %.3f MHz to match it", heard.call.text,
                     heard.speaker or "?", heard.frequency_hz / 1e6)
            return
        if found is not None:
            key, (carrier, plane, sim_time, along) = found
            if (*key, answer) in self._answered:
                return  # said already this pass
            self._answered.add((*key, answer))
            self._answered.add((*key, heard.call.call))
            clip, said = self.clips.pick(answer), None
            if answer is Call.ROGER_BALL and (wind := self.deck_wind(carrier)) is not None:
                if (with_wind := self.clips.roger_ball(wind.speed / KT, wind.off_axis)) is not None:
                    clip = with_wind  # "Roger ball, 25 knots."
                    said = clip.text.rstrip(".")
            self.made.append(MadeCall(plane.id, sim_time, along, answer, said))
            radio = self.settings.radio_for(carrier.pilot, self.carrier_radio(carrier.pilot) if self.carrier_radio else None)
            who = plane.pilot or hex(plane.id)
        else:
            radio = next((r for r in self._radios() if abs(r.frequency_hz - heard.frequency_hz) < 1000.0),
                         Radio(heard.frequency_hz / 1e6))
            who = heard.speaker or "?"
            clip = self.clips.pick(answer)
        log.info("ANSWER %s -> %s (%.3f %s): %r", answer.value, who, radio.frequency_mhz, radio.modulation.name, clip.text)
        task = asyncio.get_running_loop().create_task(self.sink.say(answer, clip, radio, time.monotonic()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _radios(self) -> list[Radio]:
        radios = [Radio(self.settings.frequency_mhz, self.settings.modulation)]
        radios += [Radio(*v) for v in self.settings.carriers.values()]
        return radios

    def _match(self, heard):
        """The pass in progress the call is from: on that frequency, by the speaker's name (their SRS name is
        their DCS name), else the side number they said, else the only jet in a groove there."""
        if not self._live:
            return None
        latest = max(t for _, _, t, _ in self._live.values())
        here = []
        for key, entry in self._live.items():
            carrier, plane, sim_time, along = entry
            radio = self.settings.radio_for(carrier.pilot, self.carrier_radio(carrier.pilot) if self.carrier_radio else None)
            if latest - sim_time <= MATCH_FRESH_S and abs(radio.frequency_hz - heard.frequency_hz) < 1000.0 \
                    and -50.0 <= along <= MATCH_MAX_ALONG_M:
                here.append((key, entry))
        name = _norm(heard.speaker)
        by_name = [x for x in here if name and _norm(x[1][1].pilot) == name]
        if by_name:
            return by_name[0]
        side = heard.call.side_number
        if side and self.side_number_for is not None:
            by_side = [x for x in here if self.side_number_for(x[1][1].pilot, x[1][2]) == side]
            if len(by_side) == 1:
                return by_side[0]
        return here[0] if len(here) == 1 else None

    def calls_for(self, aircraft_id: int, start: float, end: float) -> list[dict]:
        if self.made:
            newest = self.made[-1].time
            self.made = [c for c in self.made if newest - c.time <= KEEP_CALLS_S]
        return [c.to_dict() for c in self.made if c.aircraft_id == aircraft_id and start <= c.time <= end]

    async def settled(self, aircraft_id: int, timeout: float = STOP_WAIT_S + 1.0) -> None:
        """Wait (briefly) for this aircraft's welcome to be decided, so its wire is in the calls (normally long
        done when the pass is sliced; not when a recording is replayed at full speed)."""
        task = self._welcomes.pop(aircraft_id, None)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=timeout)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


# Case III by the weather (when it isn't night): a ceiling below this, or visibility below this.
CASE_III_CEILING_M = 1000 * 0.3048
CASE_III_VISIBILITY_M = 5 * 1852.0
# Cloud layers that make a ceiling: legacy clouds this dense (0-10), or these presets (DCS's broken and overcast
# presets; the lower ones are few or scattered).
CEILING_DENSITY = 7
CEILING_PRESETS = frozenset({f"Preset{n}" for n in range(13, 28)} | {"RainyPreset1", "RainyPreset2", "RainyPreset3"})


def is_case_iii(night: bool, weather: dict | None) -> bool:
    """Case III: at night, or (from the mission's weather, the hook's `weather` event) a ceiling under 1,000 ft
    or visibility under 5 nm (fog or dust included)."""
    if night:
        return True
    if not weather:
        return False
    clouds = weather.get("clouds") or {}
    base = clouds.get("base_m")
    layer = clouds.get("preset") in CEILING_PRESETS or (not clouds.get("preset") and (clouds.get("density") or 0)
                                                         >= CEILING_DENSITY)
    if layer and base is not None and base < CASE_III_CEILING_M:
        return True
    seen = [v for v in (weather.get("visibility_m"), (weather.get("fog") or {}).get("visibility_m"),
                        weather.get("dust_m")) if v]
    return any(v < CASE_III_VISIBILITY_M for v in seen)
