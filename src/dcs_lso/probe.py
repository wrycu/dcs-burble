"""Proving stage P1 (and P6): check that we can read Tacview's real-time stream from DCS.

Reports the host handshake, throughput, per-aircraft sample rates, and how far
behind real time the data arrives.

The DCS plugin stamps `RecordingTime` with the wall-clock time the connection's
recording started, and the first frame carries the sim time at that moment. Delay
is therefore estimated as `wall clock - (RecordingTime + (frame time - first frame time))`.
That assumes the sim runs at 1x without pauses; the jitter (spread above the
minimum) does not depend on it.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .acmi import AcmiParser, Frame, GlobalProperty, ObjectUpdate
from .acmi.stream import TelemetryClient

# More than this behind real time suggests the multiplayer playback delay is active (P6).
PLAYBACK_DELAY_SUSPECTED_S = 30.0


@dataclass
class ProbeStats:
    connected_at: float = 0.0
    first_data_at: float | None = None
    lines: int = 0
    bytes: int = 0
    frames: int = 0
    updates: int = 0
    recording_time: float | None = None
    first_frame_time: float | None = None
    last_frame_time: float | None = None
    offsets: list[float] = field(default_factory=list)
    samples: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    labels: dict[int, str] = field(default_factory=dict)
    aircraft: set[int] = field(default_factory=set)

    def observe(self, line: str, now: float, parser: AcmiParser) -> None:
        if self.first_data_at is None:
            self.first_data_at = now
        self.lines += 1
        self.bytes += len(line.encode("utf-8"))
        for record in parser.feed(line):
            if isinstance(record, Frame):
                self.frames += 1
                self.first_frame_time = record.time if self.first_frame_time is None else self.first_frame_time
                self.last_frame_time = record.time
                if self.recording_time is not None:
                    self.offsets.append(now - (self.recording_time + record.time - self.first_frame_time))
            elif isinstance(record, GlobalProperty) and record.key == "RecordingTime":
                self.recording_time = datetime.fromisoformat(record.value).timestamp()
            elif isinstance(record, ObjectUpdate):
                self.updates += 1
                tags = record.props.get("Type", "")
                if "FixedWing" in tags or "Rotorcraft" in tags or "AircraftCarrier" in tags:
                    self.aircraft.add(record.id)
                    self.labels[record.id] = (f"{record.props.get('Pilot', '?')} "
                                              f"({record.props.get('Name', '?')})")
                    if record.moved:
                        self.samples[record.id].append(record.time)

    def report(self, now: float) -> str:
        elapsed = now - self.connected_at
        out = [
            f"elapsed {elapsed:6.1f}s  lines {self.lines}  {self.bytes / max(elapsed, 1e-9) / 1024:.1f} KiB/s  "
            f"frames {self.frames}  updates {self.updates}",
        ]
        if self.first_data_at is not None:
            out.append(f"  first data {1000 * (self.first_data_at - self.connected_at):.0f} ms after handshake")
        if self.first_frame_time is not None and self.last_frame_time is not None:
            out.append(f"  sim time {self.first_frame_time:.2f}s .. {self.last_frame_time:.2f}s")
        if self.offsets:
            ordered = sorted(self.offsets)
            floor = ordered[0]
            jitter = [o - floor for o in ordered]
            p = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
            out.append(f"  delay behind real time: p50 {p(ordered, .5) * 1000:.0f} ms  p95 {p(ordered, .95) * 1000:.0f} ms"
                       f"  (assumes 1x sim without pauses)")
            out.append(f"  jitter above minimum: p50 {p(jitter, .5) * 1000:.0f} ms  p95 {p(jitter, .95) * 1000:.0f} ms"
                       f"  max {jitter[-1] * 1000:.0f} ms")
            if p(ordered, .5) > PLAYBACK_DELAY_SUSPECTED_S:
                out.append(f"  WARNING: data is {p(ordered, .5):.0f}s behind real time; "
                           "the multiplayer playback delay (tacviewPlaybackDelay) may be active")
        for oid in sorted(self.aircraft, key=lambda i: self.labels.get(i, "")):
            times = self.samples.get(oid, [])
            if len(times) >= 3:
                dt = statistics.median(b - a for a, b in zip(times, times[1:]))
                out.append(f"  {self.labels[oid]:<45} {len(times):6d} samples  median dt {dt:.3f}s (~{1 / dt:.1f} Hz)"
                           if dt > 0 else f"  {self.labels[oid]:<45} {len(times):6d} samples")
        return "\n".join(out)


async def probe(host: str, port: int, password: str | None, duration: float | None,
                save: Path | None, report_every: float = 10.0) -> ProbeStats:
    client = TelemetryClient(host, port, password=password)
    t0 = time.time()
    info = await client.connect()
    stats = ProbeStats(connected_at=time.time())
    print(f"connected to {host}:{port} in {1000 * (stats.connected_at - t0):.0f} ms; host name: {info.name!r}")
    parser = AcmiParser()
    out: TextIO | None = save.open("w", encoding="utf-8", newline="") if save else None
    next_report = stats.connected_at + report_every
    try:
        async with asyncio.timeout(duration):
            async for line in client.lines():
                now = time.time()
                if out:
                    out.write(line)
                stats.observe(line, now, parser)
                if now >= next_report:
                    print(stats.report(now), flush=True)
                    next_report = now + report_every
        print("host closed the connection")
    except TimeoutError:
        pass
    finally:
        # Also runs when Ctrl-C cancels the task, so the final report is never lost.
        if out:
            out.close()
        print("--- final ---")
        print(stats.report(time.time()))
        if save:
            print(f"raw stream saved to {save} (analyze with: dcs-lso analyze {save})")
        await client.close()
    return stats
