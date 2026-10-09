"""Write standalone ACMI slices: a time window of a recording, limited to chosen objects.

ACMI stores only what changed, so a slice can't just copy lines from the middle of a
recording. It starts with the header, the global properties and the complete current
state of every included object, then copies the original lines for those objects
unchanged. Times stay as in the source (mission time), so DCS events still line up.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Collection, Iterable
from pathlib import Path

from .parser import AcmiParser, Frame, ObjectState
from .reader import iter_lines

_HEADER_KEYS = ("FileType", "FileVersion")
# Tacview's integrity signature for the original file; it can't be valid for a slice.
_DROPPED_GLOBALS = ("AuthenticationKey",)


def escape(value: str) -> str:
    """Escape a property value: backslashes and commas, and newlines as line continuations."""
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("\n", "\\\n")


def _num(x: float, decimals: int | None = None) -> str:
    text = repr(x) if decimals is None else f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return text[:-2] if text.endswith(".0") else text


def transform_text(state: ObjectState, ref_lon: float, ref_lat: float) -> str | None:
    t = state.transform
    if t.lon is None or t.lat is None:
        return None
    # Longitude/latitude lose a little precision when the reference is added and removed again;
    # write them at well beyond DCS's own precision (7 decimals) to drop the float noise.
    out = [_num(t.lon - ref_lon, 9), _num(t.lat - ref_lat, 9)]
    rest: list[float | None] = [t.alt]
    if any(v is not None for v in (t.roll, t.pitch, t.yaw, t.u, t.v, t.heading)):
        if t.u is not None or t.v is not None or t.heading is not None:
            rest += [t.roll, t.pitch, t.yaw, t.u, t.v, t.heading]
        else:
            rest += [t.roll, t.pitch, t.yaw]
    return "|".join(out + ["" if v is None else _num(v) for v in rest])


def snapshot_line(state: ObjectState, ref_lon: float, ref_lat: float) -> str:
    parts = [format(state.id, "x")]
    if (t := transform_text(state, ref_lon, ref_lat)) is not None:
        parts.append(f"T={t}")
    parts.extend(f"{k}={escape(v)}" for k, v in state.props.items())
    return ",".join(parts)


def _object_id(line: str) -> int | None:
    head = line.split(",", 1)[0]
    if head.startswith("-"):
        head = head[1:]
    try:
        return int(head, 16)
    except ValueError:
        return None


def slice_lines(lines: Iterable[str], start: float, end: float, object_ids: Collection[int]) -> Iterable[str]:
    """Yield the lines (without newlines) of a standalone recording of [start, end]."""
    parser = AcmiParser()
    ids = set(object_ids)
    started = False
    pending_frame: str | None = None
    buffer: str | None = None
    for raw in lines:
        records = parser.feed(raw)  # the parser joins continued lines itself
        text = raw.rstrip("\r\n")
        # Keep our copy of multi-line (continued) records together too.
        buffer = text if buffer is None else buffer + "\n" + text
        if text.endswith("\\") and not text.endswith("\\\\"):
            continue
        line, buffer = buffer, None
        frame = next((r for r in records if isinstance(r, Frame)), None)
        if frame is not None:
            if frame.time > end:
                return
            if not started and frame.time >= start:
                started = True
                yield from (f"{k}={parser.globals[k]}" for k in _HEADER_KEYS if k in parser.globals)
                yield from (f"0,{k}={escape(v)}" for k, v in parser.globals.items()
                            if k not in _HEADER_KEYS and k not in _DROPPED_GLOBALS)
                yield line
                for oid in sorted(ids):
                    if oid in parser.objects:
                        yield snapshot_line(parser.objects[oid], parser.reference_longitude,
                                            parser.reference_latitude)
                continue
            if started:
                pending_frame = line
            continue
        if not started:
            continue
        oid = _object_id(line)
        if oid is None or (oid != 0 and oid not in ids):
            continue
        if oid == 0 and ("Event=" in line or any(f",{k}=" in line for k in _DROPPED_GLOBALS)):
            continue  # events may reference objects that aren't in the slice
        if pending_frame is not None:
            yield pending_frame
            pending_frame = None
        yield line


def write_slice(source: str | Path, out: str | Path, start: float, end: float,
                object_ids: Collection[int]) -> Path:
    """Write [start, end] of `source` for `object_ids` to `out` (`.zip.acmi` is compressed)."""
    out = Path(out)
    text = "\n".join(slice_lines(iter_lines(source), start, end, object_ids)) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.name.endswith(".zip.acmi"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(out.name.removesuffix(".zip.acmi") + ".txt.acmi", text.encode("utf-8"))
        out.write_bytes(buf.getvalue())
    else:
        out.write_text(text, encoding="utf-8")
    return out
