"""Command line entry point: `dcs-lso hub`, `dcs-lso agent`, `dcs-lso analyze <recording.acmi>`, ..."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
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
        wire = p.wire_label or "-"
        pilot = p.pilot or f"id {p.aircraft_id:x}"
        grade = p.dcs_grade.raw if p.dcs_grade else "no DCS grade"
        print(f"{p.start_time:8.2f}s  {pilot:<24} {p.aircraft_type:<14} {p.carrier_type:<8} "
              f"{p.outcome.value:<10} wire {wire:<9} {grade}  ({len(p.samples)} samples)")
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
            if "wires" in event.raw:
                w = event.raw.get("wires") or {}
                detail = (f"  +{event.raw.get('delay', 0)}s after {event.raw.get('source')}: "
                          + " ".join(f"{k}={w.get(k)}" for k in ("w1", "w2", "w3", "w4")))
                place = {"name": event.raw.get("carrier")}
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


def _callouts(args: argparse.Namespace) -> int:
    from .callouts.sim import replay

    recording = load_recording(args.recording)
    passes = list(find_passes(recording))
    if not passes:
        print("no carrier passes found")
    for p in passes:
        r = replay(recording, p, hz=args.hz, strip_aoa=args.derived_aoa)
        aoa = "derived" if args.derived_aoa or p.samples[0].aoa_derived else "recorded"
        print(f"{p.start_time:8.2f}s  {p.pilot or hex(p.aircraft_id)}  {p.outcome.value}  "
              f"({r.rate_hz:.1f} Hz, {aoa} AOA)")
        for c in r.calls:
            st = c.state
            aoa_txt = f"{st.aoa:4.1f}" if st.aoa is not None else "   -"
            print(f"    {c.time:8.2f}s {c.along / 1852:5.2f} nm  {c.call.value:<17} "
                  f"gs {st.glideslope_deg:+5.2f}deg  lineup {st.lineup_deg:+5.2f}deg  aoa {aoa_txt}")
        if not r.calls:
            print("    (no calls)")
    return 0


def _slice(args: argparse.Namespace) -> int:
    from .slices import write_pass_slice

    recording = load_recording(args.recording)
    passes = list(find_passes(recording))
    if args.debrief:
        attach_dcs_grades(passes, recording, load_debrief(args.debrief))
    if not passes:
        print("no carrier passes found")
    for p in passes:
        acmi, meta = write_pass_slice(args.recording, recording, p, args.out_dir)
        print(f"{p.start_time:8.2f}s  {p.outcome.value:<8} -> {acmi} ({acmi.stat().st_size / 1024:.0f} KiB) + {meta.name}")
    return 0


def _grade(args: argparse.Namespace) -> int:
    from .grading import grade_pass

    recording = load_recording(args.recording)
    passes = list(find_passes(recording))
    if args.debrief:
        attach_dcs_grades(passes, recording, load_debrief(args.debrief))
    results = [(p, grade_pass(p)) for p in passes]
    if args.json:
        json.dump([{"pass": {k: v for k, v in p.to_dict().items() if k != "samples"}, "grade": g.to_dict()}
                   for p, g in results], sys.stdout, indent=2, default=str)
        print()
        return 0
    if not results:
        print("no carrier passes found")
    for p, g in results:
        wire = f" wire {p.wire_label}" if p.wire_label else ""
        print(f"{p.start_time:8.2f}s  {p.pilot or hex(p.aircraft_id):<16} {g.text}  [{g.points:g} pts, v{g.version}]{wire}")
        if p.dcs_grade:
            print(f"{'':10}{'DCS LSO:':<17}{p.dcs_grade.raw}")
        if args.verbose:
            for st in g.positions:
                if st.samples:
                    aoa = f"{st.aoa:4.1f}" if st.aoa is not None else "  - "
                    print(f"{'':12}{st.position.value:<3} n={st.samples:<3} glideslope {st.glideslope_deg:+5.2f}deg  "
                          f"lineup {st.lineup_deg:+5.2f}deg ({st.lateral_m:+5.1f} m)  aoa {aoa}")
    return 0


def _sibling_debrief(recording: str) -> Path | None:
    """`<name>.debrief.log` next to `<name>.zip.acmi` / `.txt.acmi` / `.acmi`, if present."""
    path = Path(recording)
    stem = path.name
    for suffix in (".zip.acmi", ".txt.acmi", ".acmi"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    candidate = path.with_name(f"{stem}.debrief.log")
    return candidate if candidate.is_file() else None


def _cards(args: argparse.Namespace) -> int:
    from .cards import CardEntry, render_card, render_index
    from .grading import GRADING_VERSION, grade_pass
    from .slices import slice_name
    from .sun import is_night

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in args.recordings:
        recording = load_recording(path)
        passes = list(find_passes(recording))
        debrief = args.debrief or _sibling_debrief(path)
        if debrief:
            attach_dcs_grades(passes, recording, load_debrief(debrief))
        for p in passes:
            grade = grade_pass(p)
            stem = slice_name(recording, p)
            svg = render_card(p, grade, recording.globals.get("Title", ""), uid=f"c{len(entries)}",
                              night=bool(is_night(recording, p.carrier_id, p.end_time)))
            (out_dir / f"{stem}.svg").write_text(svg, encoding="utf-8")
            name = f"{stem}.svg"
            entries.append(CardEntry(name, svg, p.pilot or hex(p.aircraft_id), grade.grade.value, grade.text,
                                     grade.points, p.outcome.value, Path(path).name, p.start_time,
                                     p.dcs_grade.raw if p.dcs_grade else None))
            print(f"{p.start_time:8.2f}s  {grade.text:<40} -> {out_dir / name}")
    index = out_dir / "index.html"
    index.write_text(render_index(entries, GRADING_VERSION), encoding="utf-8")
    print(f"{len(entries)} cards; open {index}")
    return 0


def _hub(args: argparse.Namespace):
    from .hub.service import Hub

    data_dir = Path(args.data_dir)
    if not args.database_url and not (data_dir / "lso.db").exists() and not args.create:
        # A typo'd or missing --data-dir would otherwise quietly start a new, empty hub.
        raise SystemExit(f"error: no hub in {data_dir.resolve()} (no lso.db there). Point --data-dir at your hub's "
                         "data, or add --create to start a new hub in that folder.")
    url = args.database_url or f"sqlite:///{(data_dir / 'lso.db').resolve()}"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Hub(url, data_dir, require_upload_token=args.require_upload_token, pilot_hook_accept=args.pilot_hook_accept,
               other_servers=args.other_servers)


def _hub_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .hub.app import create_app

    hub = _hub(args)
    if args.discord_board_webhook or args.discord_traps_webhook:
        from .hub.discord import Discord
        Discord(hub, args.discord_board_webhook, args.discord_traps_webhook, args.public_url or "")
    uvicorn.run(create_app(hub), host=args.host, port=args.port)
    return 0


def _hub_add_agent(args: argparse.Namespace) -> int:
    token = _hub(args).add_source(args.name)
    print(f"server agent {args.name!r} added. Its server agent token (shown once, keep it secret):")
    print(token)
    return 0


def _hub_add_pilot_token(args: argparse.Namespace) -> int:
    token = _hub(args).add_pilot_token(args.pilot, args.label)
    print(f"pilot token for {args.pilot!r} created. Everything uploaded with it is credited to them "
          "(shown once, keep it secret):")
    print(token)
    return 0


def _hub_remove_pilot(args: argparse.Namespace) -> int:
    try:
        _hub(args).remove_pilot(args.pilot)
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"pilot {args.pilot!r} removed (their pilot tokens are revoked)")
    return 0


def _hub_remove_alias(args: argparse.Namespace) -> int:
    try:
        moved = _hub(args).remove_alias(args.alias)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"alias {args.alias!r} removed; {moved} passes reported under it moved back to a pilot of that name")
    return 0


def _hub_set_config(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _hub(args).set_config(args.name, config)
    print(f"configuration for {args.name!r} updated; its agent applies it when the next mission starts")
    return 0


def _voice_build(args: argparse.Namespace) -> int:
    from .callouts.voice import ClipLibrary, build_clips

    out = build_clips(args.model, args.out_dir, speed=args.speed)
    lib = ClipLibrary.load(out)
    for call, clips in lib.clips.items():
        for clip in clips:
            print(f"  {call.value:<17} {clip.seconds:4.2f}s  {clip.text!r}")
    print(f"clips written to {out}")
    return 0


def _hub_reset_password(args: argparse.Namespace) -> int:
    try:
        _hub(args).reset_pilot_password(args.pilot)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"password for {args.pilot!r} cleared; they (or anyone) can set a new one on their settings page")
    return 0


def _hub_regrade(args: argparse.Namespace) -> int:
    done, skipped = _hub(args).regrade(force=args.force)
    print(f"regraded {done} passes; {skipped} already had the current grading version")
    return 0


def _hub_wire_check(args: argparse.Namespace) -> int:
    import statistics

    from .detect.wire import SERVER_MAX_ERROR_M, SERVER_OVERSHOOT_M
    rows = _hub(args).wire_check(args.days)
    if not rows:
        print("no traps with a server agent's copy of the jet")
        return 0
    print(f"{'landing':>7} {'report':>6}  {'when':16}  {'pilot':14} {'known':>5}  {'from':9}  {'overshoot':>9}  "
          f"{'stop':>4} {'error':>6}  {'hook':>4}  {'says':>4}")
    for r in rows:
        g = r.signals
        when = f"{r.occurred_at:%Y-%m-%d %H:%M}" if r.occurred_at else "?"
        says = g.wire or "-"
        verdict = "" if not r.known or g.wire is None else ("  right" if g.wire == r.known else "  WRONG")
        print(f"{r.landing_id:>7} {r.report_id:>6}  {when:16}  {r.pilot[:14]:14} {r.known or '?':>5}  "
              f"{r.known_from or '':9}  {'' if r.overshoot_m is None else f'{r.overshoot_m:+.1f} m':>9}  "
              f"{g.stop_wire:>4} {g.stop_error_m:+5.1f}m  {g.hook_wire or '-':>4}  {says:>4}{verdict}")
    known = [r for r in rows if r.known]
    print(f"\n{len(rows)} traps, the wire known on {len(known)} ({sum(r.known_from == 'DCS' for r in known)} from DCS).")
    if known:
        over = [r.overshoot_m for r in known]
        spread = f", sd {statistics.stdev(over):.1f}" if len(over) > 1 else ""
        print(f"Overshoot: mean {statistics.mean(over):.1f} m{spread}, {min(over):.1f} to {max(over):.1f} "
              f"(the correction used: {SERVER_OVERSHOOT_M} m).")
        named = [r for r in known if r.signals.wire is not None]
        print(f"Named (both signals agree, within {SERVER_MAX_ERROR_M} m): {len(named)} of {len(known)}, "
              f"{sum(r.signals.wire == r.known for r in named)} right. Stop signal alone right: "
              f"{sum(r.signals.stop_wire == r.known for r in known)}; hook signal alone right: "
              f"{sum(r.signals.hook_wire == r.known for r in known)}.")
    return 0


def _upload(args: argparse.Namespace) -> int:
    import httpx

    from .slices import slice_recording

    token = args.token or os.environ.get("DCS_LSO_TOKEN")
    if not token:
        print("error: no token (use --token or DCS_LSO_TOKEN)", file=sys.stderr)
        return 2
    failures = 0
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as client, \
            tempfile.TemporaryDirectory() as tmp:
        for path in args.recordings:
            debrief = args.debrief or _sibling_debrief(path)
            # Every pass, plus own-jet approaches without a carrier (merged on the hub with the
            # server's report of each landing).
            for acmi, meta in slice_recording(path, Path(tmp) / Path(path).name, load_debrief(debrief) if debrief else None):
                info = meta["pass"]
                label = f"{info['start_time']:8.2f}s  {info.get('pilot') or hex(info['aircraft_id']):<16}"
                try:
                    r = client.post("/api/v1/passes", files={"slice": (acmi.name, acmi.read_bytes(), "application/zip")},
                                    data={"sidecar": json.dumps(meta)})
                except httpx.HTTPError as exc:
                    print(f"{label} upload failed: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                if r.status_code in (200, 201):
                    b = r.json()
                    state = "new" if b["created"] else "already uploaded"
                    text = b["text"] or "own track: waiting for a report of this landing with the carrier"
                    print(f"{label} {text:<40} ({state}) {args.url.rstrip('/')}{b['url']}")
                else:
                    print(f"{label} rejected ({r.status_code}): {r.text}", file=sys.stderr)
                    failures += 1
    return 1 if failures else 0


def _agent(args: argparse.Namespace) -> int:
    import logging

    from .agent.service import Agent, AgentConfig

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is just noise
    dcs_log = Path(args.dcs_log) if args.dcs_log else None
    debrief = Path(args.debrief) if args.debrief else (dcs_log.with_name("debrief.log") if dcs_log else None)
    config = AgentConfig(
        work_dir=Path(args.work_dir), tacview_host=args.tacview_host, tacview_port=args.tacview_port,
        tacview_password=args.tacview_password, dcs_log=dcs_log, debrief=debrief,
        url=args.url, token=args.token or os.environ.get("DCS_LSO_TOKEN"),
        mode=args.mode, voice_dir=Path(args.voice_dir) if args.voice_dir else None,
        keep_archives_days=args.keep_archives_days, keep_sent_days=args.keep_sent_days,
        keep_rejected_days=args.keep_rejected_days,
    )

    async def run() -> None:
        await Agent(config).run_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


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

    hook = sub.add_parser("hook-listen", help="P4: print events from the dcs-lso server hook as they reach dcs.log")
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

    callouts = sub.add_parser("callouts", help="replay a recording through the live callout engine")
    callouts.add_argument("recording")
    callouts.add_argument("--hz", type=float, help="thin samples to this rate (mimic remote aircraft)")
    callouts.add_argument("--derived-aoa", action="store_true", help="ignore recorded AOA and derive it")
    callouts.set_defaults(func=_callouts)

    slice_cmd = sub.add_parser("slice", help="write a standalone ACMI slice (+ JSON sidecar) for every pass")
    slice_cmd.add_argument("recording")
    slice_cmd.add_argument("--out-dir", "-o", default="slices")
    slice_cmd.add_argument("--debrief", metavar="PATH", help="DCS debrief.log, to record DCS's grade and wire")
    slice_cmd.set_defaults(func=_slice)

    grade = sub.add_parser("grade", help="grade every carrier pass in a recording (grading v1)")
    grade.add_argument("recording")
    grade.add_argument("--debrief", metavar="PATH", help="DCS debrief.log, to show DCS's own grade and wire")
    grade.add_argument("--json", action="store_true")
    grade.add_argument("-v", "--verbose", action="store_true", help="show per-position deviations")
    grade.set_defaults(func=_grade)

    cards = sub.add_parser("cards", help="write SVG trap cards and an index.html for every pass")
    cards.add_argument("recordings", nargs="+")
    cards.add_argument("--out-dir", "-o", default="cards")
    cards.add_argument("--debrief", metavar="PATH",
                       help="DCS debrief.log for all recordings (default: <name>.debrief.log next to each one)")
    cards.set_defaults(func=_cards)

    hub = sub.add_parser("hub", help="run or manage the hub (website, database, grading)")
    hub.add_argument("--data-dir", default=os.environ.get("DCS_LSO_DATA_DIR", "data"),
                     help="where slices (and the default SQLite database) live [$DCS_LSO_DATA_DIR]")
    hub.add_argument("--create", action="store_true",
                     help="start a new hub if the data folder has none yet (otherwise an empty folder is an error)")
    hub.add_argument("--database-url", default=os.environ.get("DCS_LSO_DATABASE_URL"),
                     help="SQLAlchemy URL; default sqlite in the data dir [$DCS_LSO_DATABASE_URL]")
    hub.add_argument("--require-upload-token", action="store_true",
                     default=os.environ.get("DCS_LSO_REQUIRE_UPLOAD_TOKEN", "").lower() in ("1", "true", "yes"),
                     help="only accept recordings uploaded with a token (by default anyone can upload "
                          "a recording and import their own passes from it) [$DCS_LSO_REQUIRE_UPLOAD_TOKEN]")
    hub.add_argument("--pilot-hook-accept", choices=["ours", "any"],
                     default=os.environ.get("DCS_LSO_PILOT_HOOK_ACCEPT", "ours"),
                     help="pilot hook traps accepted: flown on this hub's own servers only (ours, the default), or "
                          "from any server with a pilot token (any) [$DCS_LSO_PILOT_HOOK_ACCEPT]")
    hub.add_argument("--other-servers", choices=["shown", "hidden"],
                     default=os.environ.get("DCS_LSO_OTHER_SERVERS", "shown"),
                     help="landings pilots flew on other servers (pilot hooks): on the greenie board by default "
                          "(shown, the default), or only when a viewer picks \"All servers\" (hidden; also left off "
                          "Discord) [$DCS_LSO_OTHER_SERVERS]")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    serve = hub_sub.add_parser("serve", help="serve the API, greenie board and pass pages")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--public-url", default=os.environ.get("DCS_LSO_PUBLIC_URL"),
                       help="the hub's address as people reach it (e.g. https://lso.example.com), for links in "
                            "Discord messages [$DCS_LSO_PUBLIC_URL]")
    serve.add_argument("--discord-board-webhook", default=os.environ.get("DCS_LSO_DISCORD_BOARD_WEBHOOK"),
                       help="Discord webhook for the greenie board: one message, kept up to date by editing it "
                            "[$DCS_LSO_DISCORD_BOARD_WEBHOOK]")
    serve.add_argument("--discord-traps-webhook", default=os.environ.get("DCS_LSO_DISCORD_TRAPS_WEBHOOK"),
                       help="Discord webhook for a post per landing, with its trap card [$DCS_LSO_DISCORD_TRAPS_WEBHOOK]")
    serve.set_defaults(func=_hub_serve)
    add_agent = hub_sub.add_parser("add-agent", help="add a server agent and print its server agent token")
    add_agent.add_argument("name")
    add_agent.set_defaults(func=_hub_add_agent)
    add_pilot = hub_sub.add_parser("add-pilot-token", help="create a pilot token for a pilot (pilots can also "
                                                           "create their own on their settings page)")
    add_pilot.add_argument("pilot", help="the pilot's name, as on the board (added if new)")
    add_pilot.add_argument("--label", default="", help='what it\'s for, e.g. "Wrycu\'s PC"')
    add_pilot.set_defaults(func=_hub_add_pilot_token)
    remove_pilot = hub_sub.add_parser("remove-pilot", help="remove a pilot who has no passes (e.g. a junk sign-up)")
    remove_pilot.add_argument("pilot", help="the pilot's name, as on the board")
    remove_pilot.set_defaults(func=_hub_remove_pilot)
    remove_alias = hub_sub.add_parser("remove-alias", help="undo a pilot's alias (e.g. a name claimed by mistake)")
    remove_alias.add_argument("alias")
    remove_alias.set_defaults(func=_hub_remove_alias)
    regrade = hub_sub.add_parser("regrade", help="grade every stored pass with the current grading version")
    regrade.add_argument("--force", action="store_true", help="also redo passes already at the current version")
    regrade.set_defaults(func=_hub_regrade)
    wire_check = hub_sub.add_parser("wire-check", help="measure the server-track wire signals on traps with a known "
                                                       "wire (for naming the wire in the live welcome)")
    wire_check.add_argument("--days", type=int, default=0, help="only the last N days (default: all)")
    wire_check.set_defaults(func=_hub_wire_check)
    reset = hub_sub.add_parser("reset-password", help="clear a pilot's upload password (e.g. they forgot it)")
    reset.add_argument("pilot", help="the pilot's name, as on the board")
    reset.set_defaults(func=_hub_reset_password)
    set_config = hub_sub.add_parser("set-config", help="set a server agent's configuration (JSON file)")
    set_config.add_argument("name")
    set_config.add_argument("file")
    set_config.set_defaults(func=_hub_set_config)

    voice = sub.add_parser("voice", help="LSO voice clips")
    voice_sub = voice.add_subparsers(dest="voice_command", required=True)
    build = voice_sub.add_parser("build", help="render the LSO phrases with a Piper voice model")
    build.add_argument("model", help="Piper .onnx voice (e.g. from `python -m piper.download_voices en_US-ryan-high`)")
    build.add_argument("--out-dir", "-o", default="voice")
    build.add_argument("--speed", type=float, default=1.15, help="speaking speed (1 = Piper's normal pace)")
    build.set_defaults(func=_voice_build)

    upload = sub.add_parser("upload", help="slice recordings and upload every pass to the hub")
    upload.add_argument("recordings", nargs="+")
    upload.add_argument("--url", default=os.environ.get("DCS_LSO_URL", "http://127.0.0.1:8000"),
                        help="hub URL [$DCS_LSO_URL]")
    upload.add_argument("--token", help="upload token [$DCS_LSO_TOKEN]")
    upload.add_argument("--debrief", metavar="PATH",
                        help="DCS debrief.log for all recordings (default: <name>.debrief.log next to each one)")
    upload.set_defaults(func=_upload)

    from .acmi.stream import DEFAULT_PORT as TACVIEW_PORT

    def agent_args(p: argparse.ArgumentParser, mode: str) -> None:
        p.add_argument("--work-dir", default="agent", help="session archive and upload outbox")
        p.add_argument("--tacview-host", default="127.0.0.1")
        p.add_argument("--tacview-port", type=int, default=TACVIEW_PORT)
        p.add_argument("--tacview-password")
        p.add_argument("--dcs-log", metavar="PATH", help="DCS Logs/dcs.log, for the dcs-lso server hook's events")
        p.add_argument("--debrief", metavar="PATH", help="DCS Logs/debrief.log (default: next to --dcs-log)")
        p.add_argument("--url", default=os.environ.get("DCS_LSO_URL"), help="hub URL [$DCS_LSO_URL]")
        p.add_argument("--token", help="server agent or pilot token [$DCS_LSO_TOKEN]")
        p.set_defaults(mode=mode)
        p.add_argument("--voice-dir", metavar="DIR", help="LSO voice clips (from `dcs-lso voice build`), or a folder of "
                       "clip sets: the hub's config (callouts.voice) picks one by folder name")
        for name, default, what in (("archives", 90, "session archives (raw recordings, for re-slicing)"),
                                    ("sent", 14, "local copies of uploaded slices (the hub keeps its own)"),
                                    ("rejected", 30, "slices the hub rejected")):
            env = f"DCS_LSO_KEEP_{name.upper()}_DAYS"
            p.add_argument(f"--keep-{name}-days", type=float,
                           default=float(os.environ[env]) if os.environ.get(env) else None, metavar="DAYS",
                           help=f"keep {what} this long; 0 = forever. Default: the hub's \"retention\" setting "
                                f"for this agent, else {default} [${env}]")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(func=_agent)

    agent_args(sub.add_parser("agent", help="server agent: live Tacview stream -> passes, LSO calls -> hub"), "server")
    agent_args(sub.add_parser("pilot-uploader", help="pilot uploader: your own jet's passes from your PC -> hub "
                                                     "(never transmits)"), "pilot")
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
