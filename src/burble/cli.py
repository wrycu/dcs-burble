"""Command line entry point: `burble hub`, `burble agent`, `burble analyze <recording.acmi>`, ..."""

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
from .models import download_model


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


# Kokoro's model files (`burble voice build`).
DEFAULT_DCS_LOG = Path.home() / "Saved Games" / "DCS" / "Logs" / "dcs.log"


def _hook_listen(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from .dcslog import LsoGrade, follow, parse_hook_line

    path = args.dcs_log
    print(f"following {path} for Burble hook events; Ctrl-C to stop", flush=True)
    try:
        for line in follow(path, from_start=args.from_start):
            if "BURBLE" in line and "handler injection" in line:
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


def _srs_listen(args: argparse.Namespace) -> int:
    from .srs.packet import Modulation
    from .srs.tools import listen

    try:
        return asyncio.run(listen(args.host, args.port, args.freq, Modulation[args.modulation], args.coalition,
                                  args.name, args.model, args.save, args.seconds))
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
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
    default = getattr(args, "_given", {}).get("data_dir") == "default"
    if not args.database_url and not (data_dir / "lso.db").exists() and not args.create and not default:
        # A typo'd data folder would otherwise quietly start a new, empty hub.
        raise SystemExit(f"error: no hub in {data_dir.resolve()} (no lso.db there). Point data_dir (in "
                         f"{getattr(args, '_config_path', 'the config')}, or --data-dir) at your hub's data, or add "
                         "--create to start a new hub in that folder.")
    url = args.database_url or f"sqlite:///{(data_dir / 'lso.db').resolve()}"
    if not args.database_url and not (data_dir / "lso.db").exists():
        # Said aloud: a data_dir left out of the config also lands here (the default folder), and an empty hub
        # otherwise looks like the real one that has forgotten everyone's password.
        print(f"starting a NEW, empty hub in {data_dir.resolve()} (to use an existing one, set data_dir under [hub] "
              f"in {getattr(args, '_config_path', 'the config')})", file=sys.stderr)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Hub(url, data_dir)


def _hub_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .hub.app import create_app

    hub = _hub(args)
    print(f"hub data: {args.database_url or (Path(args.data_dir) / 'lso.db').resolve()}", file=sys.stderr)
    if args.voice_dir:
        from .hub.voice import CallVoice
        hub.voice = CallVoice(args.voice_dir, lambda: hub.setting("default_voice"))
    from .hub.discord import Discord
    Discord(hub)  # posts once a webhook is set (the hub settings, on /admin)
    from .hub.telemetry import Telemetry
    Telemetry(hub)  # usage reports, once a day while on (see `burble hub telemetry`)
    # Live views keep their connections open: on a restart, close them after a few seconds rather than wait.
    uvicorn.run(create_app(hub), host=args.host, port=args.port, timeout_graceful_shutdown=5)
    return 0


def _hub_telemetry(args: argparse.Namespace) -> int:
    from .hub.telemetry import NEVER_SENT, WHAT_IS_SENT, Settings, build_report

    hub = _hub(args)
    settings = Settings(hub)
    action = args.telemetry_command
    try:
        if action == "show":
            print(json.dumps(build_report(hub), indent=2))
            return 0
        if action in ("on", "off"):
            settings.set_enabled(action == "on")
        elif action == "urls":
            if args.default or args.urls or args.none:
                settings.set_urls(None if args.default else [] if args.none else args.urls)
        elif action == "view":
            settings.set_view(None if args.clear else args.url, "" if args.clear else args.key)
            print("Usage page disconnected" if args.clear else
                  f"the admins' Usage page now shows {args.url}'s stats")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"usage reports: {'on' if settings.enabled() else 'off'}")
    print("sent to: " + (", ".join(settings.urls()) or "nowhere"))
    print("what's sent (all about the last 30 days):\n" + "\n".join(f"  - {x}" for x in WHAT_IS_SENT))
    print("never sent:\n" + "\n".join(f"  - {x}" for x in NEVER_SENT))
    return 0


def _telemetry_store(args: argparse.Namespace):
    from .telemetry.store import Store
    return Store(args.data_dir)


def _telemetry_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .telemetry.app import create_app

    uvicorn.run(create_app(_telemetry_store(args)), host=args.host, port=args.port)
    return 0


def _telemetry_set_password(args: argparse.Namespace) -> int:
    import getpass

    password = getpass.getpass("New password for the Usage page and API: ")
    if getpass.getpass("Again: ") != password:
        print("error: the passwords differ", file=sys.stderr)
        return 1
    try:
        _telemetry_store(args).set_password(password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("password set")
    return 0


def _telemetry_new_key(args: argparse.Namespace) -> int:
    try:
        key = _telemetry_store(args).new_key(args.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"API key {args.name!r} (shown once, keep it secret; give it to a hub under Admin > Usage reports):")
    print(key)
    return 0


def _telemetry_revoke_key(args: argparse.Namespace) -> int:
    try:
        _telemetry_store(args).revoke_key(args.name)
    except LookupError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1
    print(f"API key {args.name!r} revoked")
    return 0


def _telemetry_keys(args: argparse.Namespace) -> int:
    keys = _telemetry_store(args).keys()
    for k in keys:
        print(f"{k.name}\t(made {k.created_at:%Y-%m-%d})")
    if not keys:
        print("no API keys (make one with `burble telemetry new-key NAME`)")
    return 0


def _hub_add_agent(args: argparse.Namespace) -> int:
    try:
        token = _hub(args).add_source(args.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
    try:
        _hub(args).set_config(args.name, config)
    except ValueError as exc:
        print(f"not saved: {exc}", file=sys.stderr)
        return 1
    print(f"configuration for {args.name!r} updated; its agent applies it when the next mission starts")
    return 0


def _hub_config_reference(_args: argparse.Namespace) -> int:
    from .agentconfig import reference_markdown

    print(reference_markdown(), end="")
    return 0


def _build_progress(sets: int):
    """`voice build`'s progress: each clip set's count of clips as it goes (on a terminal), and each set when done."""
    started: list[str] = []

    def progress(name: str, done: int, total: int) -> None:
        if not started or started[-1] != name:
            started.append(name)
        where = f"  {name} ({len(started)} of {sets}): " if sets > 1 else f"  {name}: "
        if sys.stderr.isatty():
            print(f"\r{where}{done} of {total} clips", end="\n" if done == total else "", file=sys.stderr,
                  flush=True)
        elif done == total:
            print(f"{where}done", file=sys.stderr, flush=True)
    return progress


def _voice_build(args: argparse.Namespace) -> int:
    from .callouts.voice import ClipLibrary, build_clips, build_kokoro_clips

    engine = args.engine
    if args._given["model"] == "flag" and args._given["engine"] != "flag":
        engine = "kokoro" if args.model.name.startswith("kokoro") else "piper"
    needed = [args.model] + ([args.kokoro_voices] if engine == "kokoro" else [])
    for path in [p for p in needed if not p.exists()]:
        try:
            download_model(path, engine)
        except (OSError, ValueError) as exc:
            print(f"error: no {path}, and it couldn't be downloaded: {exc}", file=sys.stderr)
            return 1
    # A build takes every core it can get (Kokoro: ~14 of 16): behind a DCS server and its agent on the same machine.
    os.nice(10)
    if engine == "kokoro":
        count = f"{len(args.speakers)} voices" if len(args.speakers) != 1 else "1 voice"
        print(f"building {count} into {args.out_dir} (this takes a while: about a minute each)", file=sys.stderr)
        try:
            outs = build_kokoro_clips(args.model, args.kokoro_voices, args.out_dir, tuple(args.speakers),
                                      speed=args.speed, progress=_build_progress(len(args.speakers)))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        for out in outs:
            print(f"clips written to {out}")
        return 0
    print(f"building the voice {args.model.stem} (this takes a minute or so)", file=sys.stderr)
    out = build_clips(args.model, args.out_dir / (args.name or args.model.stem), speed=args.speed,
                      progress=_build_progress(1))
    lib = ClipLibrary.load(out)
    for call, clips in lib.clips.items():
        for clip in clips:
            print(f"  {call.value:<17} {clip.seconds:4.2f}s  {clip.text!r}")
    if lib.wind:
        print(f"  {'roger ball + wind':<17} {len(lib.wind)} clips ({min(k for k, _ in lib.wind)}-"
              f"{max(k for k, _ in lib.wind)} knots, plain and axial)" + (", and winds calm" if lib.calm else ""))
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


def _hub_set_admin(args: argparse.Namespace) -> int:
    try:
        _hub(args).set_admin(args.pilot, not args.remove)
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.pilot!r} is no longer an admin" if args.remove else
          f"{args.pilot!r} is now an admin: signed in on the website, they can manage the hub at /admin")
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
        print(f"Named (the corrected stop within {SERVER_MAX_ERROR_M} m of a wire): {len(named)} of {len(known)}, "
              f"{sum(r.signals.wire == r.known for r in named)} right. Stop signal alone right: "
              f"{sum(r.signals.stop_wire == r.known for r in known)}; hook signal alone right: "
              f"{sum(r.signals.hook_wire == r.known for r in known)}.")
    return 0


def _upload(args: argparse.Namespace) -> int:
    import httpx

    from .slices import slice_recording

    token = args.token
    if not token and not args.dry_run:
        print(f"error: no token (token in [upload] in {args._config_path}, or --token)", file=sys.stderr)
        return 2
    hooks = None
    if args.dcs_log:
        from .agent.service import HookFeed
        hooks = HookFeed(Path(args.dcs_log), follow=False)
    failures = 0
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as client, \
            tempfile.TemporaryDirectory() as tmp:
        for path in args.recordings:
            debrief = args.debrief or (None if hooks else _sibling_debrief(path))
            # Every pass, plus own-jet approaches without a carrier (merged on the hub with the
            # server's report of each landing).
            for acmi, meta in slice_recording(path, Path(tmp) / Path(path).name,
                                              load_debrief(debrief) if debrief else None, pilot=args.pilot,
                                              hooks=hooks):
                info = meta["pass"]
                label = f"{info['start_time']:8.2f}s  {info.get('pilot') or hex(info['aircraft_id']):<16}"
                if args.dry_run:
                    dcs = (meta.get("dcs") or {}).get("grade") or {}
                    wire = (meta.get("dcs") or {}).get("wire")
                    print(f"{label} {info.get('aircraft_type') or '':<14} {info.get('outcome') or 'own track':<9} "
                          f"{info.get('occurred_at') or '':<32} {f'wire {wire}' if wire else '':<7} {dcs.get('raw') or ''}")
                    continue
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
    dcs_log = args.dcs_log
    debrief = args.debrief or (dcs_log.with_name("debrief.log") if dcs_log else None)
    log = logging.getLogger("burble")
    log.info("settings from %s", args._config_path)
    voice_dir = getattr(args, "voice_dir", None)
    if voice_dir is not None and not voice_dir.exists():
        log.info("no voice clips in %s (build them with `burble voice build`)", voice_dir)
        voice_dir = None
    listen_model = getattr(args, "listen_model", None)
    if listen_model is not None and not listen_model.exists():
        try:
            download_model(listen_model, "vosk")
        except (OSError, ValueError) as exc:
            log.warning("no listen_model %s, and it couldn't be downloaded (%s): listening stays off", listen_model,
                        exc)
            listen_model = None
    config = AgentConfig(
        work_dir=args.work_dir, tacview_host=args.tacview_host, tacview_port=args.tacview_port,
        tacview_password=args.tacview_password, dcs_log=dcs_log, debrief=debrief, url=args.url, token=args.token,
        mode=args.mode, voice_dir=voice_dir, listen_model=listen_model,
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
    from . import localconfig as lc
    from .callouts.voice import KOKORO_SPEAKERS

    lc.SECTIONS.clear()  # (built afresh with the parser: defaults under $BURBLE_HOME)
    parser = argparse.ArgumentParser(prog="burble")
    parser.add_argument("--config", metavar="PATH", help="the settings file (default: ~/.burble/config.toml, written "
                                                         "with every default on first use)")
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

    hook = sub.add_parser("hook-listen", help="P4: print events from the burble server hook as they reach dcs.log")
    lc.add(hook, "agent", "--dcs-log", "--log", kind="path", default=DEFAULT_DCS_LOG, help="DCS Logs/dcs.log",
           template=False)
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

    srs_listen = sub.add_parser("srs-listen", help="print what's said on an SRS frequency, and the pilot calls "
                                                   "heard (ball, Clara, Paddles)")
    srs_args(srs_listen)
    srs_listen.add_argument("--coalition", type=int, default=2, help="0 spectator, 1 red, 2 blue")
    srs_listen.add_argument("--name", default="LSO (listening)", help="client name shown in SRS")
    srs_listen.add_argument("--model", help="Vosk model folder (e.g. "
                                            "~/.burble/models/vosk-model-small-en-us-0.15); without it, transmissions are only recorded")
    srs_listen.add_argument("--save", metavar="DIR", help="keep each transmission as a WAV here")
    srs_listen.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl-C)")
    srs_listen.set_defaults(func=_srs_listen)

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

    hub = sub.add_parser("hub", help="serve the hub (website, database, grading), or manage it with a command")

    def hub_opt(*flags: str, **kw) -> None:
        lc.add(hub, "hub", *flags, **kw)

    hub_opt("--data-dir", kind="path", default=lc.home() / "hub", help="where the hub's database and slices live")
    hub.add_argument("--create", action="store_true",
                     help="start a new hub in --data-dir (or data_dir in the config) if it has none yet: otherwise a "
                          "folder without one is an error, so a mistyped path never starts an empty hub (the default "
                          "folder is created as needed)")
    hub_opt("--database-url", help="SQLAlchemy URL, for a database other than SQLite in the data folder")
    hub_opt("--host", default="127.0.0.1", help="the address to serve on (publish it through a reverse proxy)")
    hub_opt("--port", kind="int", default=8000, help="the port to serve on")
    hub_opt("--voice-dir", kind="path", default=lc.home() / "voice",
            help="the LSO's voice clips (`burble voice build`; the server agent's voice_dir, on the same machine), "
                 "which the live view and replays without a radio recording say its calls with, in the voice that "
                 "said each (without them, the browser's own voice)")
    hub.set_defaults(func=_hub_serve)
    hub_sub = hub.add_subparsers(dest="hub_command")
    hub_sub.add_parser("serve", help="serve the API, greenie board, and pass pages (what `burble hub` does on its own)"
                       ).set_defaults(func=_hub_serve)
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
    set_admin = hub_sub.add_parser("set-admin", help="make a pilot an admin (they manage the hub on the website, at "
                                                     "/admin, while signed in)")
    set_admin.add_argument("pilot", help="the pilot's name, as on the board (they must have set a password)")
    set_admin.add_argument("--remove", action="store_true", help="stop them being an admin")
    set_admin.set_defaults(func=_hub_set_admin)
    set_config = hub_sub.add_parser("set-config", help="set a server agent's configuration (JSON file)")
    set_config.add_argument("name")
    set_config.add_argument("file")
    set_config.set_defaults(func=_hub_set_config)
    hub_sub.add_parser("config-reference", help="print every server agent configuration key, as Markdown (for the "
                                                "wiki)").set_defaults(func=_hub_config_reference)
    tel = hub_sub.add_parser("telemetry", help="usage reports: anonymous counts sent once a day (on unless turned off)")
    tel_sub = tel.add_subparsers(dest="telemetry_command")
    tel.set_defaults(func=_hub_telemetry, telemetry_command="status")
    tel_sub.add_parser("status", help="on or off, where reports go, and what is and isn't sent").set_defaults(
        func=_hub_telemetry)
    tel_sub.add_parser("show", help="print the report as it would be sent now (sends nothing)").set_defaults(
        func=_hub_telemetry)
    tel_sub.add_parser("on", help="send usage reports").set_defaults(func=_hub_telemetry)
    tel_sub.add_parser("off", help="send nothing").set_defaults(func=_hub_telemetry)
    tel_urls = tel_sub.add_parser("urls", help="where reports go (each address gets one); no arguments: show them")
    tel_urls.add_argument("urls", nargs="*", metavar="URL", help="usage receivers, e.g. https://telemetry.wrycu.com")
    tel_urls.add_argument("--default", action="store_true", help="back to the project's receiver")
    tel_urls.add_argument("--none", action="store_true", help="no addresses (sends nothing)")
    tel_urls.set_defaults(func=_hub_telemetry)
    tel_view = tel_sub.add_parser("view", help="show a usage receiver's stats on the admins' Usage page")
    tel_view.add_argument("url", nargs="?", help="the receiver's address")
    tel_view.add_argument("key", nargs="?", help="its API key (`burble telemetry new-key`) or password")
    tel_view.add_argument("--clear", action="store_true", help="disconnect it")
    tel_view.set_defaults(func=_hub_telemetry)

    telemetry = sub.add_parser("telemetry", help="run a usage report receiver (without a hub), or manage it with a "
                                                 "command")
    lc.add(telemetry, "telemetry", "--data-dir", kind="path", default=lc.home() / "telemetry",
           help="where its database (telemetry.db) lives")
    lc.add(telemetry, "telemetry", "--host", default="127.0.0.1", help="the address to serve on")
    lc.add(telemetry, "telemetry", "--port", kind="int", default=8100, help="the port to serve on")
    telemetry.set_defaults(func=_telemetry_serve)
    telemetry_sub = telemetry.add_subparsers(dest="receiver_command")
    telemetry_sub.add_parser("serve", help="take hubs' reports; the Usage page at / (with the password); what "
                                           "`burble telemetry` does on its own").set_defaults(func=_telemetry_serve)
    telemetry_sub.add_parser("set-password", help="set the password for the Usage page (and the API)").set_defaults(
        func=_telemetry_set_password)
    t_key = telemetry_sub.add_parser("new-key", help="make an API key for /api/v1/usage (e.g. for a hub's Usage page)")
    t_key.add_argument("name", help="what it's for, e.g. the hub's name")
    t_key.set_defaults(func=_telemetry_new_key)
    t_revoke = telemetry_sub.add_parser("revoke-key", help="revoke an API key")
    t_revoke.add_argument("name")
    t_revoke.set_defaults(func=_telemetry_revoke_key)
    telemetry_sub.add_parser("keys", help="list the API keys").set_defaults(func=_telemetry_keys)

    voice = sub.add_parser("voice", help="LSO voice clips")
    voice_sub = voice.add_subparsers(dest="voice_command", required=True)
    build = voice_sub.add_parser("build", help="render the LSO phrases with Kokoro (a clip set per speaker) or a Piper "
                                               "voice model")

    def voice_opt(*flags: str, **kw) -> None:
        lc.add(build, "voice", *flags, **kw)

    voice_opt("--engine", choices=["kokoro", "piper"], default="kokoro",
              help="Kokoro (needs espeak-ng installed) or Piper; a model named on the "
                   "command line picks it by its name")
    voice_opt("model", kind="path", shown='"~/.burble/models/kokoro-v1.0.onnx"',
              default=lambda a: lc.home() / "models" / ("kokoro-v1.0.onnx" if a.engine == "kokoro"
                                                        else "en_US-ryan-high.onnx"),
              help="the voice model: Kokoro's kokoro-v1.0.onnx, or a Piper .onnx with its .onnx.json beside it (for "
                   "Piper, ~/.burble/models/en_US-ryan-high.onnx by default)")
    voice_opt("--kokoro-voices", kind="path", default=lc.home() / "models" / "voices-v1.0.bin",
              help="Kokoro's voices-v1.0.bin")
    voice_opt("--speaker", dest="speakers", kind="list", default=KOKORO_SPEAKERS,
              help="the Kokoro voices to build, a clip set each (repeat the flag for more)")
    voice_opt("--out-dir", "-o", kind="path", default=lc.home() / "voice",
              help="the folder the clip sets go in (the agent's voice_dir)")
    voice_opt("--name", help="the Piper clip set's folder name (default: the model's name)")
    voice_opt("--speed", kind="float", default=1.15, help="speaking speed (1 = the voice's normal pace)")
    build.set_defaults(func=_voice_build)

    upload = sub.add_parser("upload", help="slice recordings and upload every pass to the hub")
    upload.add_argument("recordings", nargs="+")
    lc.add(upload, "upload", "--url", default="http://127.0.0.1:8000", help="the hub's address")
    lc.add(upload, "upload", "--token", help="an upload token (a pilot or server agent token)")
    upload.add_argument("--debrief", metavar="PATH",
                        help="DCS debrief.log for all recordings (default: <name>.debrief.log next to each one)")
    upload.add_argument("--dcs-log", metavar="PATH",
                        help="the DCS server's dcs.log with the burble server hook's events of the recordings' mission "
                             "(e.g. a server agent's session archive): pilots checked, DCS's grades and wires, "
                             "liveries, and when each pass was flown, as the agent does live")
    upload.add_argument("--pilot", help="only this pilot's passes")
    upload.add_argument("--dry-run", action="store_true", help="list the passes, don't upload")
    upload.set_defaults(func=_upload)

    from .acmi.stream import DEFAULT_PORT as TACVIEW_PORT

    def agent_args(p: argparse.ArgumentParser, mode: str, section: str) -> None:
        def opt(*flags: str, **kw) -> None:
            lc.add(p, section, *flags, **kw)

        folder = "agent" if mode == "server" else "pilot-uploader"
        if mode == "server":
            p.add_argument("instance", nargs="?", help="which DCS server, for several on one machine: its own table in "
                                                       "the config ([agent.<instance>]) over [agent]")
        opt("--work-dir", kind="path", default=lambda a: lc.home() / folder / (getattr(a, "instance", None) or ""),
            shown=f'"~/.burble/{folder}"', help="session archive and upload outbox"
            + (" (for an instance, by default ~/.burble/agent/<instance>)" if mode == "server" else ""))
        opt("--url", default="http://127.0.0.1:8000", help="the hub's address")
        opt("--token", help="a server agent token (`burble hub add-agent <name>`)" if mode == "server"
            else "a pilot token (from your settings page on the hub)")
        opt("--tacview-host", default="127.0.0.1", help="where DCS's Tacview real-time telemetry is")
        opt("--tacview-port", kind="int", default=TACVIEW_PORT, help="its port")
        opt("--tacview-password", help="its password (tacviewRealTimeTelemetryPassword), if set")
        opt("--dcs-log", kind="path", help="DCS Logs/dcs.log, for the burble server hook's events")
        opt("--debrief", kind="path", help="DCS Logs/debrief.log (default: next to dcs_log)")
        if mode == "server":
            opt("--voice-dir", kind="path", default=lc.home() / "voice",
                help="the LSO's voice clips: a folder of clip sets (`burble voice build`), of which the hub's config "
                     "picks (callouts.voice), or one clip set")
            opt("--listen-model", kind="path", default=lc.home() / "models" / "vosk-model-small-en-us-0.15",
                help="a Vosk model, to hear pilots' calls on the LSO frequencies and answer them, if the hub's "
                     "config says so (callouts.listen); downloaded when missing, if named like vosk-model-...")
        for name, default, what in (("archives", 90, "session archives (raw recordings, for re-slicing)"),
                                    ("sent", 14, "local copies of uploaded slices (the hub keeps its own)"),
                                    ("rejected", 30, "slices the hub rejected")):
            opt(f"--keep-{name}-days", kind="float", metavar="DAYS", shown=str(default),
                help=f"keep {what} this long; 0 = forever (unset: the hub's \"retention\" setting for this agent, else "
                     f"{default})")
        opt("-v", "--verbose", dest="verbose", kind="bool", default=False, help="log more")
        p.set_defaults(func=_agent, mode=mode)

    agent_args(sub.add_parser("agent", help="server agent: live Tacview stream -> passes, LSO calls -> hub"), "server",
               "agent")
    agent_args(sub.add_parser("pilot-uploader", help="pilot uploader: your own jet's passes from your PC -> hub "
                                                     "(never transmits)"), "pilot", "pilot_uploader")
    args = parser.parse_args(argv)
    if getattr(args, "_options", None):
        config, path = lc.load(args.config)
        lc.resolve(args, config, path)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
