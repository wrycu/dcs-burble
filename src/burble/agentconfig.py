"""What a server agent reads from its configuration on the hub (PLAN #43), in one place: for the settings editor on
/admin (`apply_form`), checking a configuration before it's saved (`problems`), and the reference page for the wiki
(`reference_markdown`, `burble hub config-reference`).

The agent reads these keys itself (agent/callouts.py `CalloutSettings.from_config`, agent/livefeed.py `live_settings`,
agent/lights.py `lights_settings`, agent/service.py `Agent.retention`); the defaults here are checked against those by
the tests. Keys the agent doesn't read are kept as they are.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields

from .callouts.rules import NM, Call, Thresholds
from .srs.packet import Modulation


@dataclass(frozen=True, slots=True)
class Setting:
    path: tuple[str, ...]  # e.g. ("callouts", "srs", "host")
    kind: str  # "bool", "text", "int", "number", or "choice"
    default: object
    label: str
    help: str
    choices: tuple[tuple[object, str], ...] = ()  # (value, label) for "choice"
    minimum: float | None = None

    @property
    def key(self) -> str:
        return ".".join(self.path)


MODULATIONS = (("AM", "AM"), ("FM", "FM"))
CASES = (("auto", "Auto: Case III at night or in low weather"), ("I", "Always Case I"), ("III", "Always Case III"))
COALITIONS = ((2, "Blue"), (1, "Red"), (0, "Spectator"))

# The settings with a field of their own on the form, in its order (by section). Carriers, calls, and thresholds
# have their own parts of the form.
SECTIONS: tuple[tuple[str, tuple[Setting, ...]], ...] = (
    ("Live calls", (
        Setting(("callouts", "enabled"), "bool", False, "Make live LSO calls over SRS",
                "Off: the agent still records passes and sends them to the hub, but says nothing."),
        Setting(("callouts", "srs", "host"), "text", "127.0.0.1", "SRS server address",
                "As the agent's machine reaches it."),
        Setting(("callouts", "srs", "port"), "int", 5002, "SRS server port", "", minimum=1),
        Setting(("callouts", "srs", "coalition"), "choice", 2, "Coalition",
                "The side the LSO joins SRS on; pilots hear it only on their own side's radios.", COALITIONS),
        Setting(("callouts", "srs", "name"), "text", "LSO", "Name in SRS",
                "How the LSO shows in players' SRS client lists (\" (recording)\" is added while recording the "
                "radio)."),
        Setting(("callouts", "frequency_mhz"), "number", 127.5, "Default LSO frequency (MHz)",
                "For a carrier with no frequency of its own (carriers' frequencies) and none set in the mission (the "
                "server hook reports the mission's).", minimum=1),
        Setting(("callouts", "modulation"), "choice", "AM", "Default modulation", "", MODULATIONS),
        Setting(("callouts", "voice"), "text", None, "Voice",
                "Which clip set the LSO speaks with, when the agent's --voice-dir is a folder of them (its folder "
                "name). Unset or unknown: the first by name."),
        Setting(("callouts", "case"), "choice", "auto", "Recovery case",
                "Case III: \"Paddles contact\" coming down final, then \"call the ball\" at three quarters of a mile. "
                "Auto uses it at night, or with a ceiling under 1,000 ft or visibility under 5 nm in the mission's "
                "weather.", CASES),
    )),
    ("Listening and recording", (
        Setting(("callouts", "listen"), "bool", False, "Listen to pilots and answer them",
                "\"Roger ball\" with the wind, \"Roger, Clara\" with a talk-down, and radio checks. Needs the agent "
                "started with --listen-model."),
        Setting(("callouts", "record_radio"), "bool", True, "Record the carrier frequency's radio with each pass",
                "Played in the pass's replay: the pass's own pilot (only if their SRS client has \"Allow "
                "Transmissions To Be Recorded\" on) and the LSO, nobody else. Tell your players, e.g. in the "
                "server's description: what they say on the carrier frequency is published with their passes."),
    )),
    ("Live view", (
        Setting(("live", "enabled"), "bool", True, "Send the live view to the hub",
                "Aircraft around each carrier, for signed-in pilots watching on the hub's Live page."),
        Setting(("live", "radius_nm"), "number", 10.0, "Around each carrier (nm)",
                "Aircraft within this distance of a carrier (and below 10,000 ft) are sent.", minimum=0.1),
    )),
    ("Carrier lights", (
        Setting(("lights", "enabled"), "bool", True, "Turn the carrier's landing lights on for Case III recoveries",
                "At night DCS's Supercarrier keeps its deck dark unless a pilot uses its carrier ATC. With this on, a "
                "carrier is set to recovery lighting while it's Case III there (as the recovery case above decides) "
                "and an aircraft is recovering to it, then back as the mission had it (its own lighting trigger, "
                "else auto). Needs the agent started with --dcs-log and the current server hook."),
        Setting(("lights", "radius_nm"), "number", 10.0, "Recovering within (nm)",
                "An aircraft this close to the carrier, astern of it, pointed at it, and below 5,000 ft above its deck "
                "counts as recovering.", minimum=0.1),
        Setting(("lights", "hold_s"), "number", 120.0, "Keep them on for (seconds)",
                "After the last aircraft recovering, for a bolter or the next one in.", minimum=0),
    )),
    ("Keeping files on the agent's machine", (
        Setting(("retention", "archives_days"), "number", 90.0, "Session recordings (days)",
                "Whole Tacview sessions the agent archived; 0 keeps them for good.", minimum=0),
        Setting(("retention", "sent_days"), "number", 14.0, "Passes already sent to the hub (days)",
                "The hub keeps its own copy; 0 keeps them for good.", minimum=0),
        Setting(("retention", "rejected_days"), "number", 30.0, "Passes the hub refused (days)",
                "0 keeps them for good.", minimum=0),
    )),
)
SETTINGS = tuple(s for _, group in SECTIONS for s in group)

# The calls a server can turn off, grouped for the form's checklist. The welcome's variants that name the wire, and
# the dig after a rough trap, go with "welcome aboard" (ALWAYS_CALLS: kept in a list of calls, never on the form).
CALL_GROUPS: tuple[tuple[str, tuple[Call, ...]], ...] = (
    ("Wave-offs", (Call.WAVE_OFF, Call.WAVE_OFF_FOUL_DECK, Call.WAVE_OFF_GEAR)),
    ("Glideslope", (Call.POWER_X3, Call.POWER_X2, Call.POWER, Call.LOW, Call.HIGH, Call.EASY_WITH_IT, Call.DONT_SETTLE,
                    Call.DONT_CLIMB, Call.GOING_LOW, Call.LITTLE_LOW, Call.LITTLE_HIGH, Call.GOING_HIGH)),
    ("Lineup", (Call.RIGHT_FOR_LINEUP, Call.COME_LEFT, Call.LITTLE_RIGHT, Call.LITTLE_LEFT, Call.DRIFTING_LEFT,
                Call.DRIFTING_RIGHT)),
    ("Speed and attitude", (Call.FAST, Call.SLOW, Call.EASY_WINGS, Call.EASY_NOSE)),
    ("Before and in the groove", (Call.KEEP_TURN_IN, Call.KEEP_IT_COMING, Call.ON_GLIDESLOPE, Call.PADDLES_CONTACT,
                                  Call.CALL_THE_BALL)),
    ("After touchdown", (Call.TRAPPED, Call.TRAPPED_WAVED_OFF, Call.BOLTER)),
    ("Answers, with listening on", (Call.ROGER_BALL, Call.ROGER_CLARA, Call.LOUD_AND_CLEAR)),
)
CHOOSABLE_CALLS = tuple(c for _, group in CALL_GROUPS for c in group)
ALWAYS_CALLS = tuple(c for c in Call if c not in CHOOSABLE_CALLS)
CALL_NOTES = {
    Call.WAVE_OFF_FOUL_DECK: "another aircraft in the landing area",
    Call.WAVE_OFF_GEAR: "gear up",
    Call.TRAPPED: "every welcome, with the wire when it's known, and now and then a dig after a rough trap",
    Call.TRAPPED_WAVED_OFF: "trapped through a wave-off",
    Call.KEEP_TURN_IN: "overshooting the turn to final",
    Call.KEEP_IT_COMING: "reassurance, when on glideslope and centerline",
    Call.ON_GLIDESLOPE: "the talk-down after \"Clara\"",
    Call.PADDLES_CONTACT: "Case III",
    Call.CALL_THE_BALL: "Case III",
    Call.LOUD_AND_CLEAR: "to a radio check (\"Paddles\")",
}

# Thresholds the form doesn't offer: set per aircraft (its on-speed band) or only used by replays.
HIDDEN_THRESHOLDS = frozenset({"aoa_fast", "aoa_slow", "default_call_s"})


def call_label(call: Call) -> str:
    return call.value[:1].upper() + call.value[1:]


@dataclass(frozen=True, slots=True)
class Threshold:
    name: str
    default: float
    unit: str
    note: str

    @property
    def is_int(self) -> bool:
        return isinstance(self.default, int)


# What each threshold does, for the form and the wiki (every one the form offers has a note; see the tests).
THRESHOLD_NOTES = {
    "groove_start_m": "Calls start inside this distance from the aim point, once in the groove.",
    "groove_heading_deg": "In the groove: pointing within this of the landing area...",
    "groove_roll_deg": "...with the wings within this of level...",
    "groove_settle_s": "...for this long...",
    "groove_always_inside_m": "...or, whatever the attitude, once this close.",
    "paddles_contact_m": "Case III: \"Paddles contact\" once inside this, inbound.",
    "call_the_ball_m": "Case III: \"call the ball\" inside this, if the pilot hasn't called it (or \"Clara\").",
    "ball_wait_s": "After \"call the ball\": only urgent calls for this long, or until the pilot answers.",
    "ball_reply_s": "After \"call the ball\": only wave-offs and urgent calls for this long (the start of the wait "
                    "above), while the pilot keys up to answer.",
    "clara_talk_s": "After \"Clara\": \"on glideslope\" when steady, this long after the last call.",
    "quiet_inside_m": "No calls at all inside this (at the ramp: too late to act on them).",
    "wave_off_inside_m": "Wave-offs for a dangerous pass are called inside this.",
    "gear_wave_off_inside_m": "\"Wave off, gear\" inside this with the gear up.",
    "foul_deck_inside_m": "\"Wave off, foul deck\" inside this with another aircraft in the landing area.",
    "in_close_m": "In close from here: \"power, power, power\", \"don't settle\", \"don't climb\".",
    "little_deg": "Off the glideslope by this much: \"a little low/high\".",
    "deviation_deg": "Off the glideslope by this much: \"you're low/high\".",
    "power_x3_deg": "This low in close: \"power, power, power\".",
    "wave_off_low_deg": "This low inside the wave-off distance: \"wave off\".",
    "sinking_deg_s": "A little low and sinking at least this fast: \"power\".",
    "going_deg_s": "On the glideslope but moving off it this fast: \"you're going low/high\".",
    "easy_with_it_deg_s": "Climbing this fast right after a power call: \"easy with it\".",
    "power_escalate_s": "Another power call within this long becomes \"power, power\".",
    "little_lineup_deg": "Off the centerline by this much: \"a little right for lineup / come left\".",
    "lineup_deg": "Off the centerline by this much: \"right for lineup\" / \"come left\".",
    "drifting_deg_s": "Drifting off the centerline this fast: \"you're drifting left/right\".",
    "wave_off_lateral_m": "This far off the centerline inside the wave-off distance: \"wave off\".",
    "easy_nose_deg_s": "Pitch changing this fast: \"easy with the nose\".",
    "easy_wings_deg": "Banked this much in the groove: \"easy with your wings\".",
    "aoa_hysteresis": "A fast or slow call stays on until the AOA is this far back inside the on-speed band.",
    "angle_hysteresis_deg": "A glideslope or lineup call stays on until it's this far back inside its band.",
    "correcting_deg_s": "Coming back at least this fast counts as correcting (the call isn't made).",
    "keep_coming_share": "\"Keep it coming\" within this share of the \"a little\" bands (0 to 1).",
    "keep_coming_hold_s": "...steady there for this long...",
    "keep_coming_after_s": "...with no call for this long.",
    "keep_coming_max": "At most this many \"keep it coming\" in a pass...",
    "keep_coming_from_m": "...said between this distance...",
    "keep_coming_to_m": "...and this one.",
    "overshoot_lateral_m": "Before the groove: past the extended centerline by this much and still turning away: "
                           "\"keep your turn in\"...",
    "overshoot_heading_deg": "...heading away from it by at least this...",
    "overshoot_from_m": "...between this distance...",
    "overshoot_to_m": "...and this one.",
    "hold_s": "A condition must last this long before it's called...",
    "lineup_hold_s": "...a lineup call this long...",
    "speed_hold_s": "...and a fast or slow call this long.",
    "repeat_s": "The same call isn't repeated until this long after it was said.",
    "spacing_s": "A new call waits this long after the last one finished.",
    "urgent_spacing_s": "...or only this long for power, \"you're low\", and \"don't settle\" (a wave-off "
                        "interrupts anything).",
    "rough_dig_chance": "How often a poor (No Grade) or cut trap's welcome gets a dig, 0 to 1.",
}


def thresholds() -> list[Threshold]:
    """The call thresholds a server can change, with their defaults, units, and notes."""
    defaults = Thresholds()
    return [Threshold(f.name, getattr(defaults, f.name), _unit(f.name), THRESHOLD_NOTES.get(f.name, ""))
            for f in fields(Thresholds) if f.name not in HIDDEN_THRESHOLDS]


def _unit(name: str) -> str:
    if name.endswith("_deg_s"):
        return "°/s"
    if name.endswith("_deg"):
        return "°"
    if name.endswith("_m"):
        return "m"
    if name.endswith("_s"):
        return "s"
    if name.startswith("aoa_"):
        return "° AOA"
    return ""


def show_number(value: float, unit: str = "") -> str:
    """A default for people: "1389 m (0.75 nm)", "0.35°"."""
    text = f"{value:.0f}" if abs(value) >= 100 else f"{value:g}"
    if unit == "m" and value >= 400:
        return f"{text} m ({value / NM:g} nm)"
    if not unit:
        return text
    return f"{text}{unit}" if unit in ("°", "°/s") else f"{text} {unit}"


# -- reading and checking ------------------------------------------------------------------------------------------

def get(config: Mapping | None, path: tuple[str, ...]) -> object:
    node: object = config or {}
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _check(setting: Setting, value: object) -> str | None:
    """Why `value` won't do for `setting`, or None."""
    if setting.kind == "bool":
        return None if isinstance(value, bool) else "true or false"
    if setting.kind == "text":
        return None if isinstance(value, str) else "text"
    if setting.kind in ("int", "number"):
        if not _number(value) or setting.kind == "int" and not float(value).is_integer():
            return "a whole number" if setting.kind == "int" else "a number"
        if setting.minimum is not None and value < setting.minimum:
            return f"at least {setting.minimum:g}"
        return None
    allowed = [c for c, _ in setting.choices]
    if setting.path[-1] == "case" and isinstance(value, str) and value.upper() in ("AUTO", "I", "III"):
        return None
    if setting.path[-1] == "modulation" and isinstance(value, str) and value in Modulation.__members__:
        return None
    return None if value in allowed and type(value) is type(allowed[0]) else \
        "one of " + ", ".join(str(c) for c in allowed)


def problems(config: object) -> list[str]:
    """What's wrong in a server agent's configuration, as the agent would read it (empty: nothing). Keys it doesn't
    read aren't checked."""
    if config is None:
        return []
    if not isinstance(config, dict):
        return ["the configuration is a JSON object"]
    out = []
    for section in ("callouts", "callouts.srs", "live", "lights", "retention", "callouts.carriers",
                    "callouts.thresholds"):
        value = get(config, tuple(section.split(".")))
        if value is not None and not isinstance(value, dict):
            out.append(f"{section}: should be an object")
    if out:
        return out
    for setting in SETTINGS:
        value = get(config, setting.path)
        if value is None:
            continue
        if (why := _check(setting, value)) is not None:
            out.append(f"{setting.key}: should be {why} (it's {_json(value)})")
    for unit, radio in (get(config, ("callouts", "carriers")) or {}).items():
        if not isinstance(radio, dict) or not _number(radio.get("frequency_mhz")) or radio["frequency_mhz"] <= 0:
            out.append(f"callouts.carriers.{unit}: should have a frequency_mhz (MHz)")
        elif "modulation" in radio and radio["modulation"] not in Modulation.__members__:
            out.append(f"callouts.carriers.{unit}.modulation: should be AM or FM (it's {_json(radio['modulation'])})")
    calls = get(config, ("callouts", "calls"))
    if calls is not None:
        known = {c.value for c in Call}
        if not isinstance(calls, list):
            out.append("callouts.calls: should be a list of calls")
        elif unknown := [c for c in calls if c not in known]:
            out.append(f"callouts.calls: no such call {', '.join(_json(c) for c in unknown)}")
    known_thresholds = {f.name for f in fields(Thresholds)}
    for name, value in (get(config, ("callouts", "thresholds")) or {}).items():
        if name not in known_thresholds:
            out.append(f"callouts.thresholds: no threshold {name!r}")
        elif not _number(value):
            out.append(f"callouts.thresholds.{name}: should be a number (it's {_json(value)})")
    return out


def _json(value: object) -> str:
    import json

    return json.dumps(value)


# -- the form --------------------------------------------------------------------------------------------------------

def field_name(setting: Setting) -> str:
    return setting.key


def apply_form(config: dict | None, form: Mapping[str, list[str]]) -> dict | None:
    """`config` with the settings editor's fields (`form`: each field's values) put in. A setting left at its default
    is left out (so a later default applies); keys the form doesn't cover are kept. ValueError naming the field if a
    value won't do."""
    out = copy.deepcopy(config) if isinstance(config, dict) else {}

    def one(name: str) -> str:
        values = form.get(name) or [""]
        return values[-1].strip()

    for setting in SETTINGS:
        raw = one(field_name(setting))
        if setting.kind == "bool":
            value: object = raw in ("1", "on", "true")
        elif raw == "":
            value = None
        elif setting.kind in ("int", "number"):
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{setting.label}: {raw!r} isn't a number") from None
            if setting.kind == "int" and not value.is_integer():
                raise ValueError(f"{setting.label}: should be a whole number")
            if value.is_integer():
                value = int(value)
            if setting.minimum is not None and value < setting.minimum:
                raise ValueError(f"{setting.label}: should be at least {setting.minimum:g}")
        elif setting.kind == "choice":
            value = next((c for c, _ in setting.choices if str(c) == raw), None)
            if value is None:
                raise ValueError(f"{setting.label}: {raw!r} isn't one of the choices")
        else:
            value = raw
        _put(out, setting.path, None if value == setting.default else value)

    carriers = {}
    for i in range(int(one("carriers") or 0)):
        unit = one(f"carrier.{i}.unit")
        if not unit:
            continue
        try:
            mhz = float(one(f"carrier.{i}.frequency_mhz"))
        except ValueError:
            raise ValueError(f"{unit}: its frequency isn't a number (MHz)") from None
        if mhz <= 0:
            raise ValueError(f"{unit}: its frequency isn't a frequency")
        modulation = one(f"carrier.{i}.modulation") or "AM"
        if modulation not in Modulation.__members__:
            raise ValueError(f"{unit}: {modulation!r} isn't a modulation")
        carriers[unit] = {"frequency_mhz": mhz} | ({"modulation": modulation} if modulation != "AM" else {})
    _put(out, ("callouts", "carriers"), carriers or None)

    chosen = set(form.get("call") or [])
    if all(c.value in chosen for c in CHOOSABLE_CALLS):
        calls = None  # every call, including any added later
    else:
        calls = [c.value for c in Call if c.value in chosen or c in ALWAYS_CALLS]
    _put(out, ("callouts", "calls"), calls)

    existing = get(out, ("callouts", "thresholds"))
    changed = {k: v for k, v in existing.items() if k in HIDDEN_THRESHOLDS} if isinstance(existing, dict) else {}
    for t in thresholds():
        raw = one(f"threshold.{t.name}")
        if raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{t.name}: {raw!r} isn't a number") from None
        if t.is_int and not value.is_integer():
            raise ValueError(f"{t.name}: should be a whole number")
        if value.is_integer():
            value = int(value)
        if not math.isclose(value, t.default, rel_tol=1e-9, abs_tol=1e-12):
            changed[t.name] = value
    _put(out, ("callouts", "thresholds"), changed or None)
    return out or None


def _put(config: dict, path: tuple[str, ...], value: object) -> None:
    """Set config[path] (None: remove it), dropping objects left empty."""
    if value is None:
        parents = []
        node = config
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                return
            parents.append((node, part))
            node = node[part]
        node.pop(path[-1], None)
        for parent, part in reversed(parents):
            if not parent[part]:
                del parent[part]
        return
    node = config
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[path[-1]] = value


# -- the reference ---------------------------------------------------------------------------------------------------

EXAMPLE = {
    "callouts": {
        "enabled": True,
        "srs": {"host": "127.0.0.1", "port": 5002},
        "carriers": {"CVN-75 Truman": {"frequency_mhz": 127.5}},
        "voice": "clips-ryan",
        "listen": True,
    },
    "retention": {"archives_days": 60},
}


def _show_default(setting: Setting) -> str:
    if setting.default is None:
        return "unset"
    if setting.kind == "bool":
        return "on" if setting.default else "off"
    if setting.kind == "choice":
        return f"`{_json(setting.default)}` ({dict(setting.choices)[setting.default].split(':')[0]})"
    return f"`{_json(setting.default)}`"


def _types(setting: Setting) -> str:
    if setting.kind == "choice":
        if all(isinstance(c, int) for c, _ in setting.choices):
            return ", ".join(f"`{c}` ({label.lower()})" for c, label in setting.choices)
        return " or ".join(f"`{_json(c)}`" for c, _ in setting.choices)
    return {"bool": "true/false", "text": "text", "int": "whole number", "number": "number"}[setting.kind]


def reference_markdown() -> str:
    """Every key a server agent reads from its configuration on the hub, for the wiki."""
    import json

    lines = [
        "# Server agent configuration",
        "",
        "Each server agent fetches its configuration from its hub and applies it when the next mission starts. Edit it "
        "on the hub's **Admin** page (a server agent's **Settings**), or set it from a JSON file with "
        "`burble hub set-config NAME FILE`. The hub checks it before saving; keys the agent doesn't read are kept.",
        "",
        "*This page is generated by `burble hub config-reference`.*",
        "",
        "## Example",
        "",
        "```json",
        json.dumps(EXAMPLE, indent=2),
        "```",
        "",
    ]
    for title, group in SECTIONS:
        lines += [f"## {title}", "", "| Key | Type | Default | What it does |", "|---|---|---|---|"]
        for s in group:
            lines.append(f"| `{s.key}` | {_types(s)} | {_show_default(s)} | {s.label}." + (f" {s.help}" if s.help else "")
                         + " |")
        if title == "Live calls":
            lines.append("| `callouts.carriers` | object | none | A frequency per carrier, by its unit name in the "
                         "mission: `{\"CVN-75 Truman\": {\"frequency_mhz\": 127.5, \"modulation\": \"AM\"}}`. Wins over "
                         "the mission's frequency. |")
            lines.append("| `callouts.calls` | list | every call | Which calls the LSO makes (see below). |")
            lines.append("| `callouts.thresholds` | object | none | When calls are made (see below). |")
        lines.append("")
    lines += ["## Calls (`callouts.calls`)", "",
              "Leave it out to make every call. Otherwise list the calls to make, by these names:", ""]
    for title, group in CALL_GROUPS:
        lines.append(f"- **{title}:** " + ", ".join(
            f"`{c.value}`" + (f" ({CALL_NOTES[c]})" if c in CALL_NOTES else "") for c in group) + ".")
    lines += ["", "The welcome's variants that name the wire and the dig after a rough trap go with "
              "`welcome aboard`: " + ", ".join(f"`{c.value}`" for c in ALWAYS_CALLS) + ".", "",
              "## Thresholds (`callouts.thresholds`)", "",
              "Each one left out keeps its default. Distances are in metres (1 nm = 1852 m) and angles in degrees. "
              "The AOA band is set per aircraft and can't be changed here.", "",
              "| Threshold | Default | Notes |", "|---|---|---|"]
    for t in thresholds():
        lines.append(f"| `{t.name}` | {show_number(t.default, t.unit)} | {t.note} |")
    return "\n".join(lines) + "\n"
