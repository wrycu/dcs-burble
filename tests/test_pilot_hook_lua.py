"""The pilot hook's Lua, run under LuaJIT with stubbed DCS APIs: the recorder (Export.lua side) writes an
approach file from a real trap, the uploader sends it, and the hub takes what it sent."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcs_lso.acmi import load_recording
from dcs_lso.hub.app import create_app
from dcs_lso.hub.db import Pass
from dcs_lso.hub.pilothook import parse_upload
from dcs_lso.hub.service import Hub
from test_pilot_hook import HOME, JOINED_S, PILOT, SERVER, UCID, make_hub, server_report

ROOT = Path(__file__).parents[1] / "pilot-hook"
RECORDER = ROOT / "Scripts" / "dcs-lso-pilot-recorder.lua"
UPLOADER = ROOT / "Scripts" / "Hooks" / "dcs-lso-pilot-hook.lua"
MISSION = "wrycu_training_syria_v1.12"


def luajit() -> str:
    path = shutil.which("luajit")
    if path is None:
        pytest.skip("luajit not installed")
    return path


def lua_samples() -> str:
    """The pilot's own track of a real wire-2 trap as DCS's LoGetSelfData would give it (mission time, DCS
    coordinates, radians), each with the carrier as LoGetWorldObjects would show it then (from the server's
    recording of the same trap)."""
    import bisect
    recording = load_recording(PILOT)
    (jet,) = [o for o in recording.objects.values() if o.name == "FA-18C_hornet"]
    (carrier,) = [o for o in load_recording(SERVER).objects.values() if o.name == "CVN_75"]
    times = [c.time for c in carrier.samples]
    rows = []
    for s in jet.samples:
        t, mission_t = s.transform, s.time + JOINED_S
        # The carrier moves smoothly in the game: interpolate between the server's samples of it.
        k = min(max(bisect.bisect_left(times, mission_t), 1), len(times) - 1)
        a, b = carrier.samples[k - 1], carrier.samples[k]
        f = min(max((mission_t - a.time) / (b.time - a.time), 0.0), 1.0) if b.time > a.time else 0.0
        lerp = lambda u, v: u + (v - u) * f  # noqa: E731
        ca, cb = a.transform, b.transform
        rows.append([mission_t, t.v, t.alt, t.u, math.radians(t.heading), math.radians(t.pitch), math.radians(t.roll),
                     s.aoa or 0, t.lat, t.lon, lerp(ca.v, cb.v), 0.0, lerp(ca.u, cb.u), math.radians(cb.heading),
                     lerp(ca.lat, cb.lat), lerp(ca.lon, cb.lon)])
    # Keep the frames coming after the track ends: the jet sits on deck, moving with the carrier at 14 m/s
    # (north), so over the map it never stands still.
    last = rows[-1]
    for i in range(1, 400):
        moved = list(last)
        moved[0], moved[1], moved[10] = last[0] + i * 0.1, last[1] + i * 1.4, last[10] + i * 1.4
        rows.append(moved)
    return "{" + ",\n".join("{" + ",".join(f"{v:.7f}" for v in r) + "}" for r in rows) + "}"


RECORDER_STUBS = r"""
WRITEDIR = ...
lfs = { writedir = function() return WRITEDIR end, mkdir = function(p) os.execute('mkdir -p "' .. p .. '"') end }
log = { INFO = 'INFO', write = function(src, lvl, msg) print('LOG ' .. msg) end }
local samples = SAMPLES
local i = 0
local chained = 0
LuaExportAfterNextFrame = function() chained = chained + 1 end  -- e.g. Tacview's, defined before ours
LoGetModelTime = function() return samples[i] and samples[i][1] end
LoGetSelfData = function()
  local s = samples[i]
  return { Name = 'FA-18C_hornet', Position = { x = s[2], y = s[3], z = s[4] }, Heading = s[5], Pitch = s[6],
           Bank = s[7], LatLongAlt = { Lat = s[9], Long = s[10] } }
end
LoGetAngleOfAttack = function() return samples[i][8] end
LoGetWorldObjects = function()
  local s = samples[i]
  return { [7] = { Name = 'FA-18C_hornet', Type = { level1 = 1 }, Position = { x = s[2], y = s[3], z = s[4] } },
           [42] = { Name = 'CVN_75', UnitName = 'CVN-75 Harry S. Truman', Type = { level1 = 3 },
                    Position = { x = s[11], y = s[12], z = s[13] }, Heading = s[14], LatLongAlt = { Lat = s[15], Long = s[16] } } }
end
LoGetPilotName = function() return 'Wrycu' end
dofile(RECORDER_PATH)
for n = 1, #samples do i = n; LuaExportAfterNextFrame() end
print('LOG mission ends')
LuaExportStop()
print('CHAINED ' .. chained)
"""


@pytest.fixture(scope="module")
def recorded(tmp_path_factory) -> Path:
    """The approach file the recorder writes for the real trap."""
    root = tmp_path_factory.mktemp("savedgames")
    script = f"RECORDER_PATH = {str(RECORDER)!r}\n" + RECORDER_STUBS.replace("SAMPLES", lua_samples())
    out = subprocess.run([luajit(), "-", f"{root}/"], input=script, capture_output=True, text=True, check=True)
    chained = int(next(line for line in out.stdout.splitlines() if line.startswith("CHAINED")).split()[1])
    files = sorted((root / "Logs" / "dcs-lso").glob("approach-*.csv"))
    assert len(files) == 1, out.stdout
    logs = [line for line in out.stdout.splitlines() if line.startswith("LOG ")]
    # Written a few seconds after the trap (on deck), not only when the mission ends.
    assert logs.index("LOG mission ends") > next(i for i, line in enumerate(logs) if "written" in line)
    return files[0], chained


def test_recorder_writes_the_approach(recorded):
    path, chained = recorded
    assert chained > 100  # the export function defined before ours still ran every frame
    lines = path.read_text().splitlines()
    assert lines[0] == "# dcs-lso pilot hook 1" and "# aircraft=FA-18C_hornet" in lines and "# pilot=Wrycu" in lines
    header, carrier = lines.index("t,x,y,z,heading,pitch,bank,aoa,lat,lon"), lines.index("## carrier")
    assert "# carrier_type=CVN_75" in lines and "# carrier_unit=CVN-75 Harry S. Truman" in lines
    carrier_csv = [line for line in lines[carrier:] if not line.startswith("#")]
    body = {"pilot": "Wrycu", "aircraft": "FA-18C_hornet", "mission": MISSION, "csv": "\n".join(lines[header:carrier]),
            "carrier": {"type": "CVN_75", "unit": "CVN-75 Harry S. Truman", "csv": "\n".join(carrier_csv)}}
    upload = parse_upload(body)
    assert len(upload.rows) > 300 and upload.rows[0]["aoa"] > 0
    assert upload.carrier is not None and len(upload.carrier.rows) > 100  # about 10 Hz


UPLOADER_STUBS = r"""
WRITEDIR, SEND_TO_ALL, REFUSE_HOST = ...
SEND_TO_ALL = SEND_TO_ALL == 'true'
local clock = 1000
local options = { sendToAll = SEND_TO_ALL, hub1Url = 'http://hub1:8000', hub1Token = '',
                  hub2Url = 'https://hub2.example.com/lso/', hub2Token = ' tok2 ' }
package.preload['optionsEditor'] = function() return { getOption = function(k) return options[k:gsub('^plugins%.DCS%-LSO%.', '')] end } end
package.preload['lfs'] = function()
  return { writedir = function() return WRITEDIR end, mkdir = function(p) os.execute('mkdir -p "' .. p .. '"') end,
           dir = function(p)
             local names = {}
             local h = io.popen('ls -1 "' .. p .. '"')
             for name in h:lines() do names[#names + 1] = name end
             h:close()
             local k = 0
             return function() k = k + 1 return names[k] end
           end }
end
local function conn_for()
  local c = { sent = '' }
  function c:settimeout() end
  function c:connect(ip, port) self.port = port return nil, 'timeout' end
  function c:send(data, i) self.sent = self.sent .. data:sub(i) return #data end
  function c:receive()
    io.write('<<<REQUEST\n', self.sent, '\nREQUEST>>>\n')
    local host = self.sent:match('Host: ([^\r]+)')
    if self.sent:match('^GET') then
      local here = host == 'hub1'
      return nil, 'closed', 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"here": ' .. tostring(here) .. '}'
    end
    if host == REFUSE_HOST then return nil, 'closed', 'HTTP/1.1 401 Unauthorized\r\n\r\n{"detail": "not a pilot token"}' end
    return nil, 'closed', 'HTTP/1.1 200 OK\r\n\r\n{"reports": [{"text": "waiting"}]}'
  end
  function c:close() end
  return c
end
package.preload['socket'] = function()
  return { gettime = function() return clock end, tcp = conn_for, dns = { toip = function() return '127.0.0.1' end },
           select = function(r, w) return {}, w end }
end
log = { INFO = 'INFO', write = function(src, lvl, msg) print('LOG ' .. msg) end }
local callbacks
package.preload['terrain'] = function() return { GetTerrainConfig = function(k) if k == 'SummerTimeDelta' then return 0 end end } end
DCS = { setUserCallbacks = function(c) callbacks = c end, getMissionName = function() return 'MISSION' end,
        isMultiplayer = function() return true end,
        getCurrentMission = function() return { mission = { date = { Year = 2016, Month = 6, Day = 21 }, start_time = 28800 } } end,
        getSunAzimuthElevation = function(lat, lon, y, m, d, seconds)
          print(string.format('SUN %.4f %.4f %d-%02d-%02d %.0f', lat, lon, y, m, d, seconds))
          return 95.0, -12.5
        end }
net = { get_my_player_id = function() return 2 end,
        get_player_info = function() return { ucid = 'UCID', name = 'Wrycu', ipaddr = 'HOME' } end,
        get_server_host = function() return '192.168.1.238:10308' end }
dofile(UPLOADER_PATH)
callbacks.onSimulationStart()
for n = 1, 400 do clock = clock + 0.1; callbacks.onSimulationFrame() end
"""


def run_uploader(tmp_path: Path, approach: Path, send_to_all: bool,
                 previous_session: str | None = None, refuse: str = "") -> tuple[list[str], Path]:
    out_dir = tmp_path / "Logs" / "dcs-lso"
    out_dir.mkdir(parents=True)
    # Written during this session (the uploader stamps it with the session's mission, account and server)...
    shutil.copy(approach, out_dir / approach.name)
    if previous_session is not None:
        # ...or as the previous session ended (a file from before this session started).
        (out_dir / "session.txt").write_text(previous_session)
        text = (out_dir / approach.name).read_text()
        (out_dir / approach.name).write_text(re_written.sub("# written_at=1000", text))
    stubs = UPLOADER_STUBS.replace("'MISSION'", repr(MISSION)).replace("'UCID'", repr(UCID)).replace("'HOME'", repr(HOME))
    script = f"UPLOADER_PATH = {str(UPLOADER)!r}\n" + stubs
    out = subprocess.run([luajit(), "-", f"{tmp_path}/", "true" if send_to_all else "false", refuse], input=script.encode(),
                         capture_output=True, check=True).stdout.decode()  # bytes: keep HTTP's \r\n
    requests = [part.split("\nREQUEST>>>")[0] for part in out.split("<<<REQUEST\n")[1:]]
    return requests, out_dir


import re  # noqa: E402

re_written = re.compile(r"# written_at=\d+")


def test_a_file_from_the_last_session_is_stamped_with_it(tmp_path, recorded):
    previous = f"mission=the previous mission\nucid={UCID}\nname=Wrycu\nserver=10.0.0.1:10308\nstarted=900\n"
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=False, previous_session=previous)
    (_, _, body), = [split(r) for r in requests if r.startswith("POST")]
    assert (json.loads(body)["mission"], json.loads(body)["server"]) == ("the previous mission", "10.0.0.1:10308")
    # This session is saved for next time.
    assert f"mission={MISSION}" in (tmp_path / "Logs" / "dcs-lso" / "session.txt").read_text()


def split(request: str) -> tuple[str, dict, str]:
    head, _, body = request.partition("\r\n\r\n")
    first, *headers = head.split("\r\n")
    return first, dict(h.split(": ", 1) for h in headers), body


def test_uploader_sends_to_the_current_servers_hub(tmp_path, recorded):
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=False)
    gets = [split(r) for r in requests if r.startswith("GET")]
    posts = [split(r) for r in requests if r.startswith("POST")]
    # Both hubs asked whether we're on one of their servers; only hub1 says yes, so only it gets the trap.
    assert {(h["Host"], first.split()[1]) for first, h, _ in gets} == {
        ("hub1", f"/api/v1/pilot-hook/here?ucid={UCID}"), ("hub2.example.com", f"/lso/api/v1/pilot-hook/here?ucid={UCID}")}
    (first, headers, body), = posts
    assert first == "POST /api/v1/pilot-hook/approaches HTTP/1.1" and headers["Host"] == "hub1"
    assert "Authorization" not in headers and int(headers["Content-Length"]) == len(body.encode())
    sent = json.loads(body)
    assert (sent["mission"], sent["ucid"], sent["pilot"], sent["server"]) == (MISSION, UCID, "Wrycu", "192.168.1.238:10308")
    assert list((out_dir / "sent").glob("approach-*.csv")) and not list(out_dir.glob("approach-*.csv"))


def test_uploader_sends_dcss_own_sun(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=False)
    (_, _, body), = [split(r) for r in requests if r.startswith("POST")]
    assert json.loads(body)["sun_elevation"] == -12.5


def test_a_refused_token_is_tried_again_next_mission(tmp_path, recorded):
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=True, refuse="hub2.example.com")
    to_hub2 = [r for r in requests if r.startswith("POST") and "Host: hub2.example.com" in r]
    assert len(to_hub2) == 1  # not hammered for the rest of the mission
    # Kept (not moved to sent/), so it goes again once the token is fixed and a new mission starts.
    assert list(out_dir.glob("approach-*.csv")) and not list((out_dir / "sent").glob("*.csv"))


def test_uploader_sends_to_all_hubs_with_their_tokens(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=True)
    posts = [split(r) for r in requests if r.startswith("POST")]
    assert sorted((h["Host"], h.get("Authorization")) for _, h, _ in posts) == [
        ("hub1", None), ("hub2.example.com", "Bearer tok2")]


def test_the_hub_takes_what_the_pilot_hook_sent(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path / "game", recorded[0], send_to_all=False)
    (_, _, body), = [split(r) for r in requests if r.startswith("POST")]
    hub = make_hub(tmp_path / "hub")
    hub.ingest(1, *server_report())
    client = TestClient(create_app(hub), client=(HOME, 50000))  # from the pilot's own address: no token
    r = client.post("/api/v1/pilot-hook/approaches", content=body)
    assert r.status_code == 200, r.text
    (report,) = r.json()["reports"]
    with hub.sessions() as s:
        row = s.get(Pass, report["pass_id"])
        landing = hub.load_pass(s.get(Pass, row.merged_into_id or row.id))
    # Graded from the pilot hook's own track (recorded AOA, 50 Hz), merged with the server's report.
    assert landing.track_source == "pilot hooks" and landing.wire_estimate == 2


def test_another_communitys_hub_grades_it_on_its_own(tmp_path, recorded):
    """'Send to all': a hub with no server agent of its own (another community's), with the pilot's token."""
    requests, _ = run_uploader(tmp_path / "game", recorded[0], send_to_all=True)
    (_, headers, body), = [split(r) for r in requests if r.startswith("POST") and "hub2" in r]
    assert json.loads(body)["carrier"]["type"] == "CVN_75"
    (tmp_path / "hub").mkdir()
    hub = Hub(f"sqlite:///{tmp_path / 'hub' / 'lso.db'}", tmp_path / "hub", pilot_hook_accept="any")
    token = hub.add_pilot_token("Wrycu", "pilot hook")
    client = TestClient(create_app(hub), client=("8.8.4.4", 50000))
    r = client.post("/api/v1/pilot-hook/approaches", content=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    (report,) = r.json()["reports"]
    assert report["grade"]  # graded on its own: the pilot hook sent the carrier
    with hub.sessions() as s:
        landing = hub.load_pass(s.get(Pass, report["pass_id"]))
    assert landing.outcome.value == "trap" and landing.wire_estimate == 2
