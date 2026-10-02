"""Carrier and aircraft reference data.

Ported from DCS-gRPC/lso `src/data.rs` (https://github.com/DCS-gRPC/lso), which
extracted connector positions via ModelViewer2. Carrier-local coordinates:
x = starboard, y = up, z = forward, meters from the model origin.
"""

from __future__ import annotations

from dataclasses import dataclass

Point = tuple[float, float]  # (x starboard, z forward)


@dataclass(frozen=True, slots=True)
class CarrierInfo:
    name: str
    # Counter-clockwise offset from the ship's heading (BRC) to the landing area (FB), degrees.
    deck_angle: float
    # Deck height above the model origin, meters.
    deck_altitude: float
    # Arresting wire pendant positions (port, starboard), wire 1 first.
    wires: tuple[tuple[Point, Point], ...]


@dataclass(frozen=True, slots=True)
class AircraftInfo:
    name: str
    # Hook point relative to the aircraft origin: (y up, z forward), meters.
    hook: tuple[float, float]
    # Optimal glideslope, degrees.
    glideslope: float
    # On-speed AOA band (inclusive lower, exclusive upper), degrees.
    on_speed_aoa: tuple[float, float]


NIMITZ = CarrierInfo(
    name="Nimitz",
    deck_angle=9.1359,
    deck_altitude=20.1494,
    wires=(
        ((-17.622131, -112.129128), (18.445099, -106.040421)),
        ((-19.584789, -99.914261), (16.519514, -93.864029)),
        ((-21.578857, -87.524025), (14.471450, -81.399986)),
        ((-23.609934, -74.960480), (12.444860, -68.854492)),
    ),
)

FA18C = AircraftInfo(
    name="FA-18C",
    hook=(-2.240897, -7.237348),
    # DCS's own LSO directs to a 3.6 deg glidepath (DCS Supercarrier Operations Guide, "Inside 3/4 Mile").
    glideslope=3.6,
    # lso's "on speed" band: 7.4 < aoa < 8.8.
    on_speed_aoa=(7.4, 8.8),
)

CARRIERS: dict[str, CarrierInfo] = {
    "CVN_71": NIMITZ,
    "CVN_72": NIMITZ,
    "CVN_73": NIMITZ,
    "CVN_75": NIMITZ,
    "Stennis": NIMITZ,
}

AIRCRAFT: dict[str, AircraftInfo] = {
    "FA-18C_hornet": FA18C,
}
