"""Angle of attack from motion, for aircraft whose recording has no AOA (everyone but the
recording PC's own player: a dedicated server records no AOA at all).

AOA is the angle between the nose and the airflow in the aircraft's own vertical plane. The
airflow is the velocity over the ground minus the wind, and the velocity at a sample is the
derivative of a smooth curve through it and its two neighbours. Checked against real AOA on
dedicated-server recordings (4.8 Hz): 0.12 deg RMS, 99% of samples within 0.5 deg.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Vec3 = tuple[float, float, float]  # east, north, up (m or m/s)


@dataclass(frozen=True, slots=True)
class WindProfile:
    """The mission's wind at a carrier, by altitude: where the air is moving to, in m/s."""

    levels: tuple[tuple[float, float, float], ...]  # (altitude m, east, north), ascending altitude
    turbulence: float | None = None  # gusts along the glide path, RMS m/s (the hook's measure), when known

    def at(self, alt: float) -> tuple[float, float]:
        """(east, north) at `alt`, interpolated between levels (constant beyond them)."""
        levels = self.levels
        if not levels:
            return 0.0, 0.0
        if alt <= levels[0][0]:
            return levels[0][1], levels[0][2]
        for (a0, e0, n0), (a1, e1, n1) in zip(levels, levels[1:]):
            if alt <= a1:
                f = (alt - a0) / (a1 - a0) if a1 > a0 else 0.0
                return e0 + f * (e1 - e0), n0 + f * (n1 - n0)
        return levels[-1][1], levels[-1][2]

    def to_dict(self) -> dict:
        out: dict = {"levels": [{"alt": a, "east": e, "north": n} for a, e, n in self.levels]}
        if self.turbulence is not None:
            out["turbulence"] = self.turbulence
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> WindProfile | None:
        if not data or not data.get("levels"):
            return None
        levels = sorted((float(x["alt"]), float(x["east"]), float(x["north"])) for x in data["levels"])
        turbulence = data.get("turbulence")
        return cls(tuple(levels), float(turbulence) if turbulence is not None else None)


@dataclass(frozen=True, slots=True)
class DeckWind:
    """The wind a pilot landed in: over the angled deck (the mission's wind less the carrier's own motion), and
    the wind itself near the water. Directions are where the wind comes from, in degrees on the map grid."""

    speed: float  # over the deck, m/s
    off_axis: float  # degrees the deck wind comes from off the angled deck's axis (+ starboard, - port)
    wind_speed: float  # the mission's wind near the water, m/s
    wind_from: float
    turbulence: float | None = None  # gusts along the glide path, RMS m/s (WindProfile.turbulence)

    @property
    def crosswind(self) -> float:
        """Across the angled deck, m/s (+ from starboard)."""
        return self.speed * math.sin(math.radians(self.off_axis))

    @property
    def headwind(self) -> float:
        """Down the angled deck, m/s."""
        return self.speed * math.cos(math.radians(self.off_axis))

    @classmethod
    def at(cls, wind: WindProfile, ship_velocity: tuple[float, float], heading: float, deck_angle: float,
           alt: float = 20.0) -> DeckWind:
        """`ship_velocity`: the carrier's (east, north) m/s; `heading`: its heading (map grid), whose angled deck
        points `deck_angle` degrees to port; `alt`: where the wind is taken (about the flight deck)."""
        we, wn = wind.at(alt)
        re, rn = we - ship_velocity[0], wn - ship_velocity[1]  # the air, moving over the ship
        speed = math.hypot(re, rn)
        from_deg = math.degrees(math.atan2(-re, -rn)) % 360.0
        off = (from_deg - (heading - deck_angle) + 180.0) % 360.0 - 180.0
        return cls(speed, off if speed > 0.05 else 0.0, math.hypot(we, wn),
                   math.degrees(math.atan2(-we, -wn)) % 360.0, wind.turbulence)


def centred_velocity(t0: float, p0: Vec3, t1: float, p1: Vec3, t2: float, p2: Vec3) -> Vec3 | None:
    """Velocity at t1: derivative of the parabola through three samples (handles uneven spacing)."""
    h1, h2 = t1 - t0, t2 - t1
    if h1 <= 0 or h2 <= 0:
        return None
    w0, w1, w2 = -h2 / (h1 * (h1 + h2)), (h2 - h1) / (h1 * h2), h1 / (h2 * (h1 + h2))
    return (w0 * p0[0] + w1 * p1[0] + w2 * p2[0],
            w0 * p0[1] + w1 * p1[1] + w2 * p2[1],
            w0 * p0[2] + w1 * p1[2] + w2 * p2[2])


def body_aoa(velocity: Vec3, heading: float | None, pitch: float, roll: float) -> float | None:
    """AOA in degrees of an air-relative `velocity` (east, north, up) for an aircraft at the given
    attitude (degrees; heading in the same grid frame as the velocity). Without a heading, falls back
    to pitch minus flight path angle (exact only wings level)."""
    ve, vn, vu = velocity
    if heading is None:
        horizontal = math.hypot(ve, vn)
        if horizontal <= 0:
            return None
        return pitch - math.degrees(math.atan2(vu, horizontal))
    psi, theta, phi = math.radians(heading), math.radians(pitch), math.radians(roll)
    vd = -vu
    # Body axes in north-east-down: x out of the nose, z out of the belly.
    x = (math.cos(theta) * math.cos(psi), math.cos(theta) * math.sin(psi), -math.sin(theta))
    z = (math.cos(phi) * math.sin(theta) * math.cos(psi) + math.sin(phi) * math.sin(psi),
         math.cos(phi) * math.sin(theta) * math.sin(psi) - math.sin(phi) * math.cos(psi),
         math.cos(phi) * math.cos(theta))
    forward = vn * x[0] + ve * x[1] + vd * x[2]
    down = vn * z[0] + ve * z[1] + vd * z[2]
    if forward <= 0:
        return None
    return math.degrees(math.atan2(down, forward))


def air_velocity(ground: Vec3, wind: tuple[float, float]) -> Vec3:
    return ground[0] - wind[0], ground[1] - wind[1], ground[2]
