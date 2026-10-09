"""Themes across a pilot's recent passes ("meta grading"): what keeps going wrong, consistent biases
below the remark thresholds, speed, outcomes, wires, the trend, and what's consistently good.

Works from stored grade details (`GradeResult.to_dict()`), so it needs no recordings.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .grade import ENGLISH, POSITION_ENGLISH, Position, Severity

DEFAULT_PASSES = 12
GROOVE = (Position.X, Position.IM, Position.IC, Position.AR)
# A fault is a theme when it shows up in at least this share of passes (and at least MIN_COUNT times).
FAULT_SHARE = 0.4
MIN_COUNT = 3
# A bias below the remark thresholds is a theme when the average is at least this far off and at least
# BIAS_CONSISTENCY of the passes are off the same way.
GLIDESLOPE_BIAS_DEG = 0.25
LINEUP_BIAS_DEG = 0.4
BIAS_CONSISTENCY = 0.7
AOA_BIAS_DEG = 0.2  # beyond the on-speed band
OUTCOME_SHARE = 0.2
TREND_POINTS = 0.5  # average points difference between the newer and older half
STRENGTH_SHARE = 0.8
MIN_PASSES_FOR_STRENGTHS = 5

GLIDESLOPE_CODES = ("HI", "LO")
LINEUP_CODES = ("LUL", "LUR")
SPEED_CODES = ("F", "SLO")


@dataclass(frozen=True, slots=True)
class TrendPass:
    """One graded pass, newest first in a list."""

    grade: str
    points: float
    outcome: str  # trap | bolter | waveoff | ...
    remarks: tuple[tuple[str, str, int], ...]  # (code, position, severity)
    # Per position: (glideslope deg, lineup deg, AOA deg), each may be None.
    positions: dict[str, tuple[float | None, float | None, float | None]]
    on_speed: tuple[float, float]
    wire: int | None = None  # DCS's, else the estimate

    @classmethod
    def from_detail(cls, detail: dict, outcome: str, on_speed: tuple[float, float],
                    wire: int | None = None) -> TrendPass:
        sev = {s.name.lower(): int(s) for s in Severity}
        return cls(
            grade=detail.get("grade", ""),
            points=float(detail.get("points") or 0.0),
            outcome=outcome,
            remarks=tuple((r["code"], r["position"], sev.get(r.get("severity", "normal"), 2))
                          for r in detail.get("remarks", [])),
            positions={str(p["position"]): (p.get("glideslope_deg"), p.get("lineup_deg"), p.get("aoa"))
                       for p in detail.get("positions", [])},
            on_speed=on_speed,
            wire=wire if wire is not None else detail.get("wire_estimate"),
        )


# Themes about how the passes were flown; the rest are about their results.
ANALYSIS_KINDS = ("fault", "bias", "speed", "trend")
RESULT_KINDS = ("outcome", "wires")


@dataclass(frozen=True, slots=True)
class Theme:
    kind: str  # fault | bias | speed | trend (analysis); outcome | wires (results)
    text: str
    count: int  # passes it applies to
    of: int  # passes looked at

    @property
    def share(self) -> float:
        return self.count / self.of if self.of else 0.0


def _theme(kind: str, text: str, matching: list[TrendPass], of: int) -> Theme:
    return Theme(kind, text, len(matching), of)


@dataclass
class Trends:
    passes: int
    average_points: float | None
    grades: dict[str, int] = field(default_factory=dict)
    themes: list[Theme] = field(default_factory=list)  # most common first
    strengths: list[str] = field(default_factory=list)

    @property
    def analysis(self) -> list[Theme]:
        return [t for t in self.themes if t.kind in ANALYSIS_KINDS]

    @property
    def results(self) -> list[Theme]:
        return [t for t in self.themes if t.kind in RESULT_KINDS]

    def to_dict(self) -> dict:
        def theme(t: Theme) -> dict:
            return {"kind": t.kind, "text": t.text, "count": t.count, "of": t.of}
        return {"passes": self.passes, "average_points": self.average_points, "grades": self.grades,
                "analysis": [theme(t) for t in self.analysis], "results": [theme(t) for t in self.results],
                "strengths": self.strengths}


def _where(positions: list[str]) -> str:
    """"in close", "in the middle and in close", "from the start to the ramp"."""
    order = [p.value for p in (*GROOVE, Position.IW)]
    positions = sorted(positions, key=order.index)
    words = [POSITION_ENGLISH[Position(p)] for p in positions]
    idx = [order.index(p) for p in positions]
    if len(words) >= 3 and idx == list(range(idx[0], idx[-1] + 1)):
        return f"from {words[0].removeprefix('at ')} to {words[-1].removeprefix('at ').removeprefix('in ')}"
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _fault_themes(passes: list[TrendPass]) -> list[Theme]:
    n = len(passes)
    need = max(MIN_COUNT, FAULT_SHARE * n)
    themes = []
    for code, words in ENGLISH.items():
        per_position: dict[str, int] = {}
        for p in passes:
            for pos in {pos for c, pos, _ in p.remarks if c == code}:
                per_position[pos] = per_position.get(pos, 0) + 1
        frequent = [pos for pos, count in per_position.items() if count >= need]
        if not frequent:
            continue
        hits = [p for p in passes if any(c == code and pos in frequent for c, pos, _ in p.remarks)]
        lots = sum(any(c == code and pos in frequent and s >= Severity.LOT for c, pos, s in p.remarks) for p in hits)
        name = words if isinstance(words, str) else words[1]
        text = f"{name} {_where(frequent)}: {len(hits)} of {n} passes"
        if lots and not isinstance(words, str):
            text += f" ({lots} of them {words[2][0].lower() + words[2][1:]})"
        themes.append(_theme("fault", text, hits, n))
    return themes


def _bias_themes(passes: list[TrendPass], faults: list[Theme]) -> list[Theme]:
    """Consistent leanings too small for remarks (or not already a fault theme)."""
    n = len(passes)
    themes = []
    covered = {t.text.split(" ")[0] for t in faults}  # e.g. "High", "Lined"
    for index, threshold, (plus, minus), unit_words in (
            (0, GLIDESLOPE_BIAS_DEG, ("a little high", "a little low"), ("High", "Low")),
            (1, LINEUP_BIAS_DEG, ("lined up a little right", "lined up a little left"), ("Lined", "Lined"))):
        leaning: dict[str, list[str]] = {"+": [], "-": []}
        values: dict[str, list[float]] = {"+": [], "-": []}
        for pos in GROOVE:
            vals = [p.positions[pos.value][index] for p in passes
                    if pos.value in p.positions and p.positions[pos.value][index] is not None]
            if len(vals) < max(MIN_COUNT, n / 2):
                continue
            mean = statistics.fmean(vals)
            sign = "+" if mean > 0 else "-"
            same = sum((v > 0) == (mean > 0) for v in vals) / len(vals)
            if abs(mean) >= threshold and same >= BIAS_CONSISTENCY:
                leaning[sign].append(pos.value)
                values[sign].append(mean)
        for sign, positions in leaning.items():
            if not positions:
                continue
            leaners = [p for p in passes if any(
                pos in p.positions and p.positions[pos][index] is not None
                and (p.positions[pos][index] > 0) == (sign == "+") for pos in positions)]
            words = plus if sign == "+" else minus
            word = unit_words[0] if sign == "+" else unit_words[1]
            if index == 0 and word in covered:
                continue  # already a fault theme
            if index == 1 and any(t.text.startswith("Lined up " + ("right" if sign == "+" else "left")) for t in faults):
                continue
            avg = statistics.fmean(values[sign])
            themes.append(_theme("bias", f"Tends to be {words} {_where(positions)} ({avg:+.1f}° on average)",
                                 leaners, n))
    return themes


def _speed_theme(passes: list[TrendPass], faults: list[Theme]) -> Theme | None:
    if any(t.text.split(" ")[0] in ("Fast", "Slow") for t in faults):
        return None
    offsets, flown = [], []
    for p in passes:
        aoas = [v[2] for pos, v in p.positions.items() if pos in {g.value for g in GROOVE} and v[2] is not None]
        if aoas:
            low, high = p.on_speed
            mean = statistics.fmean(aoas)
            offsets.append(mean - low if mean < low else mean - high if mean > high else 0.0)
            flown.append(p)
    if len(offsets) < MIN_COUNT:
        return None
    avg = statistics.fmean(offsets)
    fast = sum(o < 0 for o in offsets)
    slow = sum(o > 0 for o in offsets)
    if avg <= -AOA_BIAS_DEG and fast / len(offsets) >= BIAS_CONSISTENCY - 0.2:
        return _theme("speed", f"Tends to fly a little fast: below on-speed AOA in {fast} of {len(offsets)} passes",
                      [p for p, o in zip(flown, offsets) if o < 0], len(offsets))
    if avg >= AOA_BIAS_DEG and slow / len(offsets) >= BIAS_CONSISTENCY - 0.2:
        return _theme("speed", f"Tends to fly a little slow: above on-speed AOA in {slow} of {len(offsets)} passes",
                      [p for p, o in zip(flown, offsets) if o > 0], len(offsets))
    return None


def _outcome_themes(passes: list[TrendPass]) -> list[Theme]:
    n = len(passes)
    themes = []
    for outcome, label in (("bolter", "Bolters"), ("waveoff", "Wave-offs")):
        matching = [p for p in passes if p.outcome == outcome]
        if len(matching) >= 2 and len(matching) / n >= OUTCOME_SHARE:
            themes.append(_theme("outcome", f"{label}: {len(matching)} of {n} passes", matching, n))
    return themes


def _wire_theme(passes: list[TrendPass]) -> Theme | None:
    traps = [p for p in passes if p.outcome == "trap" and p.wire]
    wires = [p.wire for p in traps]
    if len(wires) < MIN_COUNT:
        return None
    short = sum(w <= 2 for w in wires)
    long = sum(w >= 4 for w in wires)
    target = sum(w == 3 for w in wires)
    spread = ", ".join(f"#{w}: {wires.count(w)}" for w in sorted(set(wires)))
    if short / len(wires) >= 0.6:
        return _theme("wires", f"Landing short: {short} of {len(wires)} traps on the 1 or 2 wire ({spread})",
                      [p for p in traps if p.wire <= 2], len(wires))
    if long / len(wires) >= 0.4:
        return _theme("wires", f"Landing long: {long} of {len(wires)} traps on the 4 wire ({spread})",
                      [p for p in traps if p.wire >= 4], len(wires))
    return _theme("wires", f"Target 3 wire in {target} of {len(wires)} traps ({spread})",
                  [p for p in traps if p.wire == 3], len(wires))


def _trend_theme(passes: list[TrendPass]) -> Theme | None:
    n = len(passes)
    if n < 6:
        return None
    newer, older = passes[: n // 2], passes[n // 2:]
    a, b = statistics.fmean(p.points for p in newer), statistics.fmean(p.points for p in older)
    if a - b >= TREND_POINTS:
        return Theme("trend", f"Improving: {a:.1f} points on average over the last {len(newer)} passes, "
                              f"up from {b:.1f}", len(newer), n)
    if b - a >= TREND_POINTS:
        return Theme("trend", f"Slipping: {a:.1f} points on average over the last {len(newer)} passes, "
                              f"down from {b:.1f}", len(newer), n)
    return None


def _strengths(passes: list[TrendPass], themes: list[Theme]) -> list[str]:
    """Areas with (almost) no remarks, and no theme about them either."""
    n = len(passes)
    if n < MIN_PASSES_FOR_STRENGTHS:
        return []
    texts = [t.text.lower() for t in themes]
    out = []
    for codes, text, words in ((GLIDESLOPE_CODES, "Glideslope control", ("high", "low")),
                               (LINEUP_CODES, "Lineup", ("lined up",)),
                               (SPEED_CODES, "Speed (AOA)", ("fast", "slow"))):
        if any(re.search(rf"\b{w}\b", t) for t in texts for w in words):  # whole words: "below" isn't "low"
            continue
        clean = sum(not any(c in codes for c, _, _ in p.remarks) for p in passes)
        if clean / n >= STRENGTH_SHARE:
            out.append(f"{text}: no remarks in {clean} of {n} passes")
    return out


def trends(passes: list[TrendPass]) -> Trends:
    """Themes for a pilot's passes (newest first), most common first."""
    n = len(passes)
    if not n:
        return Trends(0, None)
    grades: dict[str, int] = {}
    for p in passes:
        grades[p.grade] = grades.get(p.grade, 0) + 1
    faults = _fault_themes(passes)
    themes = faults + _bias_themes(passes, faults)
    for extra in (_speed_theme(passes, faults), *_outcome_themes(passes), _wire_theme(passes)):
        if extra is not None:
            themes.append(extra)
    themes.sort(key=lambda t: (-t.share, t.kind != "fault"))
    if (trend := _trend_theme(passes)) is not None:
        themes.append(trend)
    return Trends(n, round(statistics.fmean(p.points for p in passes), 2), grades, themes,
                  _strengths(passes, themes))
