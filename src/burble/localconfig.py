"""This machine's settings: `~/.burble/config.toml` (or `$BURBLE_HOME/config.toml`), written with every default on
first use. Each command's options are set there by section; a flag on the command line wins, then the file, then the
default. Things under `~/.burble` by default: the hub's data, the agent's work folder, voice clips, and models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.toml"


def home() -> Path:
    """Burble's folder on this machine: `$BURBLE_HOME`, else ~/.burble."""
    return Path(os.environ.get("BURBLE_HOME") or "~/.burble").expanduser()


@dataclass(frozen=True, slots=True)
class Option:
    """A command's option that the config file may set: `key` in its `section` (also the flag's dest)."""

    section: str
    key: str
    kind: str  # "str", "int", "float", "bool", "path", or "list"
    default: object = None  # a value, or a function of the options resolved before it (an argparse.Namespace)
    shown: str | None = None  # the default as the config file shows it, when `default` is a function
    help: str = ""
    template: bool = True  # listed in the config file written on first use
    choices: tuple | None = None


# Every option the config file may set, by section, in the order the written file lists them.
SECTIONS: dict[str, list[Option]] = {}
SECTION_NOTES = {
    "hub": "The hub: `burble hub` serves it; `burble hub <command>` manages it.",
    "agent": ("The server agent: `burble agent`. For several DCS servers on one machine, give each a table of its "
              "own after this one (`burble agent server1` reads [agent], then [agent.server1] over it), e.g.\n"
              "[agent.server1]\ntoken = \"...\"\ndcs_log = \"~/.wine/drive_c/DCS_configs/DCS.server1/Logs/dcs.log\""),
    "pilot_uploader": "The pilot uploader: `burble pilot-uploader` (your own jet's passes from your PC).",
    "upload": "Uploading recordings by hand: `burble upload <recordings>`.",
    "voice": "Building the LSO's voice clips: `burble voice build`.",
    "telemetry": "A usage report receiver: `burble telemetry` serves it.",
}


def add(parser: argparse.ArgumentParser, section: str, *flags: str, kind: str = "str", default: object = None,
        shown: str | None = None, help: str = "", template: bool = True, **kwargs) -> Option:
    """An argparse option (or optional positional) that the config file's `section` can also set. Its argparse
    default is None, so `resolve` can tell it wasn't given."""
    positional = not flags[0].startswith("-")
    key = flags[0] if positional else kwargs.pop("dest", flags[0].lstrip("-").replace("-", "_"))
    choices = tuple(kwargs["choices"]) if kwargs.get("choices") else None
    option = Option(section, key, kind, default, shown, help, template, choices)
    shown_default = shown if shown is not None else _show(default)
    text = help + (f" (default: {shown_default})" if shown_default not in (None, "") else "")
    text += f" [{section}.{key}]"
    if kind == "bool":
        kwargs.setdefault("action", argparse.BooleanOptionalAction)
    elif kind == "list":
        kwargs.setdefault("action", "append")
    elif kind in ("int", "float"):
        kwargs.setdefault("type", int if kind == "int" else float)
    if positional:
        parser.add_argument(key, nargs="?", default=None, help=text, **kwargs)
    else:
        parser.add_argument(*flags, dest=key, default=None, help=text, **kwargs)
    options = parser.get_default("_options") or []
    parser.set_defaults(_options=[*options, option], _section=section)
    known = SECTIONS.setdefault(section, [])
    if template and all(o.key != key for o in known):
        known.append(option)
    return option


def _show(value: object) -> str | None:
    if value is None or callable(value):
        return None
    if isinstance(value, Path):
        return _home_relative(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return str(value)


def _home_relative(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _toml(value: object, kind: str) -> str:
    if kind == "path":
        return json.dumps(_home_relative(Path(value)))
    if kind == "bool":
        return "true" if value else "false"
    if kind == "list":
        return json.dumps(list(value))
    if kind in ("int", "float"):
        return str(value)
    return json.dumps(str(value))


def template() -> str:
    """The config file written on first use: every setting, each at its default and commented out."""
    def comment(text: str) -> list[str]:
        return textwrap.wrap(text, 116, initial_indent="# ", subsequent_indent="# ", break_on_hyphens=False)

    lines = comment("Burble's settings on this machine. Each command reads its own section; a flag on the command "
                    "line wins over this file. The server agents' LSO settings (calls, frequencies, voices) are on "
                    "the hub instead: each agent's Settings page, on /admin.")
    lines += ["#"] + comment("Every setting is listed below at its default, commented out with a bare #, like "
                             "this:") + ["#", '#   #port = 8000', "#"]
    lines += comment("Delete that # to change one (\"\" is unset). Lines starting \"# \" (a space after the #) "
                     "are notes. Paths may start with ~.")
    order = [s for s in SECTION_NOTES if s in SECTIONS] + [s for s in SECTIONS if s not in SECTION_NOTES]
    for section in order:
        notes = SECTION_NOTES.get(section, "").split("\n")
        lines += ["", "", "# " + "=" * 114, f"# [{section}]"] + comment(notes[0])
        lines += [f"#   {line}" for line in notes[1:]] + ["# " + "=" * 114, f"[{section}]"]
        for o in SECTIONS[section]:
            lines.append("")
            if o.help:
                lines += comment(f"{o.help[:1].upper()}{o.help[1:]}.")
            if o.shown is not None:
                value = o.shown
            elif o.default is None:
                value = "\"\""
            else:
                value = _toml(o.default, o.kind)
            lines.append(f"#{o.key} = {value}".rstrip())
    return "\n".join(lines) + "\n"


def load(path: str | Path | None = None) -> tuple[dict, Path]:
    """The config file (`path`, else ~/.burble/config.toml) as a dict, and where it is. The default one is written
    (with `template`) if there's none yet; a `path` given that doesn't exist is an error."""
    if path is None:
        path = home() / CONFIG_NAME
        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(template(), encoding="utf-8")
            path.chmod(0o600)  # it holds tokens and webhooks
            print(f"wrote Burble's settings, every one at its default, to {path}", file=sys.stderr)
    path = Path(path).expanduser()
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), path
    except FileNotFoundError:
        raise SystemExit(f"error: no config file {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"error: {path}: {exc}") from None


def _convert(value: object, kind: str, where: str) -> object:
    try:
        if kind == "path":
            if not isinstance(value, (str, Path)):
                raise TypeError
            return Path(value).expanduser()
        if kind == "bool":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError
            return value
        if kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            return float(value)
        if kind == "list":
            if isinstance(value, str):
                return [value]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise TypeError
            return value
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise TypeError
        return str(value)
    except TypeError:
        what = {"path": "a path", "bool": "true or false", "int": "a whole number", "float": "a number",
                "list": "a list of names", "str": "text"}[kind]
        raise SystemExit(f"error: {where} should be {what} (it's {value!r})") from None


def section_table(config: dict, section: str, instance: str | None, path: Path) -> dict:
    """`section`'s settings in the config file, with `instance`'s own table (`[section.instance]`) over them."""
    table = config.get(section) or {}
    if not isinstance(table, dict):
        raise SystemExit(f"error: {path}: [{section}] should be a table")
    keys = {o.key for o in SECTIONS.get(section, [])}
    merged = {}
    for key, value in table.items():
        if isinstance(value, dict):
            continue  # an instance's own table
        if key not in keys:
            raise SystemExit(f"error: {path}: no setting {key!r} in [{section}] (it has {', '.join(sorted(keys))})")
        merged[key] = value
    if instance is not None:
        own = table.get(instance) or {}
        if not isinstance(own, dict):
            raise SystemExit(f"error: {path}: [{section}.{instance}] should be a table")
        for key, value in own.items():
            if key not in keys:
                raise SystemExit(f"error: {path}: no setting {key!r} in [{section}.{instance}] (it has "
                                 f"{', '.join(sorted(keys))})")
            merged[key] = value
    return merged


def resolve(args: argparse.Namespace, config: dict, path: Path) -> argparse.Namespace:
    """Fill in each of the command's options that wasn't given on the command line: from the config file, else its
    default. `args._given[key]` says where each came from ("flag", "config", or "default")."""
    options: list[Option] = getattr(args, "_options", None) or []
    if not options:
        return args
    for name in config:
        if name not in SECTIONS:  # a misspelt section, or a setting above the first one, would do nothing
            what = f"section [{name}]" if isinstance(config[name], dict) else f"setting {name!r} outside a section"
            raise SystemExit(f"error: {path}: no {what} (the sections are {', '.join(SECTIONS)})")
    section = args._section
    instance = getattr(args, "instance", None)
    table = section_table(config, section, instance, path)
    given = {}
    for o in options:
        value = getattr(args, o.key, None)
        if value is not None:
            given[o.key] = "flag"
            if o.kind == "path":
                value = Path(value).expanduser()
        elif o.key in table and table[o.key] != "":
            given[o.key] = "config"
            value = _convert(table[o.key], o.kind, f"{path}: {section}.{o.key}")
            if o.choices and value not in o.choices:
                raise SystemExit(f"error: {path}: {section}.{o.key} should be one of {', '.join(o.choices)} "
                                 f"(it's {value!r})")
        else:
            given[o.key] = "default"
            value = o.default(args) if callable(o.default) else o.default
            if isinstance(value, tuple):
                value = list(value)
        setattr(args, o.key, value)
    args._given = given
    args._config_path = path
    return args


def if_exists(path: Callable[[], Path]) -> Callable[[argparse.Namespace], Path | None]:
    """A default that is `path()` when there's something there, else None (e.g. an optional model)."""
    def default(_args: argparse.Namespace) -> Path | None:
        p = path()
        return p if p.exists() else None
    return default
