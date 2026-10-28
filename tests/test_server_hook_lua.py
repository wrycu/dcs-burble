"""The server hook's mission-side Lua (the handler it puts into the mission), run under LuaJIT with stubbed DCS
APIs: the wind and turbulence it logs at each carrier, and the mission's weather."""

import json
import re
import subprocess
from pathlib import Path

from burble.agent.service import wind_profile
from test_pilot_hook_lua import luajit

HOOK = Path(__file__).parents[1] / "hooks" / "burble-server-hook.lua"

STUBS = r"""
env = { info = function(s) print(s) end, mission = { weather = {
  atmosphere_type = 0, groundTurbulence = 12, season = { temperature = 26 }, qnh = 755,
  visibility = { distance = 80000 }, enable_fog = false, fog = { visibility = 0, thickness = 0 },
  clouds = { preset = "Preset6", base = 2500, thickness = 1150, density = 5, iprecptns = 0 } } } }
local scheduled = {}
timer = { getTime = function() return 100 end,
          scheduleFunction = function(f, arg, at) scheduled[#scheduled + 1] = { f, arg } end }
world = { addEventHandler = function() end, event = {} }
coalition = { side = { NEUTRAL = 0, RED = 1, BLUE = 2 },
              getGroups = function(side) if side ~= 2 then return {} end return { GROUP } end }
Group = { Category = { SHIP = 3 } }
local carrier = { getTypeName = function() return "CVN_75" end, getName = function() return "CVN-75" end,
                  getPoint = function() return { x = 1000, y = 0, z = 2000 } end,
                  getPosition = function() return { x = { x = 1, y = 0, z = 0 } } end }
GROUP = { getUnits = function() return { carrier } end }
-- 10 m/s blowing to the east (from the west); gusts of +-2 m/s along the glide path.
local n = 0
atmosphere = {
  getWind = function(p) return { x = 0, y = 0, z = 10 } end,
  getWindWithTurbulence = function(p) n = n + 1 return { x = 0, y = 0, z = 10 + ((n % 2 == 0) and 2 or -2) } end }
"""


def run_handler(stubs: str = STUBS) -> list[dict]:
    text = HOOK.read_text(encoding="utf-8")
    handler = text.split("local HANDLER = [==[", 1)[1].split("]==]", 1)[0]
    script = stubs + handler + "\nfor _, s in ipairs(scheduled) do s[1](s[2], 100) end\n"
    out = subprocess.run([luajit(), "-"], input=script, capture_output=True, text=True, check=True).stdout
    return [json.loads(m) for m in re.findall(r"^BURBLE (\{.*\})$", out, re.M)]


def test_wind_turbulence_and_weather_events():
    events = {e["event"]: e for e in run_handler()}
    wind = events["wind"]
    assert wind["carrier"] == "CVN-75" and abs(wind["turbulence"] - 2.0) < 1e-9
    profile = wind_profile(wind)
    assert profile.at(50) == (10.0, 0.0) and abs(profile.turbulence - 2.0) < 1e-9
    weather = events["weather"]
    assert weather["ground_turbulence"] == 12 and weather["clouds"]["base_m"] == 2500
    assert weather["visibility_m"] == 80000 and "fog" not in weather and weather["dynamic"] is False


def test_the_hook_says_why_turbulence_or_weather_are_missing():
    stubs = (STUBS.replace("env = { info = function(s) print(s) end, mission =", "env = { info = function(s) print(s) end, _m =")
             .replace("  getWindWithTurbulence = function(p)", "  _gusty = function(p)"))
    events = {e["event"]: e for e in run_handler(stubs)}
    assert "turbulence" not in events["wind"]
    assert events["wind"]["turbulence_error"] == "no atmosphere.getWindWithTurbulence"
    assert events["weather"]["error"] == "no env.mission"


GEAR_STUBS = STUBS.replace("Group = { Category = { SHIP = 3 } }", "Group = { Category = { AIRPLANE = 0, SHIP = 3 } }") \
    .replace("getGroups = function(side) if side ~= 2 then return {} end return { GROUP } end",
             "getGroups = function(side, cat) if side ~= 2 then return {} end "
             "if cat == 0 then return { PLANES } end return { GROUP } end") + r"""
ARGS = { [0] = 0.0, [3] = 0.0, [5] = 0.0, [25] = 0.0 }
JET_AT = { x = 1000 - 2000, y = 150, z = 2000 }  -- 2 km astern of the carrier
local jet = { id_ = 16777728, getName = function() return "Aerial-5-1" end,
              getPlayerName = function() return "Wrycu" end, getPoint = function() return JET_AT end,
              getDrawArgumentValue = function(self, arg) return ARGS[arg] end }
local tanker = { id_ = 16777729, getName = function() return "TANKER_1-2-1" end, getPlayerName = function() return nil end,
                 getPoint = function() return { x = 1000, y = 7000, z = 2000 } end,
                 getDrawArgumentValue = function(self, arg) error("no such argument") end }
local far = { id_ = 16777730, getName = function() return "DRONE-1-1" end, getPlayerName = function() return nil end,
              getPoint = function() return { x = 1000 + 30000, y = 9000, z = 2000 } end,  -- 16 nm away
              getDrawArgumentValue = function(self, arg) return 0 end }
PLANES = { getUnits = function() return { jet, tanker, far } end }
"""


def test_the_hook_logs_gear_and_hook_as_they_move():
    """Each airplane near a carrier: its gear (the least lowered leg) and hook, logged when either moves by 0.1 or
    more, and afresh when it comes back within 2 nm."""
    text = HOOK.read_text(encoding="utf-8")
    handler = text.split("local HANDLER = [==[", 1)[1].split("]==]", 1)[0]
    away, back = "JET_AT = { x = 1000 - 4000, y = 300, z = 2000 }", "JET_AT = { x = 1000 - 2000, y = 150, z = 2000 }"
    steps = [[], ["ARGS[25] = 0.05"], ["ARGS[25] = 0.41"], ["ARGS[0] = 0.08", "ARGS[3] = 0.37", "ARGS[5] = 0.37"],
             ["ARGS[0] = 0.39", "ARGS[3] = 1.0", "ARGS[5] = 1.0"], [], [away], [back]]
    run = "".join("".join(line + "\n" for line in step) + "for _, s in ipairs(scheduled) do s[1](s[2], 100) end\n"
                  for step in steps)
    out = subprocess.run([luajit(), "-"], input=GEAR_STUBS + handler + "\n" + run, capture_output=True, text=True,
                         check=True).stdout
    gear = [json.loads(m) for m in re.findall(r"^BURBLE (\{.*\})$", out, re.M) if '"event":"gear"' in m]
    # (The nose at 0.08 is under a 0.1 move; the mains lead it, the nose is the least lowered leg.)
    assert [(round(e["gear"], 2), round(e["hook"], 2)) for e in gear] == [(0.0, 0.0), (0.0, 0.41), (0.39, 0.41),
                                                                          (0.39, 0.41)]  # the last: back in range
    # Not the tanker (no such draw args) nor the drone 16 nm away; the jet at 4 km (2.2 nm) is out of range.
    assert {(e["object_id"], e["player"], e["name"]) for e in gear} == {(16777728, "Wrycu", "Aerial-5-1")}
