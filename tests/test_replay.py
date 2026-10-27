import math
from pathlib import Path

from burble.acmi import load_recording
from burble.cards import render_lineup
from burble.cards.replay import LEAD_S, TAIL_S, replay_data
from burble.detect import find_passes
from burble.geometry import AIRCRAFT, CARRIERS, DeckFrame
from burble.grading import grade_pass

from test_hub import PASS_FILES, client, hub, token, upload  # noqa: F401 (fixtures)

FIXTURES = Path(__file__).parent / "fixtures"


def _replay(name: str) -> tuple[dict, object]:
    recording = load_recording(FIXTURES / "live" / name)
    (p,) = [p for p in find_passes(recording) if p.outcome.value == "trap"]
    trap = min(p.samples, key=lambda s: abs(s.time - (p.end_time - 3)))  # as the jet comes to a stop
    return replay_data(recording, p, grade_pass(p), [{"time": trap.time, "along": trap.along, "call": "welcome aboard"}]), p


def test_track_is_in_the_carriers_frame():
    data, p = _replay("trap-server.zip.acmi")
    track, carrier = data["track"], data["carrier"]
    assert track["t"][0] >= 0 and track["t"][-1] <= p.end_time - p.start_time + LEAD_S + TAIL_S
    assert track["t"] == sorted(track["t"]) and len({len(v) for v in track.values()}) == 1
    # Stopped on deck after the trap: at deck height (the jet's origin 1.8 m up), about 90 m past the aim point.
    frame = DeckFrame(CARRIERS[p.carrier_type], AIRCRAFT[p.aircraft_type])
    x, z = track["x"][-1], track["z"][-1]
    assert 1.5 < track["y"][-1] < 2.1
    along, lateral = frame.deck_point(x, z)
    assert -105 < along < -75 and abs(lateral) < 10
    # The groove starts out astern and high, heading roughly up the angled deck.
    assert track["z"][0] < -1500 and track["y"][0] > 100
    assert abs(track["heading"][len(track["heading"]) // 2] + carrier["deck_angle"]) < 6
    # The landing area's points sit where the deck frame puts them.
    assert math.isclose(frame.deck_point(*carrier["ramp"])[0], CARRIERS[p.carrier_type].ramp_along_m, abs_tol=0.05)
    assert carrier["caught"] == p.wire_estimate and 10 < carrier["speed"] < 20
    # For the PLAT monitor: the mission's clock (Tacview's ReferenceTime + the replay's start), the closing speed.
    assert data["clock"].startswith("2016-06-21T05:") and len(data["groove"]["speed"]) == len(p.samples)
    assert 50 < data["groove"]["speed"][len(p.samples) // 2] < 90  # m/s relative to the ship, in the groove


def test_no_clock_without_a_real_date():
    """The pilot hook's files count from a stand-in date (2000-01-01): the PLAT monitor shows no date or time."""
    data, _ = _replay("trap-pilot-hook.zip.acmi")
    assert data["clock"] is None


def test_heading_is_unwrapped_and_calls_are_on_the_replays_clock():
    data, p = _replay("tomcat-trap-server.zip.acmi")
    headings = data["track"]["heading"]
    assert max(abs(b - a) for a, b in zip(headings, headings[1:])) < 30  # no jumps at ±180
    assert data["aircraft"]["model"] == "tomcat"
    (call,) = data["calls"]
    trap = min(p.samples, key=lambda s: abs(s.time - (p.end_time - 3)))
    assert math.isclose(call["t"], trap.time - data["start"], abs_tol=0.01) and call["text"] == "Welcome aboard"
    assert len(data["groove"]["t"]) == len(p.samples)


def test_calls_on_another_clock_are_moved_onto_the_tracks():
    """The first pilot hook's landings (2026-10-05) have calls 5-13 s off the track's clock; placed by where
    they were made (as the trap card does), "welcome aboard" comes after the trap, not in the air."""
    recording = load_recording(FIXTURES / "live" / "trap-server.zip.acmi")
    (p,) = [p for p in find_passes(recording) if p.outcome.value == "trap"]
    groove = min(p.samples, key=lambda s: abs(s.along - 500))
    trap = min(p.samples, key=lambda s: abs(s.along + 40))  # just after the touchdown, still rolling
    late = 12.0
    calls = [{"time": groove.time - late, "along": groove.along, "call": "power"},
             {"time": trap.time - late, "along": trap.along, "call": "welcome aboard"}]
    data = replay_data(recording, p, calls=calls)
    power, welcome = data["calls"]
    assert math.isclose(power["t"], groove.time - data["start"], abs_tol=0.01)
    assert math.isclose(welcome["t"], trap.time - data["start"], abs_tol=0.01)
    # A small difference (the server's copy against the pilot's own track) is left alone.
    data = replay_data(recording, p, calls=[{"time": groove.time - 0.3, "along": groove.along, "call": "power"}])
    assert math.isclose(data["calls"][0]["t"], groove.time - 0.3 - data["start"], abs_tol=0.01)


def test_replay_page_and_data(client, token):
    pass_id = upload(client, token, PASS_FILES[0]).json()["pass_id"]
    assert f'href="/passes/{pass_id}/replay"' in client.get(f"/passes/{pass_id}").text
    page = client.get(f"/passes/{pass_id}/replay")
    assert page.status_code == 200 and f'data-src="/passes/{pass_id}/replay.json"' in page.text
    assert '<script type="importmap">' in page.text and "/static/replay.js" in page.text
    assert 'class="platframe"' in page.text and 'id="plat-distance"' in page.text  # the PLAT monitor's overlay
    # The trap card's lineup plot below it, with the scales and parts the page moves.
    assert 'id="replay-lineup"' in page.text and 'data-half="' in page.text
    assert 'id="lineup-reveal-rect"' in page.text and 'id="lineup-now"' in page.text
    data = client.get(f"/passes/{pass_id}/replay.json").json()
    assert data["track"]["t"] and data["carrier"]["wires"] and data["grade"]["grade"]
    script = client.get("/static/replay.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    # Never a stale copy after the hub is updated or given a voice.
    assert script.headers["cache-control"] == "no-cache"
    assert client.get(f"/passes/{pass_id}/replay.json").headers["cache-control"] == "no-cache"
    assert client.get("/passes/999/replay.json").status_code == 404
    # three.js is served by the hub, not a CDN.
    assert "cdn" not in page.text and "/static/three-0.169.0/three.module.min.js" in page.text
    three = client.get("/static/three-0.169.0/three.module.min.js")
    assert three.status_code == 200 and "immutable" in three.headers["cache-control"]
    assert client.get("/static/../app.py").status_code == 404 and client.get("/static/nope.js").status_code == 404


def test_what_the_lso_said():
    from burble.callouts.voice import spoken_text

    assert spoken_text({"call": "power", "time": 1}) == "Power."
    assert spoken_text({"call": "roger ball", "text": "Roger ball, 25 knots."}) == "Roger ball, 25 knots."
    assert spoken_text({"call": "welcome aboard, two wire"}) == "Welcome aboard, two wire."
    assert spoken_text({"call": "something new"}) == "Something new."  # a call this version doesn't know
    # Exactly what was said, when the agent recorded it (PLAN #51): the variant, side number, and dig.
    assert spoken_text({"call": "welcome aboard", "said": "301, Welcome home, good trap."}) == \
        "301, Welcome home, good trap."
    assert spoken_text({"call": "roger ball", "text": "Roger ball, 25 knots", "said": "Roger ball, 25 knots."}) == \
        "Roger ball, 25 knots."


def test_the_subtitles_say_what_was_said(client, token, hub):
    pass_id = upload(client, token, PASS_FILES[0]).json()["pass_id"]
    from burble.hub.db import Pass

    with hub.sessions() as s:
        s.get(Pass, pass_id).calls = [
            {"time": 3290.0, "along": 500.0, "call": "power"},
            {"time": 3300.0, "along": 0.0, "call": "welcome aboard, despite the wave off",
             "said": "You did hear the wave off, right?"}]
        s.commit()
    calls = client.get(f"/passes/{pass_id}/replay.json").json()["calls"]
    assert [(c["text"], c["say"]) for c in calls] == [
        ("Power", "Power."), ("You did hear the wave off, right?", "You did hear the wave off, right?")]


def test_call_audio_in_the_hubs_voice(client, token, hub, tmp_path):
    pass_id = upload(client, token, PASS_FILES[0]).json()["pass_id"]
    from burble.hub.db import Pass

    with hub.sessions() as s:
        s.get(Pass, pass_id).calls = [{"time": 3300.0, "along": 300.0, "call": "power"},
                                      {"time": 3290.0, "along": 500.0, "call": "ball", "by": "pilot", "text": "Hornet ball, 5.2"},
                                      {"time": 3290.0, "along": 500.0, "call": "roger ball", "text": "Roger ball, 20 knots."}]
        s.commit()
    # Without a voice: the text to speak (the browser's voice), no audio.
    calls = client.get(f"/passes/{pass_id}/replay.json").json()["calls"]
    assert [(c["by"], c.get("say"), "audio" in c) for c in calls] == [
        ("pilot", None, False), ("lso", "Roger ball, 20 knots.", False), ("lso", "Power.", False)]
    assert client.get(f"/passes/{pass_id}/calls/1.wav").status_code == 404

    said = []

    class Voice:  # stands in for Piper
        def wav(self, text):
            said.append(text)
            path = tmp_path / "said.wav"
            path.write_bytes(b"RIFF....WAVE")
            return path

    hub.voice = Voice()
    calls = client.get(f"/passes/{pass_id}/replay.json").json()["calls"]
    assert [c.get("audio") for c in calls] == [None, f"/passes/{pass_id}/calls/1.wav", f"/passes/{pass_id}/calls/2.wav"]
    r = client.get(f"/passes/{pass_id}/calls/2.wav")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and said == ["Power."]
    assert client.get(f"/passes/{pass_id}/calls/0.wav").status_code == 404  # the pilot's own call
    assert client.get(f"/passes/{pass_id}/calls/9.wav").status_code == 404


def test_lineup_plot_for_the_replay():
    _, p = _replay("trap-server.zip.acmi")
    calls = [{"time": p.start_time + 5, "along": p.samples[10].along, "call": "come left", "by": "lso"}]
    svg = render_lineup(p, calls)
    ghosts = svg.count(' tc-ghost"')
    assert ghosts and svg.count('class="tc-track') == 2 * ghosts  # the whole track faintly, the flown part over it
    assert 'clip-path="url(#lineup-reveal)"' in svg and f'data-along="{calls[0]["along"]:.1f}"' in svg
    assert "Glideslope" not in svg  # the lineup plot only
