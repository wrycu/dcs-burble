"""Incremental parser for Tacview ACMI 2.x text.

The parser is fed one logical line at a time so the same code serves both
recorded files and the real-time telemetry stream (which carries the same
text). See https://www.tacview.net/documentation/acmi/ for the format.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# Positional layouts of the `T=` property, keyed by the number of fields.
# Index into Transform field order: lon, lat, alt, roll, pitch, yaw, u, v, heading.
_T_LAYOUTS: dict[int, tuple[int, ...]] = {
    3: (0, 1, 2),
    5: (0, 1, 2, 6, 7),
    6: (0, 1, 2, 3, 4, 5),
    9: (0, 1, 2, 3, 4, 5, 6, 7, 8),
}
_T_FIELDS = ("lon", "lat", "alt", "roll", "pitch", "yaw", "u", "v", "heading")


@dataclass(frozen=True, slots=True)
class Transform:
    """Object transform. Angles in degrees, distances in meters.

    `u`/`v` are DCS flat-map coordinates (east/north). `heading` is relative to
    that map grid, while `yaw` is relative to true north; use `heading` for any
    math done in u/v space.
    """

    lon: float | None = None
    lat: float | None = None
    alt: float | None = None
    roll: float | None = None
    pitch: float | None = None
    yaw: float | None = None
    u: float | None = None
    v: float | None = None
    heading: float | None = None


@dataclass(slots=True)
class ObjectState:
    id: int
    transform: Transform = field(default_factory=Transform)
    props: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Frame:
    time: float


@dataclass(frozen=True, slots=True)
class GlobalProperty:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class ObjectUpdate:
    time: float
    id: int
    transform: Transform
    props: dict[str, str]
    # True when this record carried a `T=` property, i.e. a new position sample.
    moved: bool
    # Properties carried by this record only (not the accumulated state).
    changed: dict[str, str]


@dataclass(frozen=True, slots=True)
class ObjectRemoved:
    time: float
    id: int


Record = Frame | GlobalProperty | ObjectUpdate | ObjectRemoved


def _split_unescaped(text: str, sep: str) -> list[str]:
    """Split on `sep`, honouring backslash escapes, and unescape the parts."""
    parts: list[str] = []
    buf: list[str] = []
    it = iter(text)
    for ch in it:
        if ch == "\\":
            nxt = next(it, "")
            buf.append(nxt)
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


class AcmiParser:
    """Stateful ACMI parser. Call `feed()` with raw lines, in order."""

    def __init__(self) -> None:
        self.time = 0.0
        self.globals: dict[str, str] = {}
        self.objects: dict[int, ObjectState] = {}
        self._pending: str | None = None

    @property
    def reference_longitude(self) -> float:
        return float(self.globals.get("ReferenceLongitude", 0.0))

    @property
    def reference_latitude(self) -> float:
        return float(self.globals.get("ReferenceLatitude", 0.0))

    def feed(self, line: str) -> list[Record]:
        line = line.rstrip("\r\n")
        if self._pending is not None:
            line = self._pending + "\n" + line
            self._pending = None
        # A trailing unescaped backslash continues the line (multi-line text values).
        if line.endswith("\\") and not line.endswith("\\\\"):
            self._pending = line[:-1]
            return []

        line = line.lstrip("﻿")
        if not line or line.startswith("//"):
            return []
        if line.startswith("#"):
            self.time = float(line[1:])
            return [Frame(self.time)]
        if line.startswith("-"):
            oid = int(line[1:], 16)
            self.objects.pop(oid, None)
            return [ObjectRemoved(self.time, oid)]
        if line.startswith(("FileType=", "FileVersion=")):
            key, _, value = line.partition("=")
            self.globals[key] = value
            return []

        fields = _split_unescaped(line, ",")
        oid = int(fields[0], 16)
        if oid == 0:
            records: list[Record] = []
            for kv in fields[1:]:
                key, _, value = kv.partition("=")
                self.globals[key] = value
                records.append(GlobalProperty(key, value))
            return records

        state = self.objects.get(oid)
        if state is None:
            state = self.objects[oid] = ObjectState(oid)
        moved = False
        changed: dict[str, str] = {}
        for kv in fields[1:]:
            key, _, value = kv.partition("=")
            if key == "T":
                state.transform = self._apply_transform(state.transform, value)
                moved = True
            else:
                state.props[key] = value
                changed[key] = value
        return [ObjectUpdate(self.time, oid, state.transform, dict(state.props), moved, changed)]

    def _apply_transform(self, current: Transform, value: str) -> Transform:
        parts = value.split("|")
        layout = _T_LAYOUTS.get(len(parts))
        if layout is None:
            raise ValueError(f"unsupported T= layout with {len(parts)} fields: {value!r}")
        updates: dict[str, float] = {}
        for idx, raw in zip(layout, parts):
            if raw == "":
                continue  # empty field means unchanged
            number = float(raw)
            name = _T_FIELDS[idx]
            if name == "lon":
                number += self.reference_longitude
            elif name == "lat":
                number += self.reference_latitude
            updates[name] = number
        return replace(current, **updates)
