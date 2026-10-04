"""The Lua hook, run under LuaJIT with stubbed DCS APIs, and the Python side that reads its output."""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from dcs_lso.dcslog import LsoGrade, follow, parse_hook_line

HOOK = Path(__file__).parents[1] / "hooks" / "dcs-lso-server-hook.lua"

STUBS = r"""
LOG = {}
log = { INFO = 'INFO', ERROR = 'ERROR', write = function(src, lvl, msg) print('HOOKLOG ' .. msg) end }
local callbacks
DCS = { setUserCallbacks = function(c) callbacks = c end }
function a_do_script(code) assert(loadstring(code))() end
net = { dostring_in = function(state, code) assert(state == 'mission'); assert(loadstring(code))(); return '', true end }
env = { info = function(msg) print('2026-09-28 12:00:00.123 INFO    SCRIPTING (Main): ' .. msg) end }
local scheduled = {}
timer = { getTime = function() return 12.5 end,
          scheduleFunction = function(fn, arg, at) scheduled[#scheduled + 1] = function() fn(arg, at) end end }
local CarrierUnit = {}
CarrierUnit.__index = CarrierUnit
function CarrierUnit:getDrawArgumentValue(arg) if arg == 143 then return 0.82 end return 0 end
Unit = { getByName = function(name)
  if name == 'CVN-75 Harry S. Truman' then return setmetatable({}, CarrierUnit) end
end }
local handlers = {}
world = {
  event = { S_EVENT_TAKEOFF = 3, S_EVENT_LAND = 4, S_EVENT_LANDING_QUALITY_MARK = 36,
            S_EVENT_RUNWAY_TAKEOFF = 54, S_EVENT_RUNWAY_TOUCH = 55, S_EVENT_SHOT = 1 },
  addEventHandler = function(h) handlers[#handlers + 1] = h end,
}
local Unit = {}
Unit.__index = Unit
function Unit:getName() return 'Aerial-1-1' end
function Unit:getTypeName() return 'FA-18C_hornet' end
function Unit:getID() return 2 end
function Unit:getPlayerName() return 'Wrycu "Test"' end
function Unit:getPoint() return { x = -306619.1, y = 22.1, z = 463016.9 } end
local Airbase = {}
Airbase.__index = Airbase
function Airbase:getName() return 'CVN-75 Harry S. Truman' end
function Airbase:getTypeName() return 'CVN_75' end
local plane = setmetatable({ id_ = 16788737 }, Unit)
local carrier = setmetatable({ id_ = 16777472 }, Airbase)

dofile(HOOK_PATH)
callbacks.onMissionLoadEnd()
callbacks.onMissionLoadEnd() -- second load must not double-install
for _, h in ipairs(handlers) do
  h:onEvent({ id = 55, time = 4806.84, initiator = plane, place = carrier })
  for _, f in ipairs(scheduled) do f() end
  h:onEvent({ id = 1, time = 4807.0, initiator = plane })
  h:onEvent({ id = 36, time = 4809.469, initiator = plane, place = carrier,
              comment = 'LSO: GRADE:C : EGIW  WIRE# 2[BC]' })
end
print('HANDLERS ' .. #handlers)
"""


@pytest.fixture(scope="module")
def lua_output():
    luajit = shutil.which("luajit")
    if luajit is None:
        pytest.skip("luajit not installed")
    script = f"HOOK_PATH = {str(HOOK)!r}\n" + STUBS
    result = subprocess.run([luajit, "-"], input=script, capture_output=True, text=True, check=True)
    return result.stdout.splitlines()


def test_hook_installs_once_and_logs_injection(lua_output):
    assert "HANDLERS 1" in lua_output
    assert any("handler injection: true" in line for line in lua_output)
    events = [e.event for e in map(parse_hook_line, lua_output) if e]
    assert events.count("handler_installed") == 1
    assert "handler_already_installed" in events


def test_hook_events_round_trip(lua_output):
    events = [e for e in map(parse_hook_line, lua_output) if e and e.event not in
              ("handler_installed", "handler_already_installed")]
    samples = [e for e in events if e.event == "wire_sample"]
    events = [e for e in events if e.event != "wire_sample"]
    assert [e.event for e in events] == ["runway_touch", "landing_quality_mark"]  # S_EVENT_SHOT ignored
    # runway_touch -> wire animation sampled now and at +0.5/+1.5/+3 s; wire 3 (arg 143) deflected.
    assert [s.raw["delay"] for s in samples] == [0, 0.5, 1.5, 3.0]
    assert all(s.raw["wires"] == {"w1": 0, "w2": 0, "w3": 0.82, "w4": 0} for s in samples)
    assert samples[0].raw["carrier"] == "CVN-75 Harry S. Truman"
    touch, mark = events
    assert touch.time == pytest.approx(4806.84)
    assert touch.initiator["player"] == 'Wrycu "Test"'
    assert touch.initiator["object_id"] == 16788737
    assert touch.place["name"] == "CVN-75 Harry S. Truman"
    assert "player" not in touch.place  # Airbase has no getPlayerName; must not break the event
    assert touch.logged_at is not None
    assert LsoGrade.parse(mark.comment).wire == 2


def test_parse_ignores_other_lines():
    assert parse_hook_line("2026-09-28 12:00:00.123 INFO    TACVIEW: something") is None
    assert parse_hook_line("DCSLSO {not json") is None


def test_follow_sees_appended_and_replaced_file(tmp_path):
    log = tmp_path / "dcs.log"
    log.write_text("old line\n")
    seen: list[str] = []

    def reader():
        for line in follow(log, poll=0.01):
            seen.append(line)
            if line == "after-replace":
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.1)
    with log.open("a") as fh:
        fh.write("appended\npart")
        fh.flush()
        time.sleep(0.05)
        fh.write("ial\n")
    time.sleep(0.1)
    replacement = tmp_path / "new.log"
    replacement.write_text("after-replace\n")
    replacement.replace(log)
    thread.join(timeout=2)
    assert seen == ["appended", "partial", "after-replace"]
