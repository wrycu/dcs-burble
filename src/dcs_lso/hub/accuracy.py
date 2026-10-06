"""How far a landing's grade, wire and calls can be trusted (PLAN #31, docs/accuracy-scores.png): which parts
reported it (a server agent, the pilot hook, an uploaded recording, DCS's LSO through carrier comms), and from
those four scores, each Full / High / Medium / Low / None:

- approach: the pilot's own track (recorded AOA: the pilot hook, a listen server's host, an own Tacview file)
  against a recorded carrier is Full; the server's copy of the jet (AOA from motion, about 5 Hz) is High; no
  carrier, DCS's grade only, is None.
- wire (traps): DCS's is Full; our estimate from the pilot's own track is High; otherwise None.
- comms: the LSO's live calls on the card (made by a server agent; relayed to other hubs by the pilot hook).
- overall: the approach score (a missing wire has its own score), Low for DCS's grade only.

And where it was flown: on this hub's servers (a server agent reported it, or the pilot hook was recognised as
a player on them, or said this hub was the server's when it was flown), on another server, in single player, or
unknown (uploaded recordings, pilot hooks before version 3).

Worked out at grading and stored with the grade (`detail["accuracy"]`), so a regrade keeps it current.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Pass

LEVELS = ("Full", "High", "Medium", "Low", "None", "n/a")
FLOWN_LABELS = {"here": "this hub's servers", "elsewhere": "another server", "single player": "single player",
                "unknown": "unknown"}


@dataclass(frozen=True, slots=True)
class Part:
    name: str  # "Server report", "Pilot hook", "Uploaded recording", "DCS comms"
    present: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class Score:
    level: str  # one of LEVELS
    note: str = ""


@dataclass(frozen=True, slots=True)
class Accuracy:
    parts: list[Part]
    overall: Score
    approach: Score
    wire: Score
    comms: Score
    flown: Score  # level: "here" | "elsewhere" | "single player" | "unknown"; note: e.g. the server agent

    def to_dict(self) -> dict:
        return {"parts": [{"name": p.name, "present": p.present, "note": p.note} for p in self.parts],
                **{k: {"level": s.level, "note": s.note} for k, s in self.scores().items()},
                "flown": {"where": self.flown.level, "note": self.flown.note}}

    @classmethod
    def from_dict(cls, d: dict) -> Accuracy:
        def score(key: str) -> Score:
            return Score(d[key]["level"], d[key].get("note", ""))
        return cls([Part(x["name"], bool(x["present"]), x.get("note", "")) for x in d["parts"]],
                   score("overall"), score("approach"), score("wire"), score("comms"),
                   Score(d["flown"]["where"], d["flown"].get("note", "")))

    @property
    def flown_label(self) -> str:
        return FLOWN_LABELS.get(self.flown.level, self.flown.level)

    def scores(self) -> dict[str, Score]:
        return {"overall": self.overall, "approach": self.approach, "wire": self.wire, "comms": self.comms}


def _info(r: Pass) -> dict:
    return (r.slice.sidecar or {}).get("pass") or {}


def _is_pilot_hook(r: Pass) -> bool:
    return "pilot_hook" in (r.slice.sidecar or {})


def _rate(r: Pass) -> str:
    rate = _info(r).get("sample_rate_hz")
    return f"about {rate:.0f} Hz" if rate else ""


def landing_accuracy(landing: Pass, reports: list[Pass]) -> Accuracy:
    """`reports`: every report of the landing (`Hub.reports`), the landing first."""
    server = [r for r in reports if r.source is not None and r.source.kind == "server" and not _is_pilot_hook(r)]
    hook = [r for r in reports if _is_pilot_hook(r)]
    uploaded = [r for r in reports if r not in server and r not in hook]
    parts = [Part("Server report", bool(server), ", ".join(sorted({r.source.name for r in server}))),
             Part("Pilot hook", bool(hook))]
    if uploaded:
        parts.append(Part("Uploaded recording", True))
    parts.append(Part("DCS comms", bool(landing.dcs_grade), "DCS's LSO graded it" if landing.dcs_grade else ""))

    # The track graded: the most detailed one (as Hub.load_pass picks it), among reports that have the carrier.
    gradable = [r for r in reports if not r.is_track]
    tracks = [r for r in reports if not r.is_dcs_only] or reports
    best = max(tracks, key=lambda r: (bool(_info(r).get("aoa_recorded")), float(_info(r).get("sample_rate_hz") or 0)))
    own = bool(_info(best).get("aoa_recorded"))
    rate = f"{_rate(best)}, " if _rate(best) else ""
    if landing.is_dcs_only or not gradable:
        approach = Score("None", "no carrier in any report: DCS's grade only")
    elif own:
        who = "the pilot hook's track" if _is_pilot_hook(best) else "the jet's own track"
        approach = Score("Full", f"{who} ({rate}recorded AOA)")
    else:
        approach = Score("High", f"the server's copy of the jet ({rate}AOA from motion)")

    estimate = ((landing.grade.detail or {}) if landing.grade else {}).get("wire_estimate")
    if landing.outcome != "trap":
        wire = Score("n/a", "not a trap")
    elif landing.wire is not None:
        wire = Score("Full", f"#{landing.wire}, from DCS")
    elif estimate is not None:
        wire = Score("High", f"#{estimate}, estimated from where the jet stopped")
    elif own and approach.level == "Full":
        wire = Score("None", "the estimate declined: the stop wasn't clearly at one wire")
    else:
        wire = Score("None", "needs carrier comms (DCS's wire) or the pilot's own track (our estimate)")

    calls = landing.calls or []
    relayed = (landing.slice.sidecar or {}).get("calls_from")
    if calls:
        comms = Score("Full", f"{len(calls)} live call{'s' if len(calls) != 1 else ''}"
                      + (f", relayed from {relayed}'s server" if relayed else ""))
    else:
        comms = Score("None", "no live calls (made by a server agent with callouts on)")

    overall = Score("Low", "DCS's grade only") if approach.level == "None" else Score(approach.level)
    return Accuracy(parts, overall, approach, wire, comms, _flown(server, hook))


def _flown(server: list[Pass], hook: list[Pass]) -> Score:
    from .service import PILOT_HOOKS
    hook_info = [(r, (r.slice.sidecar or {}).get("pilot_hook") or {}) for r in hook]
    if server:
        return Score("here", ", ".join(sorted({r.source.name for r in server})))
    if any(r.source is not None and r.source.name == PILOT_HOOKS for r, _ in hook_info):
        return Score("here", "the pilot hook was recognised as a player on this hub's servers")
    if any(info.get("here") is True for _, info in hook_info):
        return Score("here", "the pilot hook was on this hub's server")
    if any(info.get("here") is False and info.get("server") for _, info in hook_info):
        return Score("elsewhere")
    if hook_info and all(not info.get("server") for _, info in hook_info):
        return Score("single player")
    return Score("unknown")


def accuracy_of(landing: Pass, reports: list[Pass] | None = None) -> Accuracy | None:
    """As stored with the landing's grade; worked out now for grades from before it was (`reports` needed)."""
    stored = ((landing.grade.detail or {}) if landing.grade else {}).get("accuracy")
    if stored:
        try:
            return Accuracy.from_dict(stored)
        except (KeyError, TypeError):
            pass
    return landing_accuracy(landing, reports) if reports is not None else None
