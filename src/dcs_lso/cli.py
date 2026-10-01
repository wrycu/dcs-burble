"""Command line entry point: `dcs-lso analyze <recording.acmi>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .acmi import load_recording
from .dcslog import attach_dcs_grades, load_debrief
from .detect import find_passes


def _analyze(args: argparse.Namespace) -> int:
    recording = load_recording(args.recording)
    passes = list(find_passes(recording))
    if args.debrief:
        attach_dcs_grades(passes, recording, load_debrief(args.debrief))
    if args.json:
        json.dump([p.to_dict() for p in passes], sys.stdout, indent=2)
        print()
        return 0
    if not passes:
        print("no carrier passes found")
        return 0
    for p in passes:
        wire = f"#{p.wire}" if p.wire else "-"
        pilot = p.pilot or f"id {p.aircraft_id:x}"
        grade = p.dcs_grade.raw if p.dcs_grade else "no DCS grade"
        print(f"{p.start_time:8.2f}s  {pilot:<24} {p.aircraft_type:<14} {p.carrier_type:<8} "
              f"{p.outcome.value:<10} wire {wire:<3} {grade}  ({len(p.samples)} samples)")
    return 0


def _probe(args: argparse.Namespace) -> int:
    from .probe import probe

    try:
        asyncio.run(probe(args.host, args.port, args.password, args.duration,
                          Path(args.save) if args.save else None, args.report_every))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _replay(args: argparse.Namespace) -> int:
    from .acmi.stream import serve_recording

    async def run() -> None:
        server = await serve_recording(args.recording, args.host, args.port,
                                       password=args.password, speed=args.speed)
        print(f"serving {args.recording} on {args.host}:{args.port} (speed {args.speed}x); Ctrl-C to stop")
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


DEFAULT_DCS_LOG = Path.home() / "Saved Games" / "DCS" / "Logs" / "dcs.log"


def _hook_listen(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from .dcslog import LsoGrade, follow, parse_hook_line

    path = Path(args.log)
    print(f"following {path} for dcs-lso hook events; Ctrl-C to stop", flush=True)
    try:
        for line in follow(path, from_start=args.from_start):
            if "DCSLSO" in line and "handler injection" in line:
                print(f"[hook] {line.strip()}", flush=True)
                continue
            event = parse_hook_line(line)
            if event is None:
                continue
            now = datetime.now(UTC)
            lag = f"{(now - event.logged_at).total_seconds() * 1000:6.0f} ms" if event.logged_at else "     ?"
            who = event.initiator or {}
            place = event.place or {}
            t = f"{event.time:9.3f}" if event.time is not None else "        -"
            detail = ""
            if event.comment:
                grade = LsoGrade.parse(event.comment)
                detail = f"  grade={grade.grade} wire={grade.wire} remarks={grade.remarks!r}"
            print(f"{now.astimezone():%H:%M:%S.%f}"[:-3] + f"  log->us {lag}  t={t}  {event.event:<21} "
                  f"{who.get('player') or who.get('name') or '-'} ({who.get('type', '-')}, id {who.get('object_id', '-')})"
                  f" @ {place.get('name', '-')}{detail}", flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def _srs_test(args: argparse.Namespace) -> int:
    from .srs.packet import Modulation
    from .srs.tools import latency_test

    try:
        return asyncio.run(latency_test(args.host, args.port, args.freq, Modulation[args.modulation],
                                        args.seconds, args.rounds))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _srs_say(args: argparse.Namespace) -> int:
    from .srs.packet import Modulation
    from .srs.tools import build_audio, say

    audio = build_audio(args.tone, args.wav, args.text)
    try:
        return asyncio.run(say(args.host, args.port, args.freq, Modulation[args.modulation], args.coalition,
                               args.name, audio, args.interactive))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dcs-lso")
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze", help="find and classify carrier passes in a Tacview recording")
    analyze.add_argument("recording", help="path to a .acmi / .zip.acmi file")
    analyze.add_argument("--json", action="store_true", help="emit full pass data as JSON")
    analyze.add_argument("--debrief", metavar="PATH",
                         help="DCS Logs/debrief.log from the same session, for LSO grades and wires")
    analyze.set_defaults(func=_analyze)

    from .acmi.stream import DEFAULT_PORT

    probe = sub.add_parser("probe", help="P1: connect to Tacview real-time telemetry and report stream stats")
    probe.add_argument("--host", default="127.0.0.1")
    probe.add_argument("--port", type=int, default=DEFAULT_PORT)
    probe.add_argument("--password", help="tacviewRealTimeTelemetryPassword, if set")
    probe.add_argument("--duration", type=float, help="seconds to run (default: until Ctrl-C or host closes)")
    probe.add_argument("--save", metavar="PATH", help="write the raw stream to this .txt.acmi file")
    probe.add_argument("--report-every", type=float, default=10.0, metavar="SECONDS")
    probe.set_defaults(func=_probe)

    replay = sub.add_parser("replay", help="serve a recording as a fake Tacview real-time host (for testing)")
    replay.add_argument("recording")
    replay.add_argument("--host", default="127.0.0.1")
    replay.add_argument("--port", type=int, default=DEFAULT_PORT)
    replay.add_argument("--password")
    replay.add_argument("--speed", type=float, default=1.0, help="playback speed; 0 = as fast as possible")
    replay.set_defaults(func=_replay)

    hook = sub.add_parser("hook-listen", help="P4: print events from the dcs-lso hook as they reach dcs.log")
    hook.add_argument("--log", default=str(DEFAULT_DCS_LOG), help="path to dcs.log")
    hook.add_argument("--from-start", action="store_true", help="also show events already in the file")
    hook.set_defaults(func=_hook_listen)

    def srs_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1", help="SRS server address")
        p.add_argument("--port", type=int, default=5002)
        p.add_argument("--freq", type=float, default=251.0, help="frequency in MHz")
        p.add_argument("--modulation", choices=["AM", "FM"], default="AM")

    srs_test = sub.add_parser("srs-test", help="P5: measure relay latency through an SRS server (two local clients)")
    srs_args(srs_test)
    srs_test.add_argument("--seconds", type=float, default=2.0, help="length of each test transmission")
    srs_test.add_argument("--rounds", type=int, default=5)
    srs_test.set_defaults(func=_srs_test)

    srs_say = sub.add_parser("srs-say", help="P5: transmit test audio on an SRS frequency (cockpit check)")
    srs_args(srs_say)
    srs_say.add_argument("--coalition", type=int, default=2, help="0 spectator, 1 red, 2 blue")
    srs_say.add_argument("--name", default="LSO", help="transmitter name shown in SRS")
    what = srs_say.add_mutually_exclusive_group()
    what.add_argument("--text", help="speak this text (espeak-ng test voice)")
    what.add_argument("--wav", help="16-bit PCM WAV file to transmit")
    what.add_argument("--tone", type=float, default=1.0, help="seconds of 1 kHz test tone (default)")
    srs_say.add_argument("--interactive", "-i", action="store_true",
                         help="stay connected and transmit each time Enter is pressed")
    srs_say.set_defaults(func=_srs_say)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
