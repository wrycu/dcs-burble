import math

import pytest

from dcs_lso.exportprobe import analyse_file

FPS = 60.0


def true_pos(t: float) -> tuple[float, float, float]:
    """A gentle turning descent at 70 m/s."""
    w = 0.05
    return (1400 * math.sin(w * t), 300 - 3.5 * t, 1400 * (1 - math.cos(w * t)))


def write_probe(path, update_hz: float, extrapolate: bool, seconds: float = 30.0) -> None:
    """Server frames at FPS; the client's position arrives at update_hz (slightly jittered) and the
    server holds it, or extrapolates it at the last received velocity, between updates."""
    lines = ["t,id,unit,x,y,z,heading,pitch,bank,lat,lon,alt"]
    last_t, last_p, last_v = None, None, (0.0, 0.0, 0.0)
    next_update = 0.0
    n = 0
    for i in range(int(seconds * FPS)):
        t = i / FPS
        if t >= next_update:
            p = true_pos(t)
            if last_p is not None:
                last_v = tuple((a - b) / (t - last_t) for a, b in zip(p, last_p))
            last_t, last_p = t, p
            n += 1
            next_update = n / update_hz + (0.004 if n % 3 == 0 else 0.0)
        dt = t - last_t
        x, y, z = (a + v * dt for a, v in zip(last_p, last_v)) if extrapolate else last_p
        lines.append(f"{t:.9f},16777473,Pilot,{x:.6f},{y:.6f},{z:.6f},0,0,0,0,0,0")
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("update_hz", [5.0, 10.0, 20.0])
def test_held_positions_give_the_update_rate(tmp_path, update_hz):
    write_probe(tmp_path / "p.csv", update_hz, extrapolate=False)
    (r,) = analyse_file(tmp_path / "p.csv")
    assert r.frame_hz == pytest.approx(FPS, rel=0.01)
    assert r.change_hz == pytest.approx(update_hz, rel=0.1)


# Up to a quarter of the frame rate, corrections are frames apart and are counted reliably; the
# question the probe answers is whether the real rate is clearly above Tacview's 5 Hz.
@pytest.mark.parametrize("update_hz", [5.0, 10.0, 15.0])
def test_extrapolated_positions_show_correction_spikes(tmp_path, update_hz):
    write_probe(tmp_path / "p.csv", update_hz, extrapolate=True)
    (r,) = analyse_file(tmp_path / "p.csv")
    assert r.unchanged_pct < 1
    assert r.spike_hz == pytest.approx(update_hz, rel=0.1)
    assert r.spike_interval_s == pytest.approx(1 / update_hz, rel=0.1)


@pytest.mark.parametrize("update_hz", [5.0, 10.0])
def test_period_of_regular_updates(tmp_path, update_hz):
    write_probe(tmp_path / "p.csv", update_hz, extrapolate=True)
    (r,) = analyse_file(tmp_path / "p.csv")
    assert r.period_s == pytest.approx(1 / update_hz, rel=0.1)


def test_sessions_and_comment_lines(tmp_path):
    write_probe(tmp_path / "a.csv", 10.0, extrapolate=False, seconds=5)
    text = (tmp_path / "a.csv").read_text()
    (tmp_path / "p.csv").write_text("# ship 1 CVN_75 Truman carrier=true\n" + text + text)
    assert [r.session for r in analyse_file(tmp_path / "p.csv")] == [1, 2]
