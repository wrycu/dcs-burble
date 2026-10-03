"""Per-pass ACMI slices: the permanent record of a pass, plus a small JSON sidecar.

The slice holds the carrier, the aircraft flying the pass, and any other aircraft that
came within `NEARBY_M` of the carrier, from `LEAD_S` before the pass was detected to
`TAIL_S` after it ended. Grades and trap cards are derived from slices and can always be
rebuilt from them.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .acmi import Recording, load_recording
from .acmi.writer import write_slice
from .detect import CarrierTimeline, PassResult
from .detect.approaches import Approach
from .dcslog import Debrief

NM = 1852.0
LEAD_S = 30.0
TAIL_S = 10.0
# A track report (own jet only, see `detect.approaches`) starts this long before the jet dropped
# below approach height, so it covers the whole pass as the server detects it.
APPROACH_LEAD_S = 45.0
NEARBY_M = 2 * NM
SIDECAR_SCHEMA = 1


def _collector_version() -> str:
    try:
        return version("dcs-lso")
    except PackageNotFoundError:
        return "unknown"


def window(p: PassResult) -> tuple[float, float]:
    return p.start_time - LEAD_S, p.end_time + TAIL_S


def slice_objects(recording: Recording, p: PassResult) -> set[int]:
    start, end = window(p)
    carrier = recording.objects[p.carrier_id]
    timeline = CarrierTimeline(carrier.samples)
    ids = {p.carrier_id, p.aircraft_id}
    for track in recording.objects.values():
        if track.id in ids or "Air" not in track.tags:
            continue
        for s in track.samples:
            if not start <= s.time <= end or s.transform.u is None:
                continue
            pose = timeline.at(s.time)
            if math.hypot(s.transform.u - pose.u, (s.transform.v or 0.0) - pose.v) <= NEARBY_M:
                ids.add(track.id)
                break
    return ids


def first_frame_time(recording: Recording) -> float | None:
    """Sim time of the recording's first sample. `RecordingTime` is the wall-clock time at that
    moment (for live streams, when the connection started), so a pass happened at
    RecordingTime + (pass time - first frame time)."""
    return recording.first_frame


def sidecar(recording: Recording, p: PassResult, source: str | Path, object_ids: set[int]) -> dict:
    start, end = window(p)
    samples = p.samples
    dts = sorted(b.time - a.time for a, b in zip(samples, samples[1:]))
    g = recording.globals
    return {
        "schema": SIDECAR_SCHEMA,
        "kind": "pass",
        "collector_version": _collector_version(),
        "source": Path(source).name,
        "recording": {**{k: g.get(k) for k in ("Title", "RecordingTime", "ReferenceTime", "DataRecorder", "DataSource")},
                      "first_frame_time": first_frame_time(recording)},
        "window": {"start": start, "end": end},
        "objects": sorted(object_ids),
        "pass": {
            "carrier_id": p.carrier_id,
            "carrier_type": p.carrier_type,
            "carrier_unit": recording.objects[p.carrier_id].pilot,
            "aircraft_id": p.aircraft_id,
            "aircraft_type": p.aircraft_type,
            "pilot": p.pilot,
            "outcome": p.outcome.value,
            "start_time": p.start_time,
            "end_time": p.end_time,
            "sample_rate_hz": round(1 / dts[len(dts) // 2], 2) if dts else None,
            "aoa_recorded": bool(samples) and not samples[0].aoa_derived,
        },
        "dcs": {"wire": p.wire, "grade": asdict(p.dcs_grade) if p.dcs_grade else None},
        "wind": p.wind.to_dict() if p.wind else None,
    }


def approach_window(a: Approach) -> tuple[float, float]:
    return a.start_time - APPROACH_LEAD_S, a.end_time + TAIL_S


def track_sidecar(recording: Recording, a: Approach, source: str | Path) -> dict:
    """Sidecar of a track report: one aircraft's own track around an approach, no carrier. Central
    grades it against the carrier from another report of the same landing."""
    start, end = approach_window(a)
    plane = recording.objects[a.aircraft_id]
    samples = [s for s in plane.samples if start <= s.time <= end]
    dts = sorted(b.time - x.time for x, b in zip(samples, samples[1:]))
    g = recording.globals
    return {
        "schema": SIDECAR_SCHEMA,
        "kind": "track",
        "collector_version": _collector_version(),
        "source": Path(source).name,
        "recording": {**{k: g.get(k) for k in ("Title", "RecordingTime", "ReferenceTime", "DataRecorder", "DataSource")},
                      "first_frame_time": first_frame_time(recording)},
        "window": {"start": start, "end": end},
        "objects": [a.aircraft_id],
        "pass": {
            "aircraft_id": a.aircraft_id,
            "aircraft_type": plane.name,
            "pilot": plane.pilot,
            "outcome": "track",
            "start_time": a.start_time,
            "end_time": a.end_time,
            "sample_rate_hz": round(1 / dts[len(dts) // 2], 2) if dts else None,
            "aoa_recorded": any(s.aoa is not None for s in samples),
        },
    }


def track_slice_name(recording: Recording, a: Approach) -> str:
    stamp = (recording.globals.get("RecordingTime") or "")[:19].replace("-", "").replace(":", "").replace("T", "-")
    pilot = re.sub(r"[^A-Za-z0-9]+", "_", recording.objects[a.aircraft_id].pilot or f"id{a.aircraft_id:x}").strip("_")
    return f"{stamp or 'recording'}_{pilot}_{a.start_time:.0f}s_track"


def slice_name(recording: Recording, p: PassResult) -> str:
    stamp = (recording.globals.get("RecordingTime") or "")[:19].replace("-", "").replace(":", "").replace("T", "-")
    pilot = re.sub(r"[^A-Za-z0-9]+", "_", p.pilot or f"id{p.aircraft_id:x}").strip("_")
    return f"{stamp or 'recording'}_{pilot}_{p.start_time:.0f}s"


def write_pass_slice(source: str | Path, recording: Recording, p: PassResult,
                     out_dir: str | Path) -> tuple[Path, Path]:
    """Write `<name>.zip.acmi` and `<name>.json` for one pass; returns both paths."""
    out_dir = Path(out_dir)
    name = slice_name(recording, p)
    ids = slice_objects(recording, p)
    start, end = window(p)
    acmi = write_slice(source, out_dir / f"{name}.zip.acmi", start, end, ids)
    meta = out_dir / f"{name}.json"
    meta.write_text(json.dumps(sidecar(recording, p, source, ids), indent=2) + "\n", encoding="utf-8")
    return acmi, meta


def _same_pilot(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


def _own(track) -> bool:
    """Flown on the PC that made the recording: Tacview records AOA (and fuel, head position...) only
    for the local player's jet. A dedicated server's recording has no such aircraft."""
    return any(s.aoa is not None for s in track.samples)


# DCS's default pilot name (single player, or a player who never set one): passes flown under it can't be
# credited to anyone, so central doesn't record them.
DEFAULT_PILOT_NAMES = frozenset({"new callsign"})


def is_default_pilot(name: str | None) -> bool:
    return (name or "").strip().casefold() in DEFAULT_PILOT_NAMES


def own_pilots(recording: Recording) -> list[str]:
    """The recording's own pilot(s): who flew an aircraft the LSO grades on the PC that recorded it.
    Normally one; none in a dedicated server's recording."""
    from .geometry import AIRCRAFT
    return sorted({t.pilot for t in recording.objects.values() if t.name in AIRCRAFT and t.pilot and _own(t)},
                  key=str.casefold)


def slice_recording(source: str | Path, out_dir: str | Path, debrief: Debrief | None = None,
                    recording: Recording | None = None, pilot: str | None = None,
                    own_only: bool = False) -> list[tuple[Path, dict]]:
    """Everything uploadable in a whole recording (backfill): a slice + sidecar for every carrier pass,
    and a track report around every approach of the recording PC's own jet (the aircraft with
    recorded AOA) that isn't already a pass, e.g. a multiplayer client's recording without the carrier.
    With `debrief` (that session's debrief.log), DCS's grades and wires are attached. With `pilot`, only
    that pilot's passes and approaches; with `own_only`, only those of aircraft flown on the PC that
    made the recording (see `own_pilots`)."""
    from .dcslog import attach_dcs_grades, track_dcs_grades
    from .detect import find_passes
    from .detect.approaches import find_approaches
    from .geometry import AIRCRAFT

    source, out_dir = Path(source), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recording = recording or load_recording(source)
    passes = [p for p in find_passes(recording) if (pilot is None or _same_pilot(p.pilot, pilot))
              and (not own_only or _own(recording.objects[p.aircraft_id]))]
    if debrief is not None:
        attach_dcs_grades(passes, recording, debrief)
    out: list[tuple[Path, dict]] = []
    for p in passes:
        acmi, meta = write_pass_slice(source, recording, p, out_dir)
        out.append((acmi, json.loads(meta.read_text(encoding="utf-8"))))
    approaches = []
    for track in recording.objects.values():
        if track.name not in AIRCRAFT or not any(s.aoa is not None for s in track.samples):
            continue
        if pilot is not None and not _same_pilot(track.pilot, pilot):
            continue
        for a in find_approaches(recording, track.id):
            start, end = approach_window(a)
            if not any(p.aircraft_id == a.aircraft_id and p.start_time < end and start < p.end_time for p in passes):
                approaches.append(a)
    grades = track_dcs_grades(recording, approaches, debrief) if debrief is not None else {}
    for a in approaches:
        start, end = approach_window(a)
        acmi = write_slice(source, out_dir / f"{track_slice_name(recording, a)}.zip.acmi", start, end, {a.aircraft_id})
        meta = track_sidecar(recording, a, source)
        if (grade := grades.get(a)) is not None:
            meta["dcs"] = {"wire": grade.wire, "grade": asdict(grade)}
        out.append((acmi, meta))
    return out
