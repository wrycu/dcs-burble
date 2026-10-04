"""Events written to dcs.log by `hooks/dcs-lso-server-hook.lua`, and a tail-follower for dcs.log."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MARKER = "DCSLSO "


@dataclass(frozen=True, slots=True)
class HookEvent:
    event: str
    time: float | None
    comment: str | None
    initiator: dict | None
    place: dict | None
    # Wall-clock timestamp of the dcs.log line, when present (DCS logs in UTC).
    logged_at: datetime | None
    raw: dict


def parse_hook_line(line: str) -> HookEvent | None:
    """Parse one dcs.log line; None if it isn't a dcs-lso hook event."""
    # Mission-side events log as "SCRIPTING (Main): DCSLSO {...}"; the hook's own as
    # "DCSLSO (Main): DCSLSO {...}": the event is the JSON after the marker that precedes a "{".
    idx = line.find(MARKER + "{")
    if idx < 0:
        return None
    try:
        data = json.loads(line[idx + len(MARKER):])
    except json.JSONDecodeError:
        return None
    logged_at = None
    try:
        logged_at = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        pass
    t = data.get("t")
    return HookEvent(
        event=data.get("event", ""),
        time=float(t) if isinstance(t, (int, float)) else None,
        comment=data.get("comment"),
        initiator=data.get("initiator"),
        place=data.get("place"),
        logged_at=logged_at,
        raw=data,
    )


def follow(path: str | Path, from_start: bool = False, poll: float = 0.05) -> Iterator[str]:
    """Yield lines appended to a file, like `tail -F` (survives truncation and replacement)."""
    path = Path(path)
    fh = None
    inode = None
    buffer = ""
    while True:
        if fh is None:
            try:
                fh = path.open(encoding="utf-8", errors="replace", newline="")
            except FileNotFoundError:
                time.sleep(poll)
                continue
            inode = path.stat().st_ino
            if not from_start:
                fh.seek(0, 2)
            from_start = True  # a replaced file is read from its beginning
        chunk = fh.read()
        if chunk:
            buffer += chunk
            *lines, buffer = buffer.split("\n")
            yield from lines
            continue
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        if st is None or st.st_ino != inode or st.st_size < fh.tell():
            fh.close()
            fh = None
            buffer = ""
            continue
        time.sleep(poll)
