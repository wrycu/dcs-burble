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

Each upload becomes a standalone ACMI slice and, for each approach in it, an own-jet track report: the same
kind of report as the pilot uploader's, merged with the server agent's report of the landing. Its times are
mission time (`"clock": "mission"` in the sidecar); the hub lines them up with the server agent's report
of the same mission, whose recording also starts at mission start.
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
from ..detect.approaches import find_approaches
from ..geometry import AIRCRAFT
from ..slices import track_sidecar

MAX_SAMPLES = 200_000  # about 15 minutes at 200 Hz
COLUMNS = ("t", "x", "y", "z", "heading", "pitch", "bank", "aoa", "lat", "lon")
# The placeholder start of a pilot hook's slice: its times are mission time, lined up by the hub
# (see `Hub._reference_time`), so the slice's own ReferenceTime is never used to place it.
PLACEHOLDER_REFERENCE = "2000-01-01T00:00:00Z"


class HookUploadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HookUpload:
    version: int
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
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise HookUploadError(f"csv is missing columns: {', '.join(missing)}")
    rows = []
    for i, raw in enumerate(reader):
        if i >= MAX_SAMPLES:
            raise HookUploadError(f"more than {MAX_SAMPLES} samples")
        try:
            row = {c: float(raw[c]) for c in COLUMNS}
        except (TypeError, ValueError):
            continue  # a frame with a missing value (e.g. "nil" while respawning)
        if all(math.isfinite(v) for v in row.values()):
            rows.append(row)
    rows.sort(key=lambda r: r["t"])
    if len(rows) < 2:
        raise HookUploadError("no samples")
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
                      onboard_num=optional("onboard_num"), rows=rows)


def to_acmi(upload: HookUpload) -> bytes:
    """The upload as a standalone ACMI slice (zipped): one object, the pilot's jet, on the mission clock."""
    lines = ["FileType=text/acmi/tacview", "FileVersion=2.2",
             f"0,ReferenceTime={PLACEHOLDER_REFERENCE}", f"0,Title={escape(upload.mission)}",
             "0,DataSource=dcs-lso pilot hook"]
    first = True
    for r in upload.rows:
        heading = math.degrees(r["heading"]) % 360.0
        transform = "|".join(f"{v:.7f}" if i < 2 else f"{v:.3f}" for i, v in enumerate((
            r["lon"], r["lat"], r["y"], math.degrees(r["bank"]), math.degrees(r["pitch"]), heading,
            r["z"], r["x"], heading)))
        lines.append(f"#{r['t']:.4f}")
        props = f"1,T={transform},AOA={r['aoa']:.3f}"
        if first:
            props += f",Type=Air+FixedWing,Name={escape(upload.aircraft)},Pilot={escape(upload.pilot)}"
            first = False
        lines.append(props)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pilot-hook.txt.acmi", "\n".join(lines) + "\n")
    return buffer.getvalue()


def track_reports(upload: HookUpload, work_dir: Path) -> list[tuple[bytes, dict]]:
    """One own-jet track report (slice bytes, sidecar) per approach in the upload."""
    data = to_acmi(upload)
    path = work_dir / "pilot-hook.zip.acmi"
    path.write_bytes(data)
    recording = load_recording(path)
    reports = []
    for approach in find_approaches(recording, 1):
        meta = track_sidecar(recording, approach, "pilot hook")
        meta["clock"] = "mission"
        meta["pilot_hook"] = {"version": upload.version, "ucid": upload.ucid, "server": upload.server}
        if upload.sent_at is not None and upload.sent_model_time is not None:
            ended = upload.sent_at - timedelta(seconds=max(0.0, upload.sent_model_time - approach.end_time))
            meta["pass"]["occurred_at"] = (ended - timedelta(seconds=approach.end_time - approach.start_time)).isoformat()
        if upload.livery or upload.onboard_num:
            meta["aircraft"] = {"livery": upload.livery, "onboard_num": upload.onboard_num}
        reports.append((data, meta))
    return reports
