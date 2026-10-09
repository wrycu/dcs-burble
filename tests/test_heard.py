"""Pilot calls heard on SRS (PLAN #20): parsing recognised text, and putting voice packets back together."""

import os
from array import array
from pathlib import Path

import pytest

from burble.callouts.heard import PilotCall, parse
from burble.srs.listen import END_GAP_S, Transmissions
from burble.srs.opus import encode_pcm, tone
from burble.srs.packet import VoicePacket


@pytest.mark.parametrize("text, want", [
    ("three zero one hornet ball five point two", PilotCall("ball", "301", "Hornet", 5.2)),
    ("Three zero one, Hornet ball, five point two.", PilotCall("ball", "301", "Hornet", 5.2)),
    ("three oh one hornet ball four point eight", PilotCall("ball", "301", "Hornet", 4.8)),
    ("one zero two hornet ball three point niner", PilotCall("ball", "102", "Hornet", 3.9)),
    ("305 tomcat ball 8.4", PilotCall("ball", "305", "Tomcat", 8.4)),
    ("hornet ball five point five", PilotCall("ball", None, "Hornet", 5.5)),
    ("three zero one clara", PilotCall("clara", "301")),
    ("three zero one clara five point two", PilotCall("clara", "301", None, 5.2)),
    ("[unk] zero one ball", PilotCall("ball", "01")),  # a lost first digit: two digits still taken
    ("three zero one paddles how do you read", PilotCall("paddles", "301")),
    ("[unk] [unk]", PilotCall(None)),
    # From the user's own calls over SRS (2026-10-06):
    ("three zero five hornet ball five point three correction five point eight", PilotCall("ball", "305", "Hornet", 5.8)),
    ("paddles three zero five [unk]", PilotCall("paddles", "305")),
    ("five hornet ball five point two", PilotCall("ball", None, "Hornet", 5.2)),  # a clipped start: no side number
    ("three oh five hornet ball five point one", PilotCall("ball", "305", "Hornet", 5.1)),
    ("", PilotCall(None)),  # a push-to-talk blip
])
def test_parse(text, want):
    got = parse(text)
    assert (got.call, got.side_number, got.aircraft, got.fuel) == (want.call, want.side_number, want.aircraft, want.fuel)


def packets(guid: str, seconds: float, unit: int = 42):
    for n, frame in enumerate(encode_pcm(tone(seconds))):
        yield VoicePacket(frame, (127.5e6,), (0,), (0,), unit, n, guid)


def test_transmissions_are_put_back_together_per_speaker():
    heard = Transmissions({"A" * 22: {"Name": "Wrycu"}}, own_guid="O" * 22)
    t = 0.0
    for a, b in zip(packets("A" * 22, 1.0), packets("B" * 22, 0.4, unit=7)):
        heard.add(a, t), heard.add(b, t)
        t += 0.04
    for p in packets("O" * 22, 0.4):
        heard.add(p, t)  # ours: left out
    assert heard.finished(t) == []  # still talking
    done = heard.finished(t + END_GAP_S + 0.01)
    assert [(x.name, x.unit_id, round(x.seconds, 1)) for x in done] == [("Wrycu", 42, 0.4), ("", 7, 0.4)]


@pytest.mark.skipif(not os.environ.get("BURBLE_VOSK_MODEL"), reason="set BURBLE_VOSK_MODEL to a Vosk model folder")
def test_recogniser_hears_a_ball_call():
    from burble.callouts.heard import Recogniser
    from burble.srs.audio import load_wav
    from piper import PiperVoice
    import wave, tempfile
    voice = PiperVoice.load(str(Path(__file__).parents[1] / "acmi" / "voices" / "en_US-ryan-high.onnx"))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "call.wav"
        with wave.open(str(path), "wb") as w:
            voice.synthesize_wav("three zero one, Hornet ball, five point two", w)
        pcm = load_wav(path)
    call = Recogniser(os.environ["BURBLE_VOSK_MODEL"]).hear(pcm)
    assert (call.call, call.side_number, call.aircraft, call.fuel) == ("ball", "301", "Hornet", 5.2)
