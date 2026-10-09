"""Carrier-relative approach geometry.

All inputs are Tacview transforms in DCS flat-map space: u = east, v = north,
`heading` relative to the map grid (not `yaw`, which is true north).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..acmi import Transform
from .data import AircraftInfo, CarrierInfo


@dataclass(frozen=True, slots=True)
class CarrierPose:
    u: float
    v: float
    alt: float
    heading: float

    @classmethod
    def from_transform(cls, t: Transform) -> CarrierPose:
        return cls(u=t.u or 0.0, v=t.v or 0.0, alt=t.alt or 0.0, heading=t.heading or 0.0)

    def to_local(self, u: float, v: float) -> tuple[float, float]:
        """Map point -> carrier-local (x starboard, z forward)."""
        h = math.radians(self.heading)
        du, dv = u - self.u, v - self.v
        return du * math.cos(h) - dv * math.sin(h), du * math.sin(h) + dv * math.cos(h)


@dataclass(frozen=True, slots=True)
class DeckPosition:
    # Distance of the hook from the aim point along the landing area centerline,
    # meters. Positive = short of (behind) the aim point.
    along: float
    # Offset from the landing area centerline, meters. Positive = right of centerline.
    lateral: float
    # Hook height above the deck, meters.
    hook_height: float


def hook_position(plane: Transform, aircraft: AircraftInfo) -> tuple[float, float, float]:
    """World (u, alt, v) of the hook point. Roll is ignored (small in the groove)."""
    h = math.radians(plane.heading or 0.0)
    p = math.radians(plane.pitch or 0.0)
    hy, hz = aircraft.hook
    fwd = (math.sin(h) * math.cos(p), math.sin(p), math.cos(h) * math.cos(p))
    up = (-math.sin(p) * math.sin(h), math.cos(p), -math.sin(p) * math.cos(h))
    return (
        (plane.u or 0.0) + hz * fwd[0] + hy * up[0],
        (plane.alt or 0.0) + hz * fwd[1] + hy * up[1],
        (plane.v or 0.0) + hz * fwd[2] + hy * up[2],
    )


class DeckFrame:
    """Landing-area frame for one carrier/aircraft combination."""

    def __init__(self, carrier: CarrierInfo, aircraft: AircraftInfo) -> None:
        self.carrier = carrier
        self.aircraft = aircraft
        # Aim point: halfway between wires 2 and 3 (NAVAIR 00-80T-104 4.2.8), as lso does.
        (w2_port, _), (_, w3_stbd) = carrier.wires[1], carrier.wires[2]
        self.aim = ((w2_port[0] + w3_stbd[0]) / 2, (w2_port[1] + w3_stbd[1]) / 2)
        # Landing area axis in carrier-local coordinates (rotated to port by the deck angle).
        a = math.radians(-carrier.deck_angle)
        self.axis = (math.sin(a), math.cos(a))
        self.wire_along = tuple(
            self._along_of((port[0] + stbd[0]) / 2, (port[1] + stbd[1]) / 2)
            for port, stbd in carrier.wires
        )

    def _along_of(self, x: float, z: float) -> float:
        return self.deck_point(x, z)[0]

    def deck_point(self, x: float, z: float) -> tuple[float, float]:
        """A carrier-local point as (along, lateral) in the landing-area frame."""
        rx, rz = self.aim[0] - x, self.aim[1] - z
        return rx * self.axis[0] + rz * self.axis[1], -(rx * self.axis[1] - rz * self.axis[0])

    @property
    def wire_ends(self) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
        """Each wire's port and starboard pendant as (along, lateral), wire 1 first."""
        return tuple((self.deck_point(*port), self.deck_point(*stbd)) for port, stbd in self.carrier.wires)

    def position(self, carrier: CarrierPose, plane: Transform) -> DeckPosition:
        hu, halt, hv = hook_position(plane, self.aircraft)
        x, z = carrier.to_local(hu, hv)
        rx, rz = self.aim[0] - x, self.aim[1] - z
        along = rx * self.axis[0] + rz * self.axis[1]
        lateral = -(rx * self.axis[1] - rz * self.axis[0])
        return DeckPosition(
            along=along,
            lateral=lateral,
            hook_height=halt - carrier.alt - self.carrier.deck_altitude,
        )

    def glideslope_height(self, along: float) -> float:
        """Ideal hook height above deck at `along` meters short of the aim point."""
        return max(along, 0.0) * math.tan(math.radians(self.aircraft.glideslope))
