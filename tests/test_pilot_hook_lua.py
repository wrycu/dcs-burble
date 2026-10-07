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
    assert lines[0] == "# dcs-lso pilot hook 2" and "# aircraft=FA-18C_hornet" in lines and "# pilot=Wrycu" in lines
    header, carrier = lines.index("t,x,y,z,heading,pitch,bank,aoa,lat,lon"), lines.index("## carrier")
    assert "# carrier_type=CVN_75" in lines and "# carrier_unit=CVN-75 Harry S. Truman" in lines
    carrier_csv = [line for line in lines[carrier:] if not line.startswith("#")]
    body = {"pilot": "Wrycu", "aircraft": "FA-18C_hornet", "mission": MISSION, "csv": "\n".join(lines[header:carrier]),
            "carrier": {"type": "CVN_75", "unit": "CVN-75 Harry S. Truman", "csv": "\n".join(carrier_csv)}}
    upload = parse_upload(body)
    assert len(upload.rows) > 300 and upload.rows[0]["aoa"] > 0
    assert upload.carrier is not None and len(upload.carrier.rows) > 100  # about 10 Hz


UPLOADER_STUBS = r"""
WRITEDIR, SEND_TO_ALL, REFUSE_HOST, FLAGS = ...
if SEND_TO_ALL == 'unset' then SEND_TO_ALL = nil else SEND_TO_ALL = SEND_TO_ALL == 'true' end
FLAGS = FLAGS or ''
local clock = 1000
local options = { sendToAll = SEND_TO_ALL, hub1Url = 'http://hub1:8000', hub1Token = '',
                  hub2Url = 'https://hub2.example.com/lso/', hub2Token = ' tok2 ' }
package.preload['optionsEditor'] = function() return { getOption = function(k) return options[k:gsub('^plugins%.DCS%-LSO%.', '')] end } end
package.preload['lfs'] = function()
  return { writedir = function() return WRITEDIR end, mkdir = function(p) os.execute('mkdir -p "' .. p .. '"') end,
           attributes = function(p)
             local f = io.open(p); if not f then return nil end; f:close()
             local h = io.popen('stat -c %Y "' .. p .. '"'); local m = tonumber(h:read('*l')); h:close()
             return { modification = m }
           end,
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
    if self.sent:match('^GET [^ ]*/calls%?') then
      return nil, 'closed', 'HTTP/1.1 200 OK\r\n\r\n{"ready": true, "calls": [{"time": 1105.0, "along": 120.0, "call": "power"}]}'
    end
    if self.sent:match('^GET') then
      if host == 'hub1' and FLAGS:find('herefail') then return nil, 'closed', '' end  -- down
      local here = host == 'hub1' and not FLAGS:find('elsewhere')
      return nil, 'closed', 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"here": ' .. tostring(here) .. '}'
    end
    if host == REFUSE_HOST and FLAGS:find('ours') then
      return nil, 'closed', 'HTTP/1.1 403 Forbidden\r\n\r\n{"detail": "this hub only accepts traps flown on its own servers"}'
    end
    if host == REFUSE_HOST then return nil, 'closed', 'HTTP/1.1 401 Unauthorized\r\n\r\n{"detail": "not a pilot token"}' end
    return nil, 'closed', 'HTTP/1.1 200 OK\r\n\r\n{"reports": [{"pass_id": 7, "text": "waiting"}], '
      .. '"pilot_hook": {"latest": 99, "update": true}}'
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
local menu_tick
if WITH_UPDATE_MANAGER then
  package.preload['UpdateManager'] = function() return { add = function(f) menu_tick = f end, delete = function() end } end
end
dofile(UPLOADER_PATH)
DRIVER
"""


IN_MISSION = """
callbacks.onSimulationStart()
for n = 1, 400 do clock = clock + 0.1; callbacks.onSimulationFrame() end
"""


def run_uploader(tmp_path: Path, approach: Path, send_to_all: bool | None,
                 previous_session: str | None = None, refuse: str = "", driver: str = IN_MISSION,
                 update_manager: bool = False, flags: str = "") -> tuple[list[str], Path]:
    """`send_to_all`: None as if never set. `flags`: "elsewhere" (on no hub's server), "ours" (`refuse` answers
    403: it only takes traps from its own servers)."""
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
    stubs = stubs.replace("DRIVER", driver)
    script = f"UPLOADER_PATH = {str(UPLOADER)!r}\nWITH_UPDATE_MANAGER = {'true' if update_manager else 'false'}\n" + stubs
    setting = "unset" if send_to_all is None else "true" if send_to_all else "false"
    out = subprocess.run([luajit(), "-", f"{tmp_path}/", setting, refuse, flags], input=script.encode(),
                         capture_output=True, check=True).stdout.decode()  # bytes: keep HTTP's \r\n
    requests = [part.split("\nREQUEST>>>")[0] for part in out.split("<<<REQUEST\n")[1:]]
    run_uploader.log = [line[4:] for line in out.splitlines() if line.startswith("LOG ")]
    return requests, out_dir


import re  # noqa: E402

re_written = re.compile(r"# written_at=\d+")


def test_a_file_from_the_last_session_is_stamped_with_it(tmp_path, recorded):
    previous = f"mission=the previous mission\nucid={UCID}\nname=Wrycu\nserver=10.0.0.1:10308\nstarted=900\n"
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=False, previous_session=previous)
    posts = {h["Host"]: (h, json.loads(body)) for _, h, body in (split(r) for r in requests if r.startswith("POST"))}
    _, body = posts["hub1"]
    assert (body["mission"], body["server"]) == ("the previous mission", "10.0.0.1:10308")
    # That session (an older pilot hook) didn't record which hubs answered: hub2 may have been its server's
    # hub, so it gets the file too, without the token (it decides by whether it knows us from its servers).
    headers, body = posts["hub2.example.com"]
    assert "Authorization" not in headers and "here" not in body
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


def test_uploader_logs_once_that_a_newer_version_is_out(tmp_path, recorded):
    run_uploader(tmp_path, recorded[0], send_to_all=True)
    sent = [line for line in run_uploader.log if "->" in line]
    updates = [line for line in run_uploader.log if "newer pilot hook" in line]
    assert len(sent) == 2 and updates == ["a newer pilot hook is available (version 99, this is 4): see http://hub1:8000"]


def test_uploader_sends_dcss_own_sun(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=False)
    (_, _, body), = [split(r) for r in requests if r.startswith("POST")]
    assert json.loads(body)["sun_elevation"] == -12.5


def test_a_hub_that_was_down_while_flying_still_gets_the_approach(tmp_path, recorded):
    # hub1 (no token) doesn't answer "are you on our server?" (down); later it's up again. Not "send to all".
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=False, flags="herefail")
    (headers, body), = [(h, json.loads(b)) for _, h, b in (split(r) for r in requests if r.startswith("POST"))
                        if h["Host"] == "hub1"]
    assert "Authorization" not in headers and "here" not in body  # unknown: the hub decides
    assert list((out_dir / "sent").glob("approach-*.csv"))


def test_a_hub_that_was_down_and_doesnt_know_us_is_left_alone(tmp_path, recorded):
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=False, refuse="hub1", flags="herefail")
    assert len([r for r in requests if r.startswith("POST") and "Host: hub1" in r]) == 1  # asked once, not again
    assert any("not taken (not flown on its servers" in line for line in run_uploader.log)


def test_a_refusing_servers_hub_doesnt_hold_up_the_others(tmp_path, recorded):
    # hub1 is the server's hub but refuses (401, with no token: e.g. it no longer recognises the player); hub2
    # (with its token) still gets the approach, instead of waiting for hub1 forever.
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=True, refuse="hub1")
    assert {split(r)[1]["Host"] for r in requests if r.startswith("POST")} == {"hub1", "hub2.example.com"}


def test_send_to_all_is_on_unless_switched_off(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=None)
    assert {split(r)[1]["Host"] for r in requests if r.startswith("POST")} == {"hub1", "hub2.example.com"}


def test_on_another_server_only_hubs_with_a_token_are_sent_to(tmp_path, recorded):
    # Flying on a server no hub knows: hub1 (no token) would refuse, so only hub2 (token) gets it.
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=True, flags="elsewhere")
    assert [split(r)[1]["Host"] for r in requests if r.startswith("POST")] == ["hub2.example.com"]
    assert list((out_dir / "sent").glob("approach-*.csv"))  # done: nothing left waiting for hub1


def test_a_hub_taking_only_its_own_servers_traps_is_not_asked_again(tmp_path, recorded):
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=True, refuse="hub2.example.com", flags="elsewhere ours")
    assert len([r for r in requests if r.startswith("POST")]) == 1
    assert any("not taken" in line for line in run_uploader.log) and not list(out_dir.glob("approach-*.csv"))


def test_a_refused_token_is_tried_again_next_mission(tmp_path, recorded):
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=True, refuse="hub2.example.com")
    to_hub2 = [r for r in requests if r.startswith("POST") and "Host: hub2.example.com" in r]
    assert len(to_hub2) == 1  # not hammered for the rest of the mission
    # Kept (not moved to sent/), so it goes again once the token is fixed and a new mission starts.
    assert list(out_dir.glob("approach-*.csv")) and not list((out_dir / "sent").glob("*.csv"))


def test_calls_are_relayed_from_the_servers_hub(tmp_path, recorded):
    requests, _ = run_uploader(tmp_path, recorded[0], send_to_all=True)
    firsts = [r.split("\r\n")[0] + " " + r.split("Host: ")[1].split("\r\n")[0] for r in requests if "/pilot-hook/here" not in r.split("\r\n")[0]]
    # The server's hub (hub1) first, then its calls, then the other hub with them.
    assert firsts == ["POST /api/v1/pilot-hook/approaches HTTP/1.1 hub1",
                      f"GET /api/v1/pilot-hook/calls?pass_id=7&ucid={UCID} HTTP/1.1 hub1",
                      "POST /lso/api/v1/pilot-hook/approaches HTTP/1.1 hub2.example.com"]
    bodies = {split(r)[1]["Host"]: json.loads(split(r)[2]) for r in requests if r.startswith("POST")}
    assert bodies["hub2.example.com"]["calls"] == [{"time": 1105.0, "along": 120.0, "call": "power"}]
    assert bodies["hub2.example.com"]["calls_from"] == "hub1" and "calls" not in bodies["hub1"]
    # Each hub is told whether it was the server's hub when the approach was flown.
    assert (bodies["hub1"]["here"], bodies["hub2.example.com"]["here"]) == (True, False)


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


def test_sent_from_the_menus_after_the_mission(tmp_path, recorded):
    """DCS's UpdateManager keeps the uploader going in the menus: an approach left when the mission ended (e.g.
    written as the pilot quit) is sent straight away, not next session."""
    driver = """
local d = WRITEDIR .. 'Logs/dcs-lso/'
local name = io.popen('ls -1 "' .. d .. '" | grep approach'):read('*l')
os.rename(d .. name, WRITEDIR .. name)  -- not written yet
callbacks.onSimulationStart()
for n = 1, 100 do clock = clock + 0.1; callbacks.onSimulationFrame() end  -- in the mission: the hubs are asked
callbacks.onSimulationStop()
os.rename(WRITEDIR .. name, d .. name)  -- the recorder's last flush, as the mission ends
assert(menu_tick, 'registered with UpdateManager')
for n = 1, 300 do clock = clock + 0.1; menu_tick() end
"""
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=False, driver=driver, update_manager=True)
    (post,) = [split(r) for r in requests if r.startswith("POST")]
    assert json.loads(post[2])["mission"] == MISSION  # stamped with the mission just left
    assert list((out_dir / "sent").glob("approach-*.csv"))


def test_dcs_grade_from_debrief_is_added_and_sent_again(tmp_path, recorded):
    lines = recorded[0].read_text().splitlines()
    header, carrier = lines.index("t,x,y,z,heading,pitch,bank,aoa,lat,lon"), lines.index("## carrier")
    times = [float(line.split(",")[0]) for line in lines[header + 1:carrier]]
    mark_t = (times[0] + times[-1]) / 2  # sometime during the approach
    debrief = (f'events = {{ [1] = {{ type = "landing quality mark", t = {mark_t}, initiatorPilotName = "Wrycu", '
               f'comment = "LSO: GRADE:OK : (LOAR)  WIRE# 2" }}, [2] = {{ type = "landing quality mark", t = {mark_t}, '
               f'initiatorPilotName = "Someone else", comment = "LSO: GRADE:C : WIRE# 4" }} }}')
    driver = f"""
callbacks.onSimulationStart()
for n = 1, 200 do clock = clock + 0.1; callbacks.onSimulationFrame() end  -- sent during the mission
callbacks.onSimulationStop()
local f = io.open(WRITEDIR .. 'Logs/debrief.log', 'w'); f:write({debrief!r}); f:close()  -- DCS writes it now
for n = 1, 300 do clock = clock + 0.1; menu_tick() end
"""
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=False, driver=driver, update_manager=True)
    posts = [json.loads(split(r)[2]) for r in requests if r.startswith("POST")]
    assert [p.get("dcs_grade") for p in posts] == [None, "LSO: GRADE:OK : (LOAR)  WIRE# 2"]  # the pilot's own
    assert list((out_dir / "sent").glob("approach-*.csv"))


def test_send_now_tries_again(tmp_path, recorded):
    driver = """
callbacks.onSimulationStart()
for n = 1, 200 do clock = clock + 0.1; callbacks.onSimulationFrame() end
local queued = DCSLSO_PILOT.status()
print('QUEUED ' .. queued)
local f = io.open(WRITEDIR .. 'Logs/dcs-lso/retry.flag', 'w'); f:write('retry'); f:close()  -- "Send now"
for n = 1, 200 do clock = clock + 0.1; callbacks.onSimulationFrame() end
"""
    requests, out_dir = run_uploader(tmp_path, recorded[0], send_to_all=True, refuse="hub2.example.com", driver=driver)
    to_hub2 = [r for r in requests if r.startswith("POST") and "Host: hub2.example.com" in r]
    assert len(to_hub2) == 2  # refused once, tried again after "Send now"
    assert (out_dir / "status.txt").read_text().startswith("queued=1")
    assert not (out_dir / "retry.flag").exists()


OPTIONS = ROOT / "Mods" / "Services" / "DCS-LSO" / "Options" / "optionsDb.lua"
OPTIONS_STUBS = r"""
WRITEDIR, SHARED = ...
local chain = setmetatable({}, { __index = function(t, k) return function(self) return self end end })
package.preload['Options.DbOption'] = function() return { new = function() return chain end } end
package.preload['lfs'] = function()
  return { writedir = function() return WRITEDIR end,
           dir = function(p) local h = io.popen('ls -1 "' .. p .. '"'); local names = {}
                             for n in h:lines() do names[#names + 1] = n end; h:close()
                             local k = 0; return function() k = k + 1; return names[k] end end }
end
package.preload['UpdateManager'] = function() return { add = function() end, delete = function() end } end
if SHARED == 'yes' then
  DCSLSO_PILOT = { status = function() return 3, os.time() - 600 end, retry = function() print('RETRIED') end }
end
local db = dofile(OPTIONS_PATH)
local label = { setText = function(self, text) print('LABEL ' .. text) end }
local button = {}
db.callbackOnShowDialog({ queueLabel = label, sendNowButton = button })
button:onChange()
db.callbackOnClose()
"""


@pytest.mark.parametrize("shared", [True, False])
def test_settings_page_shows_the_queue_and_sends_now(tmp_path, recorded, shared):
    out_dir = tmp_path / "Logs" / "dcs-lso"
    out_dir.mkdir(parents=True)
    shutil.copy(recorded[0], out_dir / recorded[0].name)
    script = f"OPTIONS_PATH = {str(OPTIONS)!r}\n" + OPTIONS_STUBS
    out = subprocess.run([luajit(), "-", f"{tmp_path}/", "yes" if shared else "no"], input=script, capture_output=True,
                         text=True, check=True).stdout
    labels = [line for line in out.splitlines() if line.startswith("LABEL")]
    if shared:  # from the uploader itself
        assert labels[0] == "LABEL 3 approaches waiting to send (oldest: 10 min ago)." and "RETRIED" in out
    else:  # counted from the folder; "Send now" leaves a flag for the uploader
        assert labels[0].startswith("LABEL 1 approach waiting to send") and (out_dir / "retry.flag").exists()
