"""P5 tools: measure SRS relay latency, and transmit test audio for a cockpit check."""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from array import array

from .audio import load_wav, speak
from .client import Radio, SrsClient
from .opus import Decoder, encode_pcm, tone
from .packet import Modulation


def build_audio(tone_seconds: float, wav: str | None, text: str | None) -> array:
    if wav:
        return load_wav(wav)
    if text:
        return speak(text)
    return tone(tone_seconds)


async def latency_test(host: str, port: int, freq_mhz: float, modulation: Modulation,
                       seconds: float, rounds: int) -> int:
    radio = Radio(freq_mhz, modulation)
    sent: dict[int, float] = {}
    received: dict[int, tuple[float, bytes]] = {}

    def on_voice(packet, at):
        received.setdefault(packet.packet_number, (at, packet.audio))

    rx = SrsClient(host, port, name="dcs-lso-rx", radios=(radio,), on_voice=on_voice)
    tx = SrsClient(host, port, name="dcs-lso-tx", radios=(radio,))
    t0 = time.perf_counter()
    await rx.connect()
    t1 = time.perf_counter()
    await tx.connect()
    t2 = time.perf_counter()
    print(f"SRS server {tx.server_version} at {host}:{port}; connected rx in {1000 * (t1 - t0):.0f} ms, "
          f"tx in {1000 * (t2 - t1):.0f} ms; {len(tx.clients)} clients on the server")
    await asyncio.sleep(0.5)  # let the server register both UDP endpoints
    frames = encode_pcm(tone(seconds))
    first_audio: list[float] = []
    for r in range(rounds):
        before = dict(received)
        call = time.perf_counter()
        await tx.transmit(frames, on_sent=lambda n, t: sent.__setitem__(n, t))
        await asyncio.sleep(0.3)
        new = [v[0] for k, v in received.items() if k not in before]
        if new:
            first_audio.append(1000 * (min(new) - call))
    await asyncio.sleep(0.2)
    await tx.close()
    await rx.close()

    lat = sorted(1000 * (received[n][0] - sent[n]) for n in sent if n in received)
    lost = len(sent) - len(lat)
    print(f"frames sent {len(sent)}, received {len(lat)}, lost {lost}")
    if not lat:
        print("FAIL: nothing received (frequency/modulation/coalition mismatch, or UDP blocked?)")
        return 1
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
    print(f"relay latency per frame (send -> receive): p50 {p(.5):.2f} ms  p95 {p(.95):.2f} ms  max {lat[-1]:.2f} ms")
    if first_audio:
        print(f"transmit() call -> first audio received: p50 {statistics.median(first_audio):.2f} ms "
              f"max {max(first_audio):.2f} ms over {len(first_audio)} transmissions")
    decoded = Decoder().decode(next(iter(received.values()))[1])
    print(f"received audio decodes: {len(decoded)} samples, peak {max(abs(x) for x in decoded)}")
    return 0 if lost == 0 else 1


async def say(host: str, port: int, freq_mhz: float, modulation: Modulation, coalition: int,
              name: str, audio: array, interactive: bool) -> int:
    client = SrsClient(host, port, name=name, coalition=coalition, radios=(Radio(freq_mhz, modulation),))
    await client.connect()
    frames = encode_pcm(audio)
    print(f"connected to SRS {client.server_version} at {host}:{port} as {name!r} "
          f"on {freq_mhz:.3f} {modulation.name} (coalition {coalition}); clip is {len(frames) * 40} ms")
    try:
        if not interactive:
            await client.transmit(frames)
            return 0
        loop = asyncio.get_running_loop()
        while True:
            print("press Enter to transmit (Ctrl-D to quit)", flush=True)
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return 0
            start = time.perf_counter()
            first: list[float] = []
            await client.transmit(frames, on_sent=lambda n, t: first or first.append(t))
            print(f"  sent {len(frames)} frames; first frame left {1000 * (first[0] - start):.1f} ms after Enter")
    finally:
        await client.close()
