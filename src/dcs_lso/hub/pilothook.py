"""Uploads from the pilot hook: a pilot's own jet, recorded by DCS's Export.lua on their PC.

The pilot hook sends one upload per approach (JSON), with the jet's samples as CSV text (simple to build
in DCS's Lua). Columns, one row per frame (or whatever rate the hook sends):

    t        DCS model time, seconds: on a multiplayer client this is the *mission* time (the server's)
    x, y, z  DCS map coordinates, meters (x north, y up, z east): Tacview's v, altitude and u
    heading, pitch, bank  radians (heading relative to the map grid, as Tacview's `heading`)
    aoa      angle of attack, degrees
    lat, lon degrees

Checked against the same flight's Tacview recording on 2026-10-03: positions agree to under 1 m, angles and
AOA exactly, with Tacview's time = model time minus the client's join offset.

Where the server lets clients see other objects, the upload also has the carrier the pilot approached
(`"carrier": {"type", "unit", "csv"}`, columns `t,x,y,z,heading,lat,lon`, about 10 Hz). Then each pass in it
is a full report (graded on its own, no server agent needed: e.g. a trap flown on another community's server);
without it, each approach is an own-jet track report, graded against the carrier in the server agent's report
of the same landing. Either way it merges with the server agent's report when there is one. Times are mission
time (`"clock": "mission"` in the sidecar); the hub lines them up with the server agent's report of the same
mission, whose recording also starts at mission start.
"""

from __future__ import annotations

import csv
import io
import math
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..acmi import load_recording
from ..acmi.writer import escape
from ..detect import find_passes
from ..detect.approaches import find_approaches
from ..geometry import AIRCRAFT, CARRIERS
from ..slices import sidecar, track_sidecar

MAX_SAMPLES = 200_000  # about 15 minutes at 200 Hz
COLUMNS = ("t", "x", "y", "z", "heading", "pitch", "bank", "aoa", "lat", "lon")
CARRIER_COLUMNS = ("t", "x", "y", "z", "heading", "lat", "lon")
JET_ID, CARRIER_ID = 1, 2
# The placeholder start of a pilot hook's slice: its times are mission time, lined up by the hub
# (see `Hub._reference_time`), so the slice's own ReferenceTime is never used to place it.
PLACEHOLDER_REFERENCE = "2000-01-01T00:00:00Z"

# The pilot hook's latest version (`VERSION` in pilot-hook/Scripts/Hooks/dcs-lso-pilot-hook.lua). The hub tells an
# older hook in its reply, and the hook logs that an update is available.
PILOT_HOOK_VERSION = 2


class HookUploadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HookCarrier:
    type: str  # DCS type name, e.g. "CVN_75"
    unit: str  # unit name in the mission, e.g. "CVN-75 Harry S. Truman"
    rows: list[dict[str, float]]


@dataclass(frozen=True, slots=True)
class HookUpload:
    version: int  # the pilot hook's version (see PILOT_HOOK_VERSION)
    pilot: str
    aircraft: str
    mission: str
    ucid: str | None
    server: str | None
    sent_at: datetime | None  # the PC's clock when it sent the upload (UTC)
    sent_model_time: float | None  # model time when it sent the upload
    livery: str | None
    onboard_num: str | None
    rows: list[dict[str, float]]
    carrier: HookCarrier | None = None
    sun_elevation: float | None = None  # DCS's own sun (degrees) where and when the approach ended
    # Live LSO calls relayed from the hub of the server the pass was flown on (its server agent made them), and
    # that hub's address: {"time", "along", "call"}, mission time.
    calls: list[dict] | None = None
    calls_from: str | None = None
    # DCS's own LSO grade for the pass, from the pilot's debrief.log (e.g. "LSO: GRADE:OK : (LOAR)  WIRE# 1"):
    # sent again once DCS has written it (at the end of the mission); a re-upload may add it.
    dcs_grade: str | None = None


def parse_upload(body: dict) -> HookUpload:
    """Check a pilot hook upload."""
    if not isinstance(body, dict):
        raise HookUploadError("expected a JSON object")
    try:
        version = int(body.get("version", 1))
        pilot = str(body["pilot"]).strip()
        aircraft = str(body["aircraft"]).strip()
        mission = str(body["mission"]).strip()
        text = str(body["csv"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HookUploadError(f"missing or invalid field: {exc}") from exc
    if not pilot or not mission:
        raise HookUploadError("pilot and mission are required")
    if aircraft not in AIRCRAFT:
        raise HookUploadError(f"aircraft {aircraft!r} isn't graded here")
    rows = _rows(text, COLUMNS)
    if len(rows) < 2:
        raise HookUploadError("no samples")
    carrier = None
    if isinstance(body.get("carrier"), dict):
        c = body["carrier"]
        if str(c.get("type") or "") in CARRIERS:  # other ships: no deck data, as if no carrier was sent
            carrier_rows = _rows(str(c.get("csv") or ""), CARRIER_COLUMNS)
            if len(carrier_rows) >= 2:
                carrier = HookCarrier(type=str(c["type"]), unit=str(c.get("unit") or c["type"])[:100], rows=carrier_rows)
    sent_at = None
    if body.get("sent_at") is not None:
        try:
            sent_at = datetime.fromtimestamp(float(body["sent_at"]), UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            sent_at = None

    def optional(key: str) -> str | None:
        value = body.get(key)
        return str(value).strip()[:100] or None if value is not None else None

    try:
        sent_model_time = float(body["sent_model_time"]) if body.get("sent_model_time") is not None else None
    except (TypeError, ValueError):
        sent_model_time = None
    return HookUpload(version=version, pilot=pilot[:100], aircraft=aircraft, mission=mission[:200],
                      ucid=optional("ucid"), server=optional("server"), sent_at=sent_at,
                      sent_model_time=sent_model_time, livery=optional("livery"),
                      onboard_num=optional("onboard_num"), rows=rows, carrier=carrier,
                      sun_elevation=_number(body.get("sun_elevation")), calls=_calls(body.get("calls")),
                      calls_from=optional("calls_from"), dcs_grade=_dcs_grade(body.get("dcs_grade")))


def _dcs_grade(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:300] if text.startswith("LSO:") else None


def _calls(value: object) -> list[dict] | None:
    """Relayed live calls, kept only if well-formed."""
    if not isinstance(value, list):
        return None
    calls = []
    for c in value[:200]:
        if isinstance(c, dict) and isinstance(c.get("call"), str):
            time, along = _number(c.get("time")), _number(c.get("along"))
            if time is not None and along is not None:
                calls.append({"time": time, "along": along, "call": c["call"][:60]})
    return calls or None


def _number(value: object) -> float | None:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _rows(text: str, columns: tuple[str, ...]) -> list[dict[str, float]]:
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        raise HookUploadError(f"csv is missing columns: {', '.join(missing)}")
    rows = []
    for i, raw in enumerate(reader):
        if i >= MAX_SAMPLES:
            raise HookUploadError(f"more than {MAX_SAMPLES} samples")
        try:
            row = {c: float(raw[c]) for c in columns}
        except (TypeError, ValueError):
            continue  # a frame with a missing value (e.g. "nil" while respawning)
        if all(math.isfinite(v) for v in row.values()):
            rows.append(row)
    rows.sort(key=lambda r: r["t"])
    return rows


def to_acmi(upload: HookUpload) -> bytes:
    """The upload as a standalone ACMI slice (zipped), on the mission clock: the pilot's jet, and the carrier
    when the upload has it."""
    lines = ["FileType=text/acmi/tacview", "FileVersion=2.2",
             f"0,ReferenceTime={PLACEHOLDER_REFERENCE}", f"0,Title={escape(upload.mission)}",
             "0,DataSource=dcs-lso pilot hook"]
    updates: list[tuple[float, int, str]] = []
    for i, r in enumerate(upload.rows):
        heading = math.degrees(r["heading"]) % 360.0
        transform = _transform(r["lon"], r["lat"], r["y"], math.degrees(r["bank"]), math.degrees(r["pitch"]), heading,
                               r["z"], r["x"])
        props = f"{JET_ID},T={transform},AOA={r['aoa']:.3f}"
        if i == 0:
            props += f",Type=Air+FixedWing,Name={escape(upload.aircraft)},Pilot={escape(upload.pilot)}"
        updates.append((r["t"], JET_ID, props))
    if upload.carrier is not None:
        for i, r in enumerate(upload.carrier.rows):
            heading = math.degrees(r["heading"]) % 360.0
            props = f"{CARRIER_ID},T={_transform(r['lon'], r['lat'], r['y'], 0.0, 0.0, heading, r['z'], r['x'])}"
            if i == 0:
                props += (f",Type=Sea+Watercraft+AircraftCarrier,Name={escape(upload.carrier.type)},"
                          f"Pilot={escape(upload.carrier.unit)}")
            updates.append((r["t"], CARRIER_ID, props))
    updates.sort(key=lambda u: (u[0], u[1]))
    frame = None
    for t, _, props in updates:
        if t != frame:
            lines.append(f"#{t:.4f}")
            frame = t
        lines.append(props)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pilot-hook.txt.acmi", "\n".join(lines) + "\n")
    return buffer.getvalue()


def _transform(lon: float, lat: float, alt: float, roll: float, pitch: float, heading: float, u: float, v: float) -> str:
    return "|".join(f"{x:.7f}" if i < 2 else f"{x:.3f}" for i, x in enumerate(
        (lon, lat, alt, roll, pitch, heading, u, v, heading)))


def hook_reports(upload: HookUpload, work_dir: Path) -> list[tuple[bytes, dict]]:
    """The upload's reports (slice bytes, sidecar): with the carrier, a full report per pass; for approaches
    without one (no carrier sent, or not to the carrier), an own-jet track report."""
    data = to_acmi(upload)
    path = work_dir / "pilot-hook.zip.acmi"
    path.write_bytes(data)
    recording = load_recording(path)
    metas = []
    passes = [p for p in find_passes(recording) if p.aircraft_id == JET_ID] if upload.carrier else []
    for p in passes:
        metas.append((sidecar(recording, p, "pilot hook", {JET_ID, CARRIER_ID}), p.start_time, p.end_time))
    for approach in find_approaches(recording, JET_ID):
        if not any(start < approach.end_time and approach.start_time < end for _, start, end in metas):
            metas.append((track_sidecar(recording, approach, "pilot hook"), approach.start_time, approach.end_time))
    reports = []
    for meta, start, end in metas:
        meta["clock"] = "mission"
        if upload.sun_elevation is not None:
            meta["sun_elevation"] = upload.sun_elevation
        if upload.dcs_grade:
            from ..dcslog import LsoGrade
            grade = LsoGrade.parse(upload.dcs_grade)
            meta["dcs"] = {"wire": grade.wire, "grade": {"raw": grade.raw}}
        if upload.calls:
            meta["calls"] = [c for c in upload.calls if start - 60 <= c["time"] <= end + 30]  # this pass's
            meta["calls_from"] = upload.calls_from
        meta["pilot_hook"] = {"version": upload.version, "ucid": upload.ucid, "server": upload.server}
        if upload.sent_at is not None and upload.sent_model_time is not None:
            ended = upload.sent_at - timedelta(seconds=max(0.0, upload.sent_model_time - end))
            meta["pass"]["occurred_at"] = (ended - timedelta(seconds=end - start)).isoformat()
        if upload.livery or upload.onboard_num:
            meta["aircraft"] = {"livery": upload.livery, "onboard_num": upload.onboard_num}
        reports.append((data, meta))
    return reports
