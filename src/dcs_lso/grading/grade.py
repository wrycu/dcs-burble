"""Grading v1: deviations per groove position -> LSO remarks -> grade.

Uses the whole pass (hindsight is fine here, unlike live callouts). Positions follow
the LSO convention: X (start), IM (in the middle), IC (in close), AR (at the ramp).
Remarks use the DCS/LSO shorthand: HI/LO glideslope, LUL/LUR lined up left/right,
F/SLO fast/slow; "(x)" a little, "x" a deviation, "_x_" a lot.

All thresholds are first guesses, to be tuned against passes with known grades; the
version number must change whenever grading behaviour changes, so stored grades can be
told apart and passes regraded.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum

from ..detect import Outcome, PassResult
from ..geometry import AIRCRAFT

GRADING_VERSION = "5"  # 5: crashes on deck are their own outcome, graded Cut (not bolters); 4: speed on deck measured over 0.5 s (high-rate tracks stop, so traps aren't bolters); 3: wire estimated from the stop point; 2: 3.6 deg glideslope (as DCS); AOA derived from motion in the aircraft frame, with wind, not graded at AR
NM = 1852.0


class Position(StrEnum):
    X = "X"
    IM = "IM"
    IC = "IC"
    AR = "AR"
    IW = "IW"  # in the wires (touchdown)


# Along-distance ranges (meters short of the aim point), outer edge first. The ramp is
# about 70 m short of the aim point on the Nimitz class.
POSITIONS: dict[Position, tuple[float, float]] = {
    Position.X: (0.75 * NM, 0.5 * NM),
    Position.IM: (0.5 * NM, 0.25 * NM),
    Position.IC: (0.25 * NM, 150.0),
    Position.AR: (150.0, 70.0),
}
MIN_SAMPLES = 2  # fewer than this in a position: not graded there ("NC", no count)

# Touchdown attitude: at or below this pitch the nose gear lands first ("LNF"), which DCS's
# LSO treats as a cut. (Three-point landings, "3PTS", aren't separable from normal ones at
# Tacview's sample rates, so they aren't graded.)
LNF_PITCH_DEG = 0.5
TOUCHDOWN_HOOK_HEIGHT_M = 0.3


class Severity(IntEnum):
    NONE = 0
    LITTLE = 1
    NORMAL = 2
    LOT = 3


# (little, normal, a lot) thresholds per quantity.
GLIDESLOPE_DEG = (0.35, 0.7, 1.2)
LINEUP_DEG = (0.5, 1.0, 2.0)
LINEUP_AR_M = (2.0, 4.0, 8.0)  # angles blow up at the ramp; use meters there
AOA_MARGIN_DEG = (0.0, 0.5, 1.5)  # beyond the aircraft's on-speed band


class Grade(StrEnum):
    PERFECT = "_OK_"
    OK = "OK"
    FAIR = "(OK)"
    NO_GRADE = "---"
    CUT = "C"
    BOLTER = "B"
    WAVE_OFF = "WO"


# Plain-language names. "---" in particular is a real grade (No Grade: safe but below
# average), not a missing one, so it should always be shown with its name.
NAMES = {Grade.PERFECT: "Perfect", Grade.OK: "OK", Grade.FAIR: "Fair", Grade.NO_GRADE: "No Grade",
         Grade.CUT: "Cut", Grade.BOLTER: "Bolter", Grade.WAVE_OFF: "Wave-off"}
# Short labels for tight spaces such as greenie board cells.
SHORT = {Grade.PERFECT: "OK+", Grade.NO_GRADE: "NG"}


def grade_name(grade: str) -> str:
    try:
        return NAMES[Grade(grade)]
    except ValueError:
        return grade


def grade_short(grade: str) -> str:
    try:
        g = Grade(grade)
    except ValueError:
        return grade
    return SHORT.get(g, g.value)


POINTS = {Grade.PERFECT: 5.0, Grade.OK: 4.0, Grade.FAIR: 3.0, Grade.NO_GRADE: 2.0,
          Grade.BOLTER: 2.5, Grade.WAVE_OFF: 1.0, Grade.CUT: 0.0}


# (a little, normal, a lot) wording per remark code; a plain string has no severity.
ENGLISH: dict[str, tuple[str, str, str] | str] = {
    "HI": ("A little high", "High", "Well high"),
    "LO": ("A little low", "Low", "Well low"),
    "LUL": ("Lined up a little left", "Lined up left", "Lined up well left"),
    "LUR": ("Lined up a little right", "Lined up right", "Lined up well right"),
    "F": ("A little fast", "Fast", "Very fast"),
    "SLO": ("A little slow", "Slow", "Very slow"),
    "LNF": "Landed nose first",
}
POSITION_ENGLISH = {Position.X: "at the start", Position.IM: "in the middle", Position.IC: "in close",
                    Position.AR: "at the ramp", Position.IW: "in the wires"}


@dataclass(frozen=True, slots=True)
class Remark:
    code: str  # HI, LO, LUL, LUR, F, SLO, LNF
    position: Position
    severity: Severity

    @property
    def english(self) -> str:
        """Plain-English wording, without the position (e.g. "Well high")."""
        words = ENGLISH.get(self.code)
        if words is None:
            return self.text
        if isinstance(words, str):
            return words
        little, normal, lot = words
        return {Severity.LITTLE: little, Severity.LOT: lot}.get(self.severity, normal)

    @property
    def english_with_position(self) -> str:
        """E.g. "Well high at the start"."""
        return f"{self.english} {POSITION_ENGLISH[self.position]}"

    @property
    def text(self) -> str:
        core = f"{self.code}{self.position.value}"
        if self.severity is Severity.LITTLE:
            return f"({core})"
        if self.severity is Severity.LOT:
            return f"_{core}_"
        return core


@dataclass(frozen=True, slots=True)
class PositionStats:
    position: Position
    samples: int
    glideslope_deg: float | None  # + high
    lineup_deg: float | None  # + right of centerline
    lateral_m: float | None
    aoa: float | None


@dataclass
class GradeResult:
    version: str
    grade: Grade
    points: float
    remarks: list[Remark] = field(default_factory=list)
    positions: list[PositionStats] = field(default_factory=list)
    not_counted: list[Position] = field(default_factory=list)
    # The wire estimated from the track (not a grade input; kept with the grade so it's versioned
    # and rebuilt by regrading). DCS's own wire, when known, is stored with the pass and wins.
    wire_estimate: int | None = None

    @property
    def text(self) -> str:
        parts = [r.text for r in self.remarks] + [f"NC{p.value}" for p in self.not_counted]
        return f"{self.grade.value} : {' '.join(parts)}" if parts else self.grade.value

    def to_dict(self) -> dict:
        d = asdict(self)
        d["text"] = self.text
        d["remarks"] = [{"code": r.code, "position": r.position.value, "severity": r.severity.name.lower(),
                         "text": r.text} for r in self.remarks]
        return d


def _angle(value: float, along: float) -> float:
    return math.degrees(math.atan2(value, max(along, 30.0)))


def _severity(value: float, thresholds: tuple[float, float, float]) -> Severity:
    v = abs(value)
    little, normal, lot = thresholds
    if v >= lot:
        return Severity.LOT
    if v >= normal:
        return Severity.NORMAL
    if v >= little and little > 0:
        return Severity.LITTLE
    return Severity.NONE


def position_stats(p: PassResult) -> list[PositionStats]:
    glideslope = AIRCRAFT[p.aircraft_type].glideslope
    out = []
    for pos, (outer, inner) in POSITIONS.items():
        s = [x for x in p.samples if inner <= x.along < outer]
        if len(s) < MIN_SAMPLES:
            out.append(PositionStats(pos, len(s), None, None, None, None))
            continue
        n = len(s)
        # AOA derived from motion can't follow the pilot's last corrections at the ramp at the server's
        # 4.8 Hz (checked against real AOA: 0.2 deg RMS before 150 m, 1-2 deg after), so it isn't graded there.
        aoas = [x.aoa for x in s if x.aoa is not None and not (x.aoa_derived and pos is Position.AR)]
        out.append(PositionStats(
            position=pos,
            samples=n,
            glideslope_deg=sum(_angle(x.hook_height, x.along) for x in s) / n - glideslope,
            lineup_deg=sum(_angle(x.lateral, x.along) for x in s) / n,
            lateral_m=sum(x.lateral for x in s) / n,
            aoa=sum(aoas) / len(aoas) if aoas else None,
        ))
    return out


def remarks_for(stats: PositionStats, on_speed: tuple[float, float]) -> list[Remark]:
    out: list[Remark] = []
    pos = stats.position
    if stats.glideslope_deg is not None:
        sev = _severity(stats.glideslope_deg, GLIDESLOPE_DEG)
        if sev:
            out.append(Remark("HI" if stats.glideslope_deg > 0 else "LO", pos, sev))
    if pos is Position.AR and stats.lateral_m is not None:
        value, sev = stats.lateral_m, _severity(stats.lateral_m, LINEUP_AR_M)
    elif stats.lineup_deg is not None:
        value, sev = stats.lineup_deg, _severity(stats.lineup_deg, LINEUP_DEG)
    else:
        value, sev = 0.0, Severity.NONE
    if sev:
        out.append(Remark("LUR" if value > 0 else "LUL", pos, sev))
    if stats.aoa is not None:
        low, high = on_speed
        if stats.aoa < low:
            sev = _severity(low - stats.aoa, AOA_MARGIN_DEG)
            if sev:
                out.append(Remark("F", pos, max(sev, Severity.LITTLE)))
        elif stats.aoa > high:
            sev = _severity(stats.aoa - high, AOA_MARGIN_DEG)
            if sev:
                out.append(Remark("SLO", pos, max(sev, Severity.LITTLE)))
    return out


def touchdown_remarks(p: PassResult) -> list[Remark]:
    touchdown = next((s for s in p.samples if s.hook_height < TOUCHDOWN_HOOK_HEIGHT_M
                      and -250.0 < s.along < 40.0 and abs(s.lateral) < 25.0), None)
    if touchdown is not None and touchdown.pitch <= LNF_PITCH_DEG:
        return [Remark("LNF", Position.IW, Severity.NORMAL)]
    return []


def _trap_grade(remarks: list[Remark]) -> Grade:
    if any(r.code == "LNF" for r in remarks):
        return Grade.CUT
    in_close = (Position.IC, Position.AR)
    # Unsafe: well low in close, or well off centerline at the ramp.
    if any(r.severity is Severity.LOT and r.position in in_close and r.code == "LO" for r in remarks) or any(
            r.severity is Severity.LOT and r.position is Position.AR and r.code in ("LUL", "LUR") for r in remarks):
        return Grade.CUT
    lots = sum(r.severity is Severity.LOT for r in remarks)
    normals = sum(r.severity is Severity.NORMAL for r in remarks)
    littles = sum(r.severity is Severity.LITTLE for r in remarks)
    if lots or normals >= 3:
        return Grade.NO_GRADE
    if normals or littles >= 3:
        return Grade.FAIR
    if littles:
        return Grade.OK
    return Grade.PERFECT


def dcs_only_grade(dcs_grade: str) -> tuple[Grade, str]:
    """A landing graded by DCS's LSO alone (no carrier in the track to grade it ourselves): DCS's grade, and
    its comment as the grade text (DCS's notation is the same as ours)."""
    from ..dcslog import LsoGrade
    parsed = LsoGrade.parse(dcs_grade)
    try:
        grade = Grade(parsed.grade or "")
    except ValueError:
        grade = Grade.NO_GRADE
    return grade, f"{grade.value} : {parsed.remarks}" if parsed.remarks else grade.value


def dcs_only_outcome(dcs_grade: str, wire: int | None) -> str:
    """A DCS-only landing's outcome: a trap if DCS named the wire, else from its grade."""
    from ..dcslog import LsoGrade
    grade = LsoGrade.parse(dcs_grade).grade
    if wire is not None:
        return "trap"
    return {"B": "bolter", "WO": "waveoff"}.get(grade or "", "unknown")


def grade_pass(p: PassResult) -> GradeResult:
    on_speed = AIRCRAFT[p.aircraft_type].on_speed_aoa
    stats = position_stats(p)
    remarks = [r for s in stats for r in remarks_for(s, on_speed)]
    not_counted = [s.position for s in stats if s.samples < MIN_SAMPLES]
    if p.outcome is Outcome.TRAP:
        remarks += touchdown_remarks(p)
        grade = _trap_grade(remarks)
    elif p.outcome is Outcome.BOLTER:
        grade = Grade.BOLTER
    elif p.outcome is Outcome.WAVEOFF:
        grade = Grade.WAVE_OFF
    elif p.outcome is Outcome.CRASH:
        grade = Grade.CUT  # as a real LSO grades an unsafe pass
    else:
        grade = Grade.NO_GRADE
    return GradeResult(GRADING_VERSION, grade, POINTS[grade], remarks, stats, not_counted, p.wire_estimate)
