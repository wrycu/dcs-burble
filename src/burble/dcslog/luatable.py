"""Minimal parser for the Lua table literals DCS writes (debrief.log, options.lua, ...).

Supports top-level `name = value` assignments, nested tables with `[key] =`,
`name =` and positional entries, strings, numbers, booleans, nil and `--` comments.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--[^\n]*)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}\[\]=,;])
    """,
    re.VERBOSE,
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}


def _unescape(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body, flags=re.S)


def _tokenize(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ValueError(f"unexpected character {text[pos]!r} at offset {pos}")
        pos = m.end()
        kind = m.lastgroup
        value = m.group()
        if kind == "ws":
            continue
        if kind == "str":
            tokens.append(("value", _unescape(value[1:-1])))
        elif kind == "num":
            tokens.append(("value", float(value) if any(c in value for c in ".eE") else int(value)))
        elif kind == "name" and value in ("true", "false", "nil"):
            tokens.append(("value", {"true": True, "false": False, "nil": None}[value]))
        else:
            tokens.append((kind, value))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self, offset: int = 0) -> tuple[str, Any] | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self, kind: str, value: Any = None) -> Any:
        tok = self.peek()
        if tok is None or tok[0] != kind or (value is not None and tok[1] != value):
            raise ValueError(f"expected {kind} {value!r}, got {tok!r} at token {self.i}")
        self.i += 1
        return tok[1]

    def value(self) -> Any:
        tok = self.peek()
        if tok == ("punct", "{"):
            return self.table()
        return self.take("value")

    def table(self) -> dict[Any, Any]:
        self.take("punct", "{")
        result: dict[Any, Any] = {}
        index = 1
        while self.peek() != ("punct", "}"):
            tok, nxt = self.peek(), self.peek(1)
            if tok == ("punct", "["):
                self.i += 1
                key = self.value()
                self.take("punct", "]")
                self.take("punct", "=")
                result[key] = self.value()
            elif tok is not None and tok[0] == "name" and nxt == ("punct", "="):
                self.i += 2
                result[tok[1]] = self.value()
            else:
                result[index] = self.value()
                index += 1
            if self.peek() in (("punct", ","), ("punct", ";")):
                self.i += 1
        self.take("punct", "}")
        return result


def parse_assignments(text: str) -> dict[str, Any]:
    """Parse a file made of top-level `name = value` assignments."""
    parser = _Parser(_tokenize(text))
    result: dict[str, Any] = {}
    while parser.peek() is not None:
        name = parser.take("name")
        parser.take("punct", "=")
        result[name] = parser.value()
    return result


def as_list(table: dict[Any, Any] | None) -> list[Any]:
    """Positional entries of a Lua table, in order."""
    if not table:
        return []
    return [table[k] for k in sorted(k for k in table if isinstance(k, int))]
