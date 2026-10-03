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
