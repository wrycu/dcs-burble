"""Estimated arresting wire, from where the jet stopped.

An arrested jet stops a fixed distance (the arresting gear's runout) past the wire it caught,
whatever its speed: on FA-18C traps with DCS's own wire known, stop point minus a 90 m runout landed
within 0.8 m of the caught wire every time (wires are 12.5 m apart), at 8 Hz or thinned to 4.8 Hz
(the jet is standing still, so the sample rate hardly matters).

Only from a track recorded on the PC that flew it (the local player's own jet, recognisable by its
recorded AOA): a server's copy of a client's jet is smoothed through the sudden arrestment and
overshoots the stop by about a wire (12 m on a wire-2 trap read as wire 3). A pilot's own track against
the server's carrier (a merged landing) is accurate (that same trap: 0.2 m from wire 2).

Always an *estimate*: DCS's own wire (its LSO grade, or the carrier's wire animation) takes priority
wherever it's known. No estimate is given when the stop point isn't clearly at one wire's runout.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..geometry import DeckFrame

if TYPE_CHECKING:
    from .passes import PassSample

# The stop point must be within this of a wire's runout (half the 12.5 m wire spacing is 6.25 m).
MAX_ERROR_M = 3.5


def estimate_wire(samples: Sequence[PassSample], frame: DeckFrame) -> int | None:
    """The wire (1-4) a trapped jet caught, or None if it can't be told."""
    runout = frame.aircraft.arrest_runout_m
    if runout is None or not frame.carrier.runout_measured or not samples or any(s.aoa_derived for s in samples):
        return None  # unmeasured aircraft or carrier, or not the recording PC's own jet (see above)
    touchdown = next((i for i, s in enumerate(samples) if s.hook_height <= 0.0 and s.along < 60.0), None)
    if touchdown is None:
        return None
    # Farthest forward point after touchdown: where the arrestment stopped the jet (before the
    # wire pulls it back a little and the pilot taxis clear).
    stop = min(s.along for s in samples[touchdown:])
    caught = stop + runout
    errors = [abs(caught - along) for along in frame.wire_along]
    best = min(range(len(errors)), key=errors.__getitem__)
    return best + 1 if errors[best] <= MAX_ERROR_M else None


# -- the wire from a server's copy of the jet (PLAN #25) -------------------------------------------------------
# A server's copy overshoots the stop (see above): on 10 traps with DCS's wire (two pilots, FA-18C and F-14),
# 11.8-14.4 m, mean 12.8, sd about 0.9 (`burble hub wire-check`). The stop point corrected by that names the wire:
# right on all 13 traps with a known wire, within 1.7 m on DCS's 10. A second signal, the next wire forward of
# where the hook first reached deck height, was right on only 8 of 13 (one wire short on every F-14 trap: the
# server's copy is too coarse to see the hook land, and hooks skip wires), so it's measured but not used.
SERVER_OVERSHOOT_M = 12.7
SERVER_MAX_ERROR_M = 4.0


def wire_at_stop(stop_along: float, frame: DeckFrame, own: bool) -> int | None:
    """The wire a trapped jet caught, from the farthest forward point its hook reached: a recording PC's own jet
    (`own`) as `estimate_wire`, a server's copy corrected by SERVER_OVERSHOOT_M. None if the aircraft's runout or
    the carrier's gear isn't measured, or the stop isn't clearly at one wire."""
    runout = frame.aircraft.arrest_runout_m
    if runout is None or not frame.carrier.runout_measured:
        return None
    caught = stop_along + runout + (0.0 if own else SERVER_OVERSHOOT_M)
    errors = [abs(caught - along) for along in frame.wire_along]
    best = min(range(len(errors)), key=errors.__getitem__)
    return best + 1 if errors[best] <= (MAX_ERROR_M if own else SERVER_MAX_ERROR_M) else None


@dataclass(frozen=True, slots=True)
class WireSignals:
    stop_along: float  # the farthest forward point (the server's copy: past the real stop)
    stop_wire: int  # nearest wire to the stop point plus the runout, corrected by SERVER_OVERSHOOT_M
    stop_error_m: float  # how far that corrected point is from that wire
    hook_down_along: float  # where the hook first reached deck height (interpolated)
    hook_wire: int | None  # the next wire forward of that (None: past the last wire)

    @property
    def wire(self) -> int | None:
        """The wire the live welcome names: the corrected stop, when it's close to a wire; else None."""
        return self.stop_wire if abs(self.stop_error_m) <= SERVER_MAX_ERROR_M else None


def wire_signals(samples: Sequence[PassSample], frame: DeckFrame,
                 overshoot_m: float = SERVER_OVERSHOOT_M) -> WireSignals | None:
    """Both signals for a trap, or None if the aircraft's runout isn't known or there's no touchdown."""
    runout = frame.aircraft.arrest_runout_m
    touchdown = next((i for i, s in enumerate(samples) if s.hook_height <= 0.0 and s.along < 60.0), None)
    if runout is None or not touchdown:
        return None
    a, b = samples[touchdown - 1], samples[touchdown]
    down = a.along + a.hook_height / (a.hook_height - b.hook_height) * (b.along - a.along) \
        if a.hook_height > 0.0 else a.along
    stop = min(s.along for s in samples[touchdown:])
    caught = stop + runout + overshoot_m
    wires = frame.wire_along
    nearest = min(range(len(wires)), key=lambda k: abs(caught - wires[k]))
    hook = next((k + 1 for k, along in enumerate(wires) if along < down), None)
    return WireSignals(stop, nearest + 1, caught - wires[nearest], down, hook)
