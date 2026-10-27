"""The carriers' landing lights for Case III (PLAN #54): the agent deciding which carrier wants them (Case III there and
an aircraft recovering to it) and writing the hook's file, and the server hook setting them, run under LuaJIT with
stubbed DCS APIs."""

import json
import re
import subprocess
from pathlib import Path

from burble import agentconfig as ac
from burble.acmi import ObjectTrack, Sample
from burble.acmi.parser import Transform
from burble.agent.callouts import recovery_case_iii
from burble.agent.lights import DEFAULT_HOLD_S, DEFAULT_RADIUS_NM, NM, CarrierLights, lights_settings
from test_pilot_hook_lua import luajit

HOOK = Path(__file__).parents[1] / "hooks" / "burble-server-hook.lua"
FT = 0.3048


def carrier(object_id=1, name="CVN-75 Harry S. Truman", u=0.0, v=0.0, heading=0.0) -> ObjectTrack:
    return ObjectTrack(object_id, {"Name": "CVN_75", "Pilot": name, "Type": "Sea+Watercraft+AircraftCarrier"},
                       [Sample(0.0, Transform(u=u, v=v, alt=0.0, heading=heading), None)])


def jet(object_id=10) -> ObjectTrack:
    return ObjectTrack(object_id, {"Name": "FA-18C_hornet", "Pilot": "Wrycu", "Type": "Air+FixedWing"})


def fly(lights: CarrierLights, plane: ObjectTrack, t: float, u: float, v: float, alt_ft: float, heading: float) -> None:
    plane.samples.append(Sample(t, Transform(u=u, v=v, alt=alt_ft * FT, heading=heading), None))
    lights.update(plane)


def tick(lights: CarrierLights, ship: ObjectTrack, t: float) -> None:
    """The carrier moves on (the stream's time goes on)."""
    ship.samples.append(Sample(t, ship.samples[-1].transform, None))
    lights.update(ship)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path, case_iii=lambda carrier, t: True, **kw):
    clock = Clock()
    path = tmp_path / "Logs" / "burble-lights.json"
    return CarrierLights(path, case_iii, clock=clock, **kw), path, clock


def written(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["carriers"]


# -- the agent ------------------------------------------------------------------------------------------------------

def test_lit_while_an_aircraft_recovers_in_case_iii(tmp_path):
    lights, path, clock = make(tmp_path)
    ship, plane = carrier(), jet()
    lights.update(ship)
    lights.write()
    assert written(path) == {}  # written from the start (so a stale file from before is replaced)
    fly(lights, plane, 10.0, 0.0, -5 * NM, 1200, 0.0)  # 5 nm astern, 1,200 ft, heading for the ship
    lights.write()
    assert written(path) == {"CVN-75 Harry S. Truman": "recovery"}
    # Still on for the hold after the last time it was seen recovering (a bolter, the next one in), then off.
    tick(lights, ship, 10.0 + DEFAULT_HOLD_S)
    lights.write()
    assert written(path) == {"CVN-75 Harry S. Truman": "recovery"}
    tick(lights, ship, 11.0 + DEFAULT_HOLD_S)
    lights.write()
    assert written(path) == {}
    lights.close()
    assert written(path) == {}


def test_only_when_recovering(tmp_path):
    cases = {
        "ahead of the ship": (0.0, 3 * NM, 1200, 0.0),
        "astern, flying away": (0.0, -5 * NM, 1200, 180.0),
        "too far": (0.0, -12 * NM, 1200, 0.0),
        "too high": (0.0, -8 * NM, 6000, 0.0),
        "on deck": (0.0, -50.0, 20, 0.0),
        "abeam": (3 * NM, 0.0, 1200, 270.0),
    }
    for why, (u, v, alt, heading) in cases.items():
        lights, path, _ = make(tmp_path)
        ship = carrier()
        lights.update(ship)
        fly(lights, jet(), 10.0, u, v, alt, heading)
        lights.write()
        assert written(path) == {}, why


def test_not_unless_case_iii(tmp_path):
    lights, path, _ = make(tmp_path, case_iii=lambda carrier, t: False)
    lights.update(carrier())
    fly(lights, jet(), 10.0, 0.0, -5 * NM, 1200, 0.0)
    lights.write()
    assert written(path) == {}


def test_only_the_carrier_it_is_recovering_to(tmp_path):
    lights, path, _ = make(tmp_path)
    lights.update(carrier(1, "CVN-75 Harry S. Truman"))
    lights.update(carrier(2, "CVN-71 Theodore Roosevelt", u=30 * NM))
    fly(lights, jet(), 10.0, 30 * NM, -4 * NM, 1500, 0.0)  # astern of the Roosevelt
    lights.write()
    assert written(path) == {"CVN-71 Theodore Roosevelt": "recovery"}


def test_the_distance_and_hold_are_configurable(tmp_path):
    lights, path, _ = make(tmp_path, radius_m=3 * NM, hold_s=10.0)
    ship = carrier()
    lights.update(ship)
    fly(lights, jet(), 10.0, 0.0, -5 * NM, 1200, 0.0)
    lights.write()
    assert written(path) == {}  # outside 3 nm
    fly(lights, jet(11), 20.0, 0.0, -2 * NM, 600, 0.0)
    lights.write()
    assert written(path) == {"CVN-75 Harry S. Truman": "recovery"}
    tick(lights, ship, 31.0)
    lights.write()
    assert written(path) == {}


def test_rewritten_every_few_seconds(tmp_path):
    lights, path, clock = make(tmp_path)
    lights.update(carrier())
    fly(lights, jet(), 10.0, 0.0, -5 * NM, 1200, 0.0)
    lights.write()
    first = json.loads(path.read_text())["at"]
    clock.now += 2
    lights.write()
    assert json.loads(path.read_text())["at"] == first  # nothing changed
    clock.now += 4
    lights.write()
    assert json.loads(path.read_text())["at"] == first + 6  # the heartbeat: the hook takes an old file as stopped


def test_settings_and_their_defaults():
    assert lights_settings({}) == (True, DEFAULT_RADIUS_NM * NM, DEFAULT_HOLD_S)
    assert lights_settings({"lights": {"enabled": False, "radius_nm": 5, "hold_s": 30}}) == (False, 5 * NM, 30.0)
    assert {s.key: s.default for s in ac.SETTINGS if s.path[0] == "lights"} == {
        "lights.enabled": True, "lights.radius_nm": DEFAULT_RADIUS_NM, "lights.hold_s": DEFAULT_HOLD_S}
    assert ac.problems({"lights": {"enabled": "yes"}}) and ac.problems({"lights": {"hold_s": -1}})
    assert ac.problems({"lights": {"enabled": False, "radius_nm": 4.5}}) == []
    assert "`lights.hold_s`" in ac.reference_markdown()


def test_case_iii_as_the_calls_decide_it():
    ship = carrier()
    assert recovery_case_iii("III", None, None, ship, 0.0) and not recovery_case_iii("I", lambda c, t: True, None, ship, 0)
    assert recovery_case_iii("auto", lambda c, t: True, None, ship, 0.0)
    assert not recovery_case_iii("auto", lambda c, t: False, lambda: None, ship, 0.0)
    overcast = {"clouds": {"preset": "Preset20", "base_m": 200}}
    assert recovery_case_iii("auto", lambda c, t: False, lambda: overcast, ship, 0.0)


# -- the server hook ------------------------------------------------------------------------------------------------

STUBS = r"""
log = { INFO = 'INFO', ERROR = 'ERROR', write = function(src, lvl, msg) print('HOOKLOG ' .. msg) end }
local callbacks
local REAL = 0
DCS = { setUserCallbacks = function(c) callbacks = c end, getModelTime = function() return REAL end,
        getRealTime = function() return REAL end,
        getCurrentMission = function()
          local truman = { name = 'CVN-75 Harry S. Truman', unitId = 245, type = 'CVN_75' }
          return { mission = { coalition = { blue = { country = { { ship = { group = { { units = { truman } } } } } } } } } }
        end }
lfs = { writedir = function() return WRITEDIR end }
-- Enough JSON for the agent's file: objects, strings, and numbers.
local function json2lua(text)
  return assert(loadstring('return ' .. text:gsub('"([^"]*)"%s*:', '["%1"]=')))()
end
net = { json2lua = json2lua, lua2json = function(t)
          local parts = {}
          for k, v in pairs(t) do parts[#parts + 1] = string.format('%q:%s', k, type(v) == 'string' and string.format('%q', v) or tostring(v)) end
          return '{' .. table.concat(parts, ',') .. '}'
        end,
        get_player_list = function() return {} end, get_player_info = function() return {} end,
        dostring_in = function(state, code)
          assert(state == 'mission')
          local chunk = assert(loadstring(code))
          setfenv(chunk, MISSION)
          return chunk()
        end }
-- The mission's own Lua state: DCS's trigger action (each call printed), and a new one for each mission.
local function new_mission()
  MISSION = setmetatable({ a_do_script = function() end,
                           a_set_carrier_illumination_mode = function(id, mode) print('CALL ' .. id .. ', ' .. mode) end },
                         { __index = _G })
end
new_mission()

dofile(HOOK_PATH)

local function agent_writes(text)
  local f = assert(io.open(WRITEDIR .. 'Logs/burble-lights.json', 'w'))
  f:write(text)
  f:close()
end
local function frame(t) REAL = t; callbacks.onSimulationFrame() end
local now = os.time()

callbacks.onMissionLoadEnd()
frame(1)                                           -- no file yet: nothing
agent_writes(string.format('{"at": %d, "carriers": {"CVN-75 Harry S. Truman": "recovery"}}', now))
frame(1.5)                                         -- under a second since the last look
frame(2)                                           -- lit
frame(3)                                           -- already lit: not set again
agent_writes(string.format('{"at": %d, "carriers": {}}', now))
frame(4)                                           -- back to auto (the mission set nothing)
MISSION.a_set_carrier_illumination_mode(245, 0)    -- the mission's own trigger: navigation lights
agent_writes(string.format('{"at": %d, "carriers": {"CVN-75 Harry S. Truman": "recovery"}}', now - 100))
frame(5)                                           -- an old file (the agent stopped): nothing
agent_writes(string.format('{"at": %d, "carriers": {"CVN-71 Nobody": "recovery"}}', now))
frame(6)                                           -- not in the mission: logged as failed
frame(7)                                           -- and not tried again
agent_writes(string.format('{"at": %d, "carriers": {"CVN-75 Harry S. Truman": "recovery"}}', now))
frame(8)                                           -- lit again
agent_writes(string.format('{"at": %d, "carriers": {}}', now))
frame(9)                                           -- back to navigation, as the mission had it
new_mission()
callbacks.onMissionLoadEnd()                       -- a new mission has its own lighting
agent_writes(string.format('{"at": %d, "carriers": {"CVN-75 Harry S. Truman": "recovery"}}', now))
frame(10)                                          -- lit in the new mission
agent_writes(string.format('{"at": %d, "carriers": {"CVN-75 Harry S. Truman": "recovery"}}', now - 100))
frame(11)                                          -- the agent stopped: back to auto (this mission set nothing)
"""


def run_hook(tmp_path) -> tuple[list[str], list[dict]]:
    (tmp_path / "Logs").mkdir()
    writedir = str(tmp_path) + "/"
    script = f"HOOK_PATH = {str(HOOK)!r}\nWRITEDIR = {writedir!r}\n" + STUBS
    out = subprocess.run([luajit(), "-"], input=script, capture_output=True, text=True, check=True).stdout
    calls = re.findall(r"^CALL (.*)$", out, re.M)
    # (Only the lights events: the stub's lua2json is just enough for those.)
    return calls, [json.loads(m) for m in re.findall(r'^HOOKLOG BURBLE (\{.*"event":"lights".*\})$', out, re.M)]


def test_the_hook_sets_the_lights_the_agent_asks_for(tmp_path):
    calls, events = run_hook(tmp_path)
    # Recovery (2) and auto (-1), by the carrier's unit id from the mission.
    # The mission's own call (245, 0) goes through too; ours put back what it set, else auto.
    assert calls == ["245, 2", "245, -1", "245, 0", "245, 2", "245, 0", "245, 2", "245, -1"]
    truman = "CVN-75 Harry S. Truman"
    assert [(e["t"], e["carrier"], e.get("mode"), bool(e.get("restored")), e["ok"]) for e in events] == [
        (2, truman, "recovery", False, True), (4, truman, "auto", True, True), (6, "CVN-71 Nobody", None, False, False),
        (8, truman, "recovery", False, True), (9, truman, "navigation", True, True), (10, truman, "recovery", False, True),
        (11, truman, "auto", True, True)]
    assert events[2]["error"] == "no such carrier in the mission"
