"""The F-14 (PLAN #34): a Tomcat trap on a dedicated server (the server's copy of the jet), and the two problems its
session showed: Tacview reusing an object's id for a respawned jet, and keeping the old pilot's name."""

from pathlib import Path

from burble.acmi import load_recording
from burble.callouts import Thresholds
from burble.detect.passes import Outcome, find_passes
from burble.detect.wire import wire_signals
from burble.geometry import AIRCRAFT, CARRIERS, DeckFrame
from burble.grading import grade_pass
from burble.grading.grade import Position

FIXTURE = Path(__file__).parent / "fixtures" / "live" / "tomcat-trap-server.zip.acmi"


def test_tomcat_trap_on_the_server():
    """Jive's F-14B(U) trap: DCS's LSO gave it "--- : _WX_ _DRX_ _LOIM_ LOIC WIRE# 3" (no AOA remarks)."""
    [p] = list(find_passes(load_recording(FIXTURE)))
    assert (p.aircraft_type, p.pilot, p.outcome) == ("F-14BU", "Jive", Outcome.TRAP)
    aircraft = AIRCRAFT[p.aircraft_type]
    # AOA from motion, with the Tomcat's offset: about on speed through the groove (DCS made no AOA remark).
    stats = {st.position: st for st in grade_pass(p).positions if st.samples}
    for pos in (Position.X, Position.IM, Position.IC):
        assert aircraft.on_speed_aoa[0] - 0.5 < stats[pos].aoa < aircraft.on_speed_aoa[1] + 0.5
    # The wire from the server's copy: the stop point, with the Tomcat's runout, is at wire 3.
    signals = wire_signals(p.samples, DeckFrame(CARRIERS[p.carrier_type], aircraft))
    assert signals.stop_wire == 3 and abs(signals.stop_error_m) < 2.0


def test_live_callouts_use_the_aircraft_on_speed_band():
    t = Thresholds().for_aircraft(AIRCRAFT["F-14BU"].on_speed_aoa)
    assert (t.aoa_fast, t.aoa_slow) == (10.2, 11.1)
    assert (Thresholds().for_aircraft(AIRCRAFT["FA-18C_hornet"].on_speed_aoa).aoa_fast) == Thresholds().aoa_fast


def _recording_with_reused_id(tmp_path):
    """Object 103: a Hornet, removed, then (the id reused) a Tomcat that Tacview still says Wrycu flies."""
    lines = ["FileType=text/acmi/tacview", "FileVersion=2.2", "0,ReferenceTime=2016-06-21T05:00:00Z"]
    for i in range(10):
        lines += [f"#{i}", f"103,T=35|35|1000|0|0|0|{i}|0|0,Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=Wrycu"]
    lines += ["#10", "-103"]
    for i in range(20, 30):
        lines += [f"#{i}", f"103,T=35|35|1000|0|0|0|{i}|0|0,Type=Air+FixedWing,Name=F-14BU,Pilot=Wrycu"]
    path = tmp_path / "reused.txt.acmi"
    path.write_text("\n".join(lines) + "\n")
    return load_recording(path)


def test_a_reused_object_id_is_two_objects(tmp_path):
    track = _recording_with_reused_id(tmp_path).objects[0x103]
    hornet, tomcat = track.lives()
    assert (hornet.name, len(hornet.samples), hornet.removed_at) == ("FA-18C_hornet", 10, 10.0)
    assert (tomcat.name, len(tomcat.samples), tomcat.samples[0].time) == ("F-14BU", 10, 20.0)
    assert tomcat.id == hornet.id == 0x103


def _feed(tmp_path, *events: str):
    from burble.agent.service import HookFeed
    from burble.dcslog import parse_hook_line

    log_file = tmp_path / "dcs.log"
    log_file.write_text("")
    feed = HookFeed(log_file)
    prefix = "2026-10-07 20:00:00.000 INFO    BURBLE (Main): BURBLE "
    for e in events:
        feed.add(parse_hook_line(prefix + e))
    return feed


def test_the_pilot_comes_from_the_hook_when_tacview_kept_an_old_name(tmp_path):
    """As on the server: Wrycu flew a Hornet and left; Jive took a Tomcat, which got the Hornet's old id and
    Tacview named it Wrycu's."""
    feed = _feed(tmp_path,
                 '{"event":"slot","t":631,"player":"Wrycu","type":"FA-18C_hornet","unit":"Aerial-5-1"}',
                 '{"event":"slot","t":824,"player":"Jive","type":"F-14BU","unit":"carrier landing (start)-1-1"}')
    # By slots: Wrycu was last in a Hornet; the only player in a Tomcat was Jive.
    assert feed.pilot_for("Wrycu", "F-14BU", 0x103, 1158.0, 1235.0) == "Jive"
    assert feed.pilot_for("Wrycu", "FA-18C_hornet", 0x103, 700.0, 760.0) == "Wrycu"  # his own Hornet pass
    assert feed.pilot_for("Jive", "F-14BU", 0x303, 2110.0, 2160.0) == "Jive"
    assert feed.pilot_for("AI-1", "F-14BU", 0x403, 2110.0, 2160.0) == "AI-1"  # no slot: not a player's name


def test_burble_grade_names_the_pilot(tmp_path):
    feed = _feed(tmp_path,
                 '{"event":"slot","t":631,"player":"Wrycu","type":"F-14BU","unit":"a"}',
                 '{"event":"slot","t":824,"player":"Jive","type":"F-14BU","unit":"b"}',
                 '{"event":"landing_quality_mark","t":1195.2,"comment":"LSO: GRADE:C : LOIM WIRE# 2",'
                 '"initiator":{"type":"F-14BU","object_id":16777474,"player":"Jive","unit_id":"371"}}')
    # Both in Tomcats, so slots can't tell; DCS's grade for object 0x103 (16777474) names Jive.
    assert feed.pilot_for("Wrycu", "F-14BU", 0x103, 1158.0, 1180.0) == "Jive"
    assert feed.pilot_for("Wrycu", "F-14BU", 0x203, 1158.0, 1180.0) == "Wrycu"  # another jet's grade


def test_backfill_with_the_server_dcs_log(tmp_path):
    """`burble upload --dcs-log`: a server agent's session archive, with the server hook's events from the
    server's dcs.log, as the agent would have sent it live."""
    from burble.agent.service import HookFeed
    from burble.slices import slice_recording

    log_file = tmp_path / "dcs.log"
    log_file.write_text("\n".join([
        '2026-10-07 20:00:00.000 INFO    BURBLE (Main): BURBLE {"event":"handler_installed","t":0}',
        '2026-10-07 20:11:01.539 INFO    BURBLE (Main): BURBLE {"type":"F-14BU","group":"f-14","t":823.972,'
        '"event":"slot","unit":"u","player":"Jive","livery":"vf-32","onboard_num":"016","unit_id":371}',
        '2026-10-07 20:45:53.424 INFO    SCRIPTING (Main): BURBLE {"comment":"LSO: GRADE:--- : _WX_  _DRX_  _LOIM_'
        '  LOIC  WIRE# 3 EGIW [BC]","initiator":{"type":"F-14BU","object_id":16780802,"player":"Jive",'
        '"unit_id":"371"},"place":{"name":"CVN-75 Harry S. Truman"},"t":2915.84,"event":"landing_quality_mark"}',
    ]) + "\n")
    hooks = HookFeed(log_file, follow=False)
    [(acmi, meta)] = slice_recording(FIXTURE, tmp_path / "out", hooks=hooks, pilot="Jive")
    assert meta["pass"]["pilot"] == "Jive"
    assert meta["dcs"]["wire"] == 3 and meta["wire_source"] == "dcs-lso"
    assert meta["aircraft"]["onboard_num"] == "016"
    # Flown at 20:45 (the mission clock had stopped for a day while the server was empty).
    assert meta["pass"]["occurred_at"].startswith("2026-10-07T20:45:")
    assert slice_recording(FIXTURE, tmp_path / "out2", hooks=hooks, pilot="Wrycu") == []
