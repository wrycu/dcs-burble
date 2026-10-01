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

from .acmi import Recording
from .acmi.writer import write_slice
from .detect import CarrierTimeline, PassResult

NM = 1852.0
LEAD_S = 30.0
TAIL_S = 10.0
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


def sidecar(recording: Recording, p: PassResult, source: str | Path, object_ids: set[int]) -> dict:
    start, end = window(p)
    samples = p.samples
    dts = sorted(b.time - a.time for a, b in zip(samples, samples[1:]))
    g = recording.globals
    return {
        "schema": SIDECAR_SCHEMA,
        "collector_version": _collector_version(),
        "source": Path(source).name,
        "recording": {k: g.get(k) for k in ("Title", "RecordingTime", "ReferenceTime", "DataRecorder", "DataSource")},
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
    }


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
