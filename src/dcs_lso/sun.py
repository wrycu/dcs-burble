"""Day or night: the sun's elevation at the carrier when a pass was flown.

The time comes from the recording: Tacview's `ReferenceTime` is the mission's start date and time in UTC
(DCS's local mission time converted with the map's time zone; checked on a Syria mission starting at
08:00 local, recorded as 05:00Z), plus the pass's mission time. The position is the carrier's latitude
and longitude from the recording.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from .acmi import Recording

# Night is from sunset to sunrise: the sun's centre this far below the horizon (the standard
# sunrise/sunset altitude, allowing for refraction and the sun's radius).
NIGHT_BELOW_DEG = -0.833


def sun_elevation(lat: float, lon: float, when: datetime) -> float:
    """The sun's elevation above the horizon in degrees (NOAA's approximate solar position, good to a
    fraction of a degree)."""
    when = when.astimezone(UTC)
    day = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hours - 12) / 24)  # fractional year, radians
    eq_time = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))  # minutes
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    solar_minutes = hours * 60 + eq_time + 4 * lon
    hour_angle = math.radians(solar_minutes / 4 - 180)
    phi = math.radians(lat)
    cos_zenith = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def is_night(recording: Recording, object_id: int, time_s: float, start: datetime | None = None) -> bool | None:
    """Was it night at `object_id` (e.g. the carrier) at mission time `time_s`? `start`: when the recording's
    clock starts, if not its ReferenceTime. None if that isn't known or the object has no position."""
    reference = start.isoformat() if start is not None else recording.globals.get("ReferenceTime")
    track = recording.objects.get(object_id)
    if not reference or track is None:
        return None
    try:
        start = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    placed = [s for s in track.samples if s.transform.lat is not None and s.transform.lon is not None]
    if not placed:
        return None
    nearest = min(placed, key=lambda s: abs(s.time - time_s))
    elevation = sun_elevation(nearest.transform.lat, nearest.transform.lon, start + timedelta(seconds=time_s))
    return elevation < NIGHT_BELOW_DEG
