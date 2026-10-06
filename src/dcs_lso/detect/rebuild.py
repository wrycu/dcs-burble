"""The carrier, rebuilt from a trapped jet's own track (PLAN #30): for pilot hook uploads from servers that don't
share other objects with clients, where nothing records the carrier itself.

After an arrestment the jet sits on the angled deck, moving with the ship:
- the ship's velocity (speed and course) is the jet's once it has stopped;
- the ship's heading is that course (ships don't crab); for a ship standing still, the jet's heading down the
  angled deck during the rollout plus the deck angle;
- where the jet stopped is a fixed runout past the wire it caught: with DCS's wire (carrier comms) that places
  the ship exactly; without it, the target wire is assumed (ASSUMED_WIRE: up to two wires, 25 m, out);
- the deck height is where the jet's hook sits.
The ship is then run back at that velocity over the approach (recoveries are flown on a steady course), and
graded against as if it had been recorded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..acmi import ObjectTrack, Sample, Transform
from ..geometry import DeckFrame
from ..geometry.data import AircraftInfo, CarrierInfo
from ..geometry.deck import hook_position

ASSUMED_WIRE = 3  # the target wire
SAMPLE_EVERY_S = 0.1
LANDING_SPEED_MS = 45.0  # an arrestment starts above this ground speed...
SHIP_MAX_MS = 22.0  # ...and ends below it (the ship's speed, plus a little) within
ARREST_WINDOW_S = 3.5
STILL_S = 1.0  # then moves with the ship at least this long
STILL_SPREAD_MS = 2.0  # velocity spread while moving with the ship (the wire pulls the jet back a little)
ROLLOUT_MIN_MS = 5.0  # rollout samples: at least this fast relative to the ship
SETTLED_MAX_S = 10.0  # use up to this long sitting on deck after the stop...
TAXI_MS = 3.0  # ...until the jet moves off (taxiing) relative to the ship
HEADING_STEP_DEG, HEADING_SEARCH_STEPS = 0.02, 200  # heading search: +-4 degrees around the first guess
# Where the jet sits when stopped, against the real carrier (measured on own-jet traps with the carrier recorded):
STOP_LATERAL_M = -2.2  # mean of six traps: -4.7 to +0.1
STOP_HOOK_HEIGHT_M = -0.24  # mean of six traps: -0.36 to -0.11


@dataclass(frozen=True, slots=True)
class RebuiltCarrier:
    samples: list[Sample]
    heading: float  # map grid, degrees
    speed_ms: float
    wire: int  # placed at this wire's runout
    wire_from_dcs: bool  # else assumed (ASSUMED_WIRE)
    stop_time: float
    rollout_heading_error: float | None  # unused (kept for the check script)


def _velocities(samples: list[Sample], half_window_s: float = 0.15) -> list[tuple[float, float] | None]:
    out: list[tuple[float, float] | None] = []
    j0 = j1 = 0
    n = len(samples)
    for i, s in enumerate(samples):
        while j0 < i and s.time - samples[j0 + 1].time >= half_window_s:
            j0 += 1
        j1 = max(j1, i)
        while j1 < n - 1 and samples[j1].time - s.time < half_window_s:
            j1 += 1
        a, b = samples[j0], samples[j1]
        dt = b.time - a.time
        if dt <= 0 or a.transform.u is None or b.transform.u is None:
            out.append(None)
            continue
        out.append(((b.transform.u - a.transform.u) / dt, (b.transform.v - a.transform.v) / dt))
    return out


def _still(samples: list[Sample], vel: list, i: int) -> tuple[int, float, float] | None:
    """From sample `i` (or within a couple of seconds after): moving steadily (with the ship) for STILL_S? As
    (first sample, mean velocity)."""
    t_end = samples[i].time + 2.0
    while i < len(samples) and samples[i].time <= t_end:
        window = [vel[j] for j in range(i, len(samples))
                  if samples[j].time - samples[i].time <= STILL_S and vel[j] is not None]
        if len(window) >= 3 and samples[-1].time - samples[i].time >= STILL_S:
            mu = sum(v[0] for v in window) / len(window)
            mv = sum(v[1] for v in window) / len(window)
            if math.hypot(mu, mv) <= SHIP_MAX_MS and \
                    max(math.hypot(v[0] - mu, v[1] - mv) for v in window) <= STILL_SPREAD_MS:
                return i, mu, mv
        i += 1
    return None


def rebuild_carrier(plane: ObjectTrack, carrier: CarrierInfo, aircraft: AircraftInfo,
                    wire: int | None = None) -> RebuiltCarrier | None:
    """The carrier `plane` trapped on, or None if its track shows no arrestment and stop on a moving deck.
    `wire`: DCS's wire, if known."""
    samples = [s for s in plane.samples if s.transform.u is not None and s.transform.v is not None]
    if len(samples) < 20:
        return None
    vel = _velocities(samples)
    speed = [math.hypot(*v) if v else None for v in vel]

    # The arrestment: from landing speed down to the ship's within ARREST_WINDOW_S, then moving with the ship.
    found = None
    k = 0
    for i, s in enumerate(samples):
        if speed[i] is None or speed[i] < LANDING_SPEED_MS:
            continue
        k = max(k, i)
        while k < len(samples) - 1 and samples[k].time - s.time < ARREST_WINDOW_S:
            k += 1
        if speed[k] is None or speed[k] > SHIP_MAX_MS:
            continue
        still = _still(samples, vel, k)
        if still is not None:
            # The rollout starts where the wire takes hold: the fastest point before the jet slows.
            top = max((j for j in range(i, k + 1) if speed[j] is not None), key=lambda j: speed[j])
            found = (top, *still)
            break
    if found is None:
        return None
    start, still, qu, qv = found

    # The deck axis: during the rollout the jet slides straight down the angled deck, so its velocities lie on a
    # line (the ship's velocity plus some speed along the axis): the line's direction is the axis.
    points = [vel[i] for i in range(start, still) if vel[i] is not None
              and math.hypot(vel[i][0] - qu, vel[i][1] - qv) > ROLLOUT_MIN_MS]
    if len(points) < 5:
        return None
    mu = sum(p[0] for p in points) / len(points)
    mv = sum(p[1] for p in points) / len(points)
    suu = sum((p[0] - mu) ** 2 for p in points)
    svv = sum((p[1] - mv) ** 2 for p in points)
    suv = sum((p[0] - mu) * (p[1] - mv) for p in points)
    angle = 0.5 * math.atan2(2 * suv, suu - svv)  # principal direction, from the u axis
    au, av = math.cos(angle), math.sin(angle)
    if au * (points[0][0] - qu) + av * (points[0][1] - qv) < 0:
        au, av = -au, -av  # pointing the way the jet rolled out: forward down the angled deck
    axis_heading = math.degrees(math.atan2(au, av)) % 360.0
    heading = (axis_heading + carrier.deck_angle) % 360.0
    # Refine: from touchdown until the pilot taxis, the jet stays on the deck's centerline, so its position across
    # the angled deck, in the ship's frame, is constant. On the map that position drifts at the ship's speed times
    # sin(deck angle). For each heading near the first guess: fit that drift (a line in time) and keep the heading
    # whose fit is best; its slope gives the ship's speed.
    end = still
    while end + 1 < len(samples) and samples[end + 1].time - samples[still].time <= SETTLED_MAX_S \
            and (vel[end + 1] is None or math.hypot(vel[end + 1][0] - qu, vel[end + 1][1] - qv) <= TAXI_MS):
        end += 1
    track = [(x.time - samples[still].time, x.transform.u, x.transform.v) for x in samples[start:end + 1]]
    sin_d = math.sin(math.radians(carrier.deck_angle))
    best = None
    for step in range(-HEADING_SEARCH_STEPS, HEADING_SEARCH_STEPS + 1):
        h = heading + step * HEADING_STEP_DEG
        n = math.radians(h - carrier.deck_angle + 90.0)  # across the angled deck
        nu, nv = math.sin(n), math.cos(n)
        ys = [u * nu + v * nv for _, u, v in track]
        ts = [t for t, _, _ in track]
        mt, my = sum(ts) / len(ts), sum(ys) / len(ys)
        stt = sum((t - mt) ** 2 for t in ts)
        slope = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / stt if stt > 0 else 0.0
        err = sum((y - my - slope * (t - mt)) ** 2 for t, y in zip(ts, ys))
        if best is None or err < best[0]:
            best = (err, h, slope)
    _, heading, slope = best
    heading %= 360.0
    ship_speed = max(0.0, slope / sin_d)
    vu, vv = ship_speed * math.sin(math.radians(heading)), ship_speed * math.cos(math.radians(heading))
    au, av = math.sin(math.radians(heading - carrier.deck_angle)), math.cos(math.radians(heading - carrier.deck_angle))
    check = None

    # The stop: the farthest forward the jet got relative to the ship, down the angled deck.
    fwd = (au, av)
    t0 = samples[still].time
    best, best_proj = still, -math.inf
    for i in range(start, len(samples)):
        s = samples[i]
        if s.time - t0 > STILL_S:
            break
        proj = (s.transform.u - vu * (s.time - t0)) * fwd[0] + (s.transform.v - vv * (s.time - t0)) * fwd[1]
        if proj > best_proj:
            best, best_proj = i, proj
    stop = samples[best]

    # Place the ship: at the stop the hook is the runout past the wire, on the centerline, on the deck.
    frame = DeckFrame(carrier, aircraft)
    runout = aircraft.arrest_runout_m
    if runout is None:
        return None
    caught = wire if wire is not None and 1 <= wire <= len(frame.wire_along) else ASSUMED_WIRE
    along, lateral = frame.wire_along[caught - 1] - runout, STOP_LATERAL_M
    ax0, ax1 = frame.axis
    rx, rz = along * ax0 - lateral * ax1, along * ax1 + lateral * ax0
    x, z = frame.aim[0] - rx, frame.aim[1] - rz
    h = math.radians(heading)
    du, dv = x * math.cos(h) + z * math.sin(h), -x * math.sin(h) + z * math.cos(h)
    hu, halt, hv = hook_position(stop.transform, aircraft)
    u0, v0 = hu - du, hv - dv
    alt = halt - carrier.deck_altitude - STOP_HOOK_HEIGHT_M

    lat, lon = stop.transform.lat, stop.transform.lon
    out = []
    t, end = samples[0].time, samples[-1].time
    while t <= end + 1e-9:
        dt = t - stop.time
        out.append(Sample(t, Transform(lon=lon, lat=lat, alt=alt, roll=0.0, pitch=0.0, yaw=heading,
                                       u=u0 + vu * dt, v=v0 + vv * dt, heading=heading), None))
        t += SAMPLE_EVERY_S
    return RebuiltCarrier(out, heading, ship_speed, caught, wire is not None, stop.time, check)
