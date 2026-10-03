"""Live LSO callouts in the server-mode collector.

For every aircraft in a pass (as tracked by `LivePassDetector`), a `LiveEstimator` +
`CalloutEngine` decide calls from the live stream; calls are spoken through a `CallSink`
(normally the persistent SRS client) on the carrier's LSO frequency, and recorded so
they can be uploaded with the pass.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, fields, replace
from collections.abc import Callable
from typing import Protocol

from ..acmi import ObjectTrack, Sample
from ..callouts import CallEvent, CalloutEngine, LiveEstimator, LiveInput, Thresholds
from ..callouts.rules import WAVE_OFFS, WELCOME_WIRE, Call
from ..callouts.voice import Clip, ClipLibrary
from ..grading import Grade
from ..geometry import CarrierPose, DeckFrame, WindProfile
from ..srs import Modulation, Radio, SrsClient

log = logging.getLogger(__name__)

# A call that couldn't be spoken within this long is dropped: late calls are worse than none.
MAX_CALL_AGE_S = 1.5

# Bolter: the hook touched the landing area, and the jet is then this far past the last wire
# still holding at least this fraction of its touchdown speed (an arrestment has taken about
# 30% off by then; a bolter at full power holds or gains speed).
BOLTER_PAST_LAST_WIRE_M = 60.0
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
# On a trap, wait this long for DCS's wire (its LSO grade arrives ~0.3 s after we detect the trap)
# so the welcome can name it; checked every WIRE_POLL_S.
WIRE_WAIT_S = 0.6
WIRE_POLL_S = 0.1


@dataclass(frozen=True, slots=True)
class SrsSettings:
    host: str = "127.0.0.1"
    port: int = 5002
    coalition: int = 2
    name: str = "LSO"


@dataclass(frozen=True, slots=True)
class CalloutSettings:
    """Callout configuration (normally fetched from central; see `from_config`)."""

    enabled: bool = False
    srs: SrsSettings = SrsSettings()
    frequency_mhz: float = 127.5  # default LSO frequency
    modulation: Modulation = Modulation.AM
    # Per-carrier overrides, keyed by the carrier's unit name: (frequency MHz, modulation).
    carriers: dict[str, tuple[float, Modulation]] = field(default_factory=dict)
    calls: frozenset[Call] = frozenset(Call)
    thresholds: Thresholds = Thresholds()

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
        )

    def radio_for(self, carrier_unit: str) -> Radio:
        freq, mod = self.carriers.get(carrier_unit, (self.frequency_mhz, self.modulation))
        return Radio(freq, mod)


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

    async def _connected(self) -> SrsClient:
        if self._client is None or not self._client.connected:
            s = self.settings
            client = SrsClient(s.host, s.port, name=s.name, coalition=s.coalition, radios=tuple(self.radios[:10]))
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
            log.warning("SRS unavailable at session start (%s); will retry on the first call", exc)

    async def say(self, call: Call, clip: Clip, radio: Radio, issued_at: float) -> None:
        if call in WAVE_OFFS and self._current is not None:
            self._current.cancel()
        async with self._lock:
            if call not in WAVE_OFFS and time.monotonic() - issued_at > MAX_CALL_AGE_S:
                log.info("dropped stale call %r", call.value)
                return
            try:
                client = await self._connected()
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
        if self._client is not None:
            await self._client.close()


def _gear(plane: ObjectTrack) -> float | None:
    """Landing gear position from Tacview's `LandingGear` property (only exported for some aircraft,
    in practice the recording player's own)."""
    try:
        return float(plane.props["LandingGear"])
    except (KeyError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class MadeCall:
    aircraft_id: int
    time: float  # sim time
    along: float  # meters short of the aim point
    call: Call

    def to_dict(self) -> dict:
        return {"time": self.time, "along": round(self.along, 1), "call": self.call.value}


class LiveCallouts:
    """Plugged into `LivePassDetector` as its sample listener."""

    def __init__(self, settings: CalloutSettings, clips: ClipLibrary, sink: CallSink,
                 wind_for: Callable[[str], WindProfile | None] | None = None,
                 wire_for: Callable[[int, float], int | None] | None = None) -> None:
        self.settings = settings
        self.wind_for = wind_for  # the mission's wind at a carrier (by unit name), from the hook
        # DCS's wire for an aircraft (Tacview id) since a mission time, from the hook, if known yet.
        self.wire_for = wire_for
        # Our grade of a pass in progress (carrier id, aircraft id), for the welcome; set by the collector.
        self.grade_for: Callable[[int, int], Grade | None] | None = None
        # Is another aircraft in the landing area (carrier id, pose, frame, this aircraft's id, time)?
        self.deck_foul: Callable[[int, CarrierPose, DeckFrame, int, float], bool] | None = None
        # A pilot's side number at a mission time (from the hook), to say before calls when the groove is busy.
        self.side_number_for: Callable[[str | None, float], str | None] | None = None
        self._groove_seen: dict[tuple[int, int], float] = {}  # (carrier, aircraft) -> last time in the groove
        self.clips = clips
        self.sink = sink
        self._engines: dict[tuple[int, int], tuple[LiveEstimator, CalloutEngine]] = {}
        self.made: list[MadeCall] = []
        self._tasks: set[asyncio.Task] = set()
        # Per (carrier, aircraft): recent (time, along, lateral) and the deck-relative speed at touchdown.
        self._track: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        self._touchdown_speed: dict[tuple[int, int], float] = {}
        self._outcome_called: set[tuple[int, int]] = set()  # bolter or trap already called

    def on_sample(self, carrier: ObjectTrack, plane: ObjectTrack, pose: CarrierPose, sample: Sample,
                  frame: DeckFrame) -> CallEvent | None:
        key = (carrier.id, plane.id)
        entry = self._engines.get(key)
        if entry is None:
            entry = self._engines[key] = (LiveEstimator(frame.aircraft.glideslope),
                                          CalloutEngine(self.settings.thresholds, self.clips.durations()))
        estimator, engine = entry
        t = sample.transform
        pos = frame.position(pose, t)
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
        outcome = self._outcome(key, sample.time, pos, frame)
        if outcome is Call.TRAPPED and engine.waved_off:
            outcome = Call.TRAPPED_WAVED_OFF  # landed through our wave-off: a saltier welcome
        if outcome is not None:
            event = CallEvent(sample.time, pos.along, outcome, state)
        if event is None or event.call not in self.settings.calls or event.call not in self.clips:
            return None
        if event.call in WELCOME_WIRE:
            # Graded now, while the pass is still being tracked (the welcome may wait for the wire).
            grade = self.grade_for(carrier.id, plane.id) if self.grade_for is not None else None
            welcome = self._welcome(carrier, plane, event, grade)  # may wait briefly for DCS's wire
        else:
            # Decided now (the groove may have changed by the time the call is spoken).
            side_number = self._side_number(carrier.id, plane, event.time) if event.call not in (Call.BOLTER,) else None
            welcome = self._say(carrier, plane, event.call, event, side_number=side_number)
        task = asyncio.get_running_loop().create_task(welcome)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return event

    async def _welcome(self, carrier: ObjectTrack, plane: ObjectTrack, event: CallEvent, grade: Grade | None) -> None:
        """The trap welcome (plain or salty), naming the wire if DCS reports it within WIRE_WAIT_S, and
        complimenting the landing only if we grade it OK or better."""
        wire = None
        if self.wire_for is not None:
            since = event.time - 20.0  # DCS's events for this landing come after touchdown
            deadline = time.monotonic() + WIRE_WAIT_S
            wire = self.wire_for(plane.id, since)
            while wire is None and time.monotonic() < deadline:
                await asyncio.sleep(WIRE_POLL_S)
                wire = self.wire_for(plane.id, since)
        call = WELCOME_WIRE[event.call].get(wire, event.call) if wire is not None else event.call
        await self._say(carrier, plane, call if call in self.clips else event.call, event,
                        praise=grade in PRAISE_GRADES)

    def _side_number(self, carrier_id: int, plane: ObjectTrack, now: float) -> str | None:
        """The pilot's side number, if another aircraft is in this carrier's groove too."""
        if self.side_number_for is None or not self._groove_busy(carrier_id, plane.id, now):
            return None
        return self.side_number_for(plane.pilot, now)

    async def _say(self, carrier: ObjectTrack, plane: ObjectTrack, call: Call, event: CallEvent,
                   praise: bool = False, side_number: str | None = None) -> None:
        self.made.append(MadeCall(plane.id, event.time, event.along, call))
        radio = self.settings.radio_for(carrier.pilot)
        clip = self.clips.with_side_number(self.clips.pick(call, praise), side_number)  # whose call, if busy
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

    def calls_for(self, aircraft_id: int, start: float, end: float) -> list[dict]:
        if self.made:
            newest = self.made[-1].time
            self.made = [c for c in self.made if newest - c.time <= KEEP_CALLS_S]
        return [c.to_dict() for c in self.made if c.aircraft_id == aircraft_id and start <= c.time <= end]

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
