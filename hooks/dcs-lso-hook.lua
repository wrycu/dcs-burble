-- dcs-lso metadata hook.
--
-- Install: copy to Saved Games/DCS/Scripts/Hooks/ (or DCS.server/ on a dedicated server).
--
-- When a mission loads, installs an event handler inside the mission scripting
-- environment that writes carrier-relevant events (touchdown, landing, and DCS's
-- LSO grade with the wire) to dcs.log as single `DCSLSO {json}` lines. The
-- collector tails dcs.log. The mission environment is sandboxed (no sockets or
-- file I/O), so the log is the channel.

local HANDLER = [==[
if DCSLSO_HANDLER then
  env.info('DCSLSO {"event":"handler_already_installed"}')
  return
end

local function try(f, ...)
  local ok, v = pcall(f, ...)
  if ok then return v end
end

local ESCAPES = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function encode(v)
  local t = type(v)
  if t == 'nil' then return 'null' end
  if t == 'boolean' then return tostring(v) end
  if t == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    return string.format('%.17g', v)
  end
  if t == 'string' then
    return '"' .. v:gsub('[%c"\\]', function(c)
      return ESCAPES[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
  end
  if t == 'table' then
    local parts = {}
    for k, x in pairs(v) do
      parts[#parts + 1] = encode(tostring(k)) .. ':' .. encode(x)
    end
    return '{' .. table.concat(parts, ',') .. '}'
  end
  return encode(tostring(v))
end

local function describe(o)
  if not o then return nil end
  local d = {
    name = try(function() return o:getName() end),
    type = try(function() return o:getTypeName() end),
    -- Runtime object id (maps to the Tacview object id).
    object_id = try(function() return o.id_ end),
    unit_id = try(function() return o:getID() end),
    player = try(function() return o:getPlayerName() end),
  }
  local p = try(function() return o:getPoint() end)
  if p then d.x, d.y, d.z = p.x, p.y, p.z end
  return d
end

local names = {}
local function watch(id, name)
  if id then names[id] = name end
end
watch(world.event.S_EVENT_RUNWAY_TOUCH, 'runway_touch')
watch(world.event.S_EVENT_LAND, 'land')
watch(world.event.S_EVENT_LANDING_QUALITY_MARK, 'landing_quality_mark')
watch(world.event.S_EVENT_TAKEOFF, 'takeoff')
watch(world.event.S_EVENT_RUNWAY_TAKEOFF, 'runway_takeoff')

DCSLSO_HANDLER = {}
-- Arresting wire animation arguments (wire 1..4), from CoreMods/tech/USS_Nimitz/Database/USS_CVN_7x.lua:
-- GT.animation_arguments.arresting_wires = {141, 142, 143, 144}
local WIRE_ARGS = { 141, 142, 143, 144 }

local function wire_args(carrier_name)
  local unit = try(function() return Unit.getByName(carrier_name) end)
  if not unit then return nil end
  local values = {}
  for i, arg in ipairs(WIRE_ARGS) do
    values['w' .. i] = try(function() return unit:getDrawArgumentValue(arg) end)
  end
  return values
end

local function log_event(fields)
  local ok, line = pcall(encode, fields)
  env.info('DCSLSO ' .. (ok and line or encode({ event = 'encode_error', error = tostring(line) })))
end

-- Sample the carrier's wire animation now and shortly after (the cable is fully paid out by the
-- time the aircraft stops), to find out which wire was caught without DCS's LSO grade.
local function sample_wires(source_event, carrier_name, initiator)
  for _, delay in ipairs({ 0, 0.5, 1.5, 3.0 }) do
    local function sample()
      log_event({ event = 'wire_sample', source = source_event, delay = delay, t = timer.getTime(),
                  carrier = carrier_name, initiator = initiator, wires = wire_args(carrier_name) })
    end
    if delay == 0 then sample() else timer.scheduleFunction(function() sample() return nil end, nil,
                                                             timer.getTime() + delay) end
  end
end

function DCSLSO_HANDLER:onEvent(e)
  local name = names[e.id]
  if not name then return end
  local place = describe(e.place)
  local initiator = describe(e.initiator)
  log_event({ event = name, t = e.time, comment = e.comment, initiator = initiator, place = place })
  if (name == 'runway_touch' or name == 'land') and place and place.name then
    pcall(sample_wires, name, place.name, initiator)
  end
end
world.addEventHandler(DCSLSO_HANDLER)
env.info('DCSLSO {"event":"handler_installed","t":' .. string.format('%.17g', timer.getTime()) .. '}')
]==]

local callbacks = {}

function callbacks.onMissionLoadEnd()
  local ok, result = pcall(net.dostring_in, 'mission', 'a_do_script([====[' .. HANDLER .. ']====])')
  log.write('DCSLSO', ok and log.INFO or log.ERROR, 'handler injection: ' .. tostring(ok) .. ' ' .. tostring(result))
end

DCS.setUserCallbacks(callbacks)
log.write('DCSLSO', log.INFO, 'dcs-lso hook loaded')
