"""Read ACMI recordings from disk (plain `.txt.acmi` or zipped `.zip.acmi`)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .parser import AcmiParser, Frame, ObjectRemoved, ObjectUpdate, Transform


def iter_lines(path: str | Path) -> Iterator[str]:
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".acmi"))
            with zf.open(name) as raw:
                yield from io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            yield from fh


@dataclass(frozen=True, slots=True)
class Sample:
    time: float
    transform: Transform
    # Recorded angle of attack in degrees, when the exporter provides it
    # (in practice only for the recording player's own aircraft).
    aoa: float | None


@dataclass(slots=True)
class ObjectTrack:
    id: int
    props: dict[str, str] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    removed_at: float | None = None
    # Tacview reuses an object's id once the object is gone (a player's respawn often gets the id of their
    # last jet): each earlier object with this id, as (index of its last sample + 1, its props, removed at).
    # `samples` holds them all; the current object's start after the last of these.
    earlier: list[tuple[int, dict[str, str], float | None]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.props.get("Name", "")

    @property
    def pilot(self) -> str:
        return self.props.get("Pilot", "")

    @property
    def tags(self) -> set[str]:
        return set(filter(None, self.props.get("Type", "").split("+")))

    def lives(self) -> list[ObjectTrack]:
        """Each object that had this id, oldest first (just this track if the id wasn't reused)."""
        if not self.earlier:
            return [self]
        out, start = [], 0
        for end, props, removed_at in self.earlier:
            out.append(ObjectTrack(self.id, props, self.samples[start:end], removed_at))
            start = end
        out.append(ObjectTrack(self.id, self.props, self.samples[start:], self.removed_at))
        return out


@dataclass(slots=True)
class Recording:
    globals: dict[str, str]
    objects: dict[int, ObjectTrack]
    # Time of the first `#` frame line (objects declared before it carry time 0).
    first_frame: float | None = None


def load_recording(path: str | Path) -> Recording:
    """Parse a whole recording into per-object sample histories."""
    parser = AcmiParser()
    tracks: dict[int, ObjectTrack] = {}
    first_frame: float | None = None
    for line in iter_lines(path):
        for record in parser.feed(line):
            if isinstance(record, Frame):
                if first_frame is None:
                    first_frame = record.time
            elif isinstance(record, ObjectUpdate):
                track = tracks.get(record.id)
                if track is None:
                    track = tracks[record.id] = ObjectTrack(record.id)
                elif track.removed_at is not None:  # the id reused by a new object
                    track.earlier.append((len(track.samples), track.props, track.removed_at))
                    track.removed_at = None
                track.props = record.props
                if record.moved:
                    aoa = record.props.get("AOA")
                    track.samples.append(
                        Sample(record.time, record.transform, float(aoa) if aoa else None)
                    )
            elif isinstance(record, ObjectRemoved) and record.id in tracks:
                tracks[record.id].removed_at = record.time
    return Recording(dict(parser.globals), tracks, first_frame)
