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
    # The ramp (aft edge of the deck), meters short of the aim point along the landing area centerline.
    ramp_along_m: float = 70.0
    # Forward end of the landing area (for "foul deck"), meters along the centerline (negative = past
    # the aim point).
    landing_area_forward_m: float = -170.0
    # Has the aircraft arrest runout (AircraftInfo.arrest_runout_m) been measured on this carrier's
    # arresting gear? If not, no wire is estimated (see detect.wire).
    runout_measured: bool = True


@dataclass(frozen=True, slots=True)
class AircraftInfo:
    name: str
    # Hook point relative to the aircraft origin: (y up, z forward), meters.
    hook: tuple[float, float]
    # Optimal glideslope, degrees.
    glideslope: float
    # On-speed AOA band (inclusive lower, exclusive upper), degrees.
    on_speed_aoa: tuple[float, float]
    # How far past the caught wire DCS's arresting gear stops this aircraft (hook point, meters), for
    # estimating the wire; None where unmeasured. See detect.wire.
    arrest_runout_m: float | None = None


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

# CV-59 Forrestal (in DCS with the F-14 module, usable by everyone): deck angle, deck height and wires
# from lso, which match DCS's CoreMods/aircraft/F14/Entry/CV-59-Forrestal_RunwaysAndRoutes.lua (landing
# strip azimuth 350.58 = 9.42 deg, deck at 18.46 m). Not yet checked against a flight:
# - the ramp: DCS gives no ramp position. Its ICLS localizer (at the stern) sits 15.5 m further forward
#   than the Nimitz's (-137.5 vs -153.0 m) while its aim point is 12.1 m further forward, so the ramp is
#   about 3.5 m closer to the aim point than the Nimitz's 70 m (which leaves the same 51 m from the ramp
#   to wire 1 as on the Nimitz);
# - the forward end of the landing area: from DCS's landing strip (centred on its start point, which
#   gives the Nimitz's 170 m too);
# - the arrest runout hasn't been measured on its gear, so no wire estimate (DCS's own wire still shows).
FORRESTAL = CarrierInfo(
    name="Forrestal",
    deck_angle=9.42,
    deck_altitude=18.46,
    wires=(
        ((-17.749493, -96.792412), (17.089462, -90.162186)),
        ((-19.516848, -87.192558), (15.311986, -80.510368)),
        ((-21.246920, -76.618980), (13.582755, -69.941109)),
        ((-23.128010, -66.396812), (11.704433, -59.733154)),
    ),
    ramp_along_m=66.5,
    landing_area_forward_m=-136.0,
    runout_measured=False,
)

FA18C = AircraftInfo(
    name="FA-18C",
    hook=(-2.240897, -7.237348),
    # DCS's own LSO directs to a 3.6 deg glidepath (DCS Supercarrier Operations Guide, "Inside 3/4 Mile").
    glideslope=3.6,
    # lso's "on speed" band: 7.4 < aoa < 8.8.
    on_speed_aoa=(7.4, 8.8),
    # Measured on six DCS 2.9 traps with DCS's own wire known (wires 1-3, 61-70 m/s): 89.3-90.2 m.
    arrest_runout_m=90.1,
)

CARRIERS: dict[str, CarrierInfo] = {
    "CVN_71": NIMITZ,
    "CVN_72": NIMITZ,
    "CVN_73": NIMITZ,
    "CVN_75": NIMITZ,
    "Stennis": NIMITZ,
    "Forrestal": FORRESTAL,
}

AIRCRAFT: dict[str, AircraftInfo] = {
    "FA-18C_hornet": FA18C,
}
