"""Two carriers in one mission: each pass goes to its own carrier, each carrier's calls go out on its own
frequency, and passes on one carrier don't affect the other (foul deck, side numbers).

The recording is a real one (Wrycu trapping on the Truman) with a second carrier added 9 km north and a
copy of the jet ("Maverick") moved with it, so both trap at the same moment on different carriers.
"""

import asyncio
import zipfile
from pathlib import Path

import pytest

from dcs_lso.acmi import load_recording
from dcs_lso.central.service import Central
from dcs_lso.detect import find_passes
from dcs_lso.slices import sidecar, slice_objects
from dcs_lso.srs import Radio
from test_live_callouts import CONFIG, SlotHooks, clips, run_collector  # noqa: F401 (clips: a fixture)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = FIXTURES / "passes" / "20260927-204347_Wrycu_4013s.zip.acmi"
TRUMAN, WASHINGTON = "CVN-75 Harry S. Truman", "CVN-73 George Washington"
NORTH_M = 9000.0
FREQUENCIES = {TRUMAN: Radio(127.5), WASHINGTON: Radio(127.75)}


def _shift(transform: str, north_m: float) -> str:
    """Move a T= value north: latitude (field 2) and the north coordinate v (5-field form: field 5;
    9-field form: field 8). Blank fields mean "unchanged" and stay blank."""
    f = transform.split("|")
    if f[1]:
        f[1] = f"{float(f[1]) + north_m / 110540.0:.7f}"
    v = {5: 4, 9: 7}.get(len(f))
    if v is not None and f[v]:
        f[v] = f"{float(f[v]) + north_m:.2f}"
    return "|".join(f)


def two_carriers(out: Path) -> Path:
    with zipfile.ZipFile(SOURCE) as z:
        (name,) = z.namelist()
        lines = z.read(name).decode("utf-8-sig").splitlines()
    copies = {"102": ("1102", {"Name=CVN_75": "Name=CVN_73", f"Pilot={TRUMAN}": f"Pilot={WASHINGTON}",
                               "Group=TRUMAN": "Group=WASHINGTON"}),
              "2c02": ("3c02", {"Pilot=Wrycu": "Pilot=Maverick"})}
    result = []
    for line in lines:
        result.append(line)
        head, _, rest = line.partition(",")
        if head not in copies:
            continue
        new_id, renames = copies[head]
        fields = rest.split(",")
        fields = [f"T={_shift(f[2:], NORTH_M)}" if f.startswith("T=") else f for f in fields]
        copy = f"{new_id}," + ",".join(fields)
        for old, new in renames.items():
            copy = copy.replace(old, new)
        result.append(copy)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\n".join(result) + "\n")
    return out


@pytest.fixture
def recording(tmp_path) -> Path:
    return two_carriers(tmp_path / "two-carriers.zip.acmi")


def test_each_pass_goes_to_its_own_carrier(recording):
    r = load_recording(recording)
    passes = sorted((r.objects[p.carrier_id].pilot, p.pilot, p.outcome.value) for p in find_passes(r))
    assert passes == [(WASHINGTON, "Maverick", "trap"), (TRUMAN, "Wrycu", "trap")]
    # The same flight relative to its own deck: the same grade and wire estimate.
    from dcs_lso.grading import grade_pass
    graded = {p.pilot: (grade_pass(p).text, p.wire_estimate) for p in find_passes(r)}
    assert graded["Maverick"] == graded["Wrycu"]


class CarrierSlotHooks(SlotHooks):
    def carrier_radio(self, carrier_unit):
        return FREQUENCIES.get(carrier_unit)


def test_each_carriers_calls_go_out_on_its_own_frequency(tmp_path, clips, recording):
    import test_live_callouts
    config = {"callouts": {k: v for k, v in CONFIG["callouts"].items() if k != "carriers"}}  # nothing per carrier
    original, test_live_callouts.CONFIG = test_live_callouts.CONFIG, config
    try:
        collector, session, sink = asyncio.run(run_collector(
            recording, tmp_path / "edge", clips, hooks=CarrierSlotHooks({"Wrycu": "301", "Maverick": "302"})))
    finally:
        test_live_callouts.CONFIG = original
    radios = {radio for _, radio in sink.said}
    assert radios == {FREQUENCIES[TRUMAN], FREQUENCIES[WASHINGTON]}
    # Calls for each jet on its own carrier's frequency: as many on each frequency as were made for that jet.
    per_jet = {a: sum(m.aircraft_id == a for m in session.callouts.made) for a in (0x2C02, 0x3C02)}
    per_radio = {r: sum(radio == r for _, radio in sink.said) for r in FREQUENCIES.values()}
    assert per_jet[0x2C02] > 0 and per_jet[0x3C02] > 0
    assert per_radio == {FREQUENCIES[TRUMAN]: per_jet[0x2C02], FREQUENCIES[WASHINGTON]: per_jet[0x3C02]}
    # Two grooves at once, but on different carriers: neither is "busy", nobody fouls the other's deck.
    from dcs_lso.callouts.rules import Call
    assert not any(text[:3] in ("301", "302") for text in sink.texts)
    assert Call.WAVE_OFF_FOUL_DECK not in {call for call, _ in sink.said}
    # Each uploaded pass names its carrier.
    units = sorted(item.meta()["pass"]["carrier_unit"] for item in collector.outbox.pending())
    assert units == sorted([TRUMAN, WASHINGTON])


def test_central_keeps_the_carrier_of_each_landing(tmp_path, recording):
    central = Central(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "central")
    central.add_source("server1")
    r = load_recording(recording)
    for p in find_passes(r):
        central.ingest(1, recording.read_bytes(), sidecar(r, p, "x", slice_objects(r, p)))
    from fastapi.testclient import TestClient

    from dcs_lso.central.app import create_app
    landings = TestClient(create_app(central)).get("/api/v1/passes", params={"days": 0}).json()
    assert sorted((x["pilot"], x["carrier"]) for x in landings) == [("Maverick", WASHINGTON), ("Wrycu", TRUMAN)]
