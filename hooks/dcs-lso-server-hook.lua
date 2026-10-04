-- dcs-lso server hook.
--
-- Install: copy to the DCS server's Saved Games/<DCS folder>/Scripts/Hooks/. If an older copy named
-- dcs-lso-hook.lua is there, delete it (both would run).
--
-- When a mission loads, installs an event handler inside the mission scripting
-- environment that writes carrier-relevant events (touchdown, landing, and DCS's
-- LSO grade with the wire) to dcs.log as single `DCSLSO {json}` lines. The
-- server agent tails dcs.log. The mission environment is sandboxed (no sockets or
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

-- The mission's wind at each carrier, by altitude, so AOA can be derived from motion for aircraft
-- whose AOA the server doesn't have (all of them, on a dedicated server). DCS vectors: x north, z east.
local CARRIER_TYPES = { 'CVN', 'Stennis', 'Forrestal', 'LHA', 'CV_1143' }
local WIND_ALTITUDES = { 10, 50, 100, 200, 400, 600 }
local WIND_INTERVAL_S = 30

local function is_carrier(type_name)
  for _, pattern in ipairs(CARRIER_TYPES) do
    if type_name:find(pattern, 1, true) then return true end
  end
  return false
end

local function log_wind()
  for _, side in ipairs({ coalition.side.NEUTRAL, coalition.side.RED, coalition.side.BLUE }) do
    for _, group in ipairs(try(function() return coalition.getGroups(side, Group.Category.SHIP) end) or {}) do
      for _, unit in ipairs(try(function() return group:getUnits() end) or {}) do
        local type_name = try(function() return unit:getTypeName() end) or ''
        local p = is_carrier(type_name) and try(function() return unit:getPoint() end)
        if p then
          local levels = {}
          for _, alt in ipairs(WIND_ALTITUDES) do
            local w = try(function() return atmosphere.getWind({ x = p.x, y = alt, z = p.z }) end)
            if w then levels[#levels + 1] = { alt = alt, east = w.z, north = w.x } end
          end
          log_event({ event = 'wind', t = timer.getTime(), carrier = try(function() return unit:getName() end),
                      type = type_name, levels = levels })
        end
      end
    end
  end
end

timer.scheduleFunction(function(_, now)
  pcall(log_wind)
  return now + WIND_INTERVAL_S
end, nil, timer.getTime() + 1)
env.info('DCSLSO {"event":"handler_installed","t":' .. string.format('%.17g', timer.getTime()) .. '}')
]==]

local callbacks = {}

-- The livery and side number (modex) of the slot a player takes, from the loaded mission (they're set
-- per slot by the mission designer). Logged as `DCSLSO {json}`, like the mission-side events.
local function find_unit(unit_id)
  local mission = DCS.getCurrentMission()
  local coalitions = mission and mission.mission and mission.mission.coalition or {}
  for _, side in pairs(coalitions) do
    for _, country in ipairs(side.country or {}) do
      for _, category in ipairs({ 'plane', 'helicopter' }) do
        for _, group in ipairs((country[category] or {}).group or {}) do
          for _, unit in ipairs(group.units or {}) do
            if unit.unitId == unit_id then return unit, group end
          end
        end
      end
    end
  end
end

local function log_slot(player_id)
  local info = net.get_player_info(player_id) or {}
  local unit_id = tonumber(info.slot)
  if not unit_id then return end  -- spectators, or a multicrew seat
  local unit, group = find_unit(unit_id)
  if not unit then return end
  local fields = { event = 'slot', t = DCS.getModelTime(), player = info.name, unit = unit.name,
                   unit_id = unit_id, group = group and group.name, type = unit.type,
                   livery = unit.livery_id, onboard_num = unit.onboard_num }
  log.write('DCSLSO', log.INFO, 'DCSLSO ' .. net.lua2json(fields))
end

-- Each carrier in the mission with the radio frequency set for it in the mission editor (Hz, and
-- modulation 0 = AM, 1 = FM), so the server agent can make its LSO calls there without configuration.
local CARRIER_TYPES = { 'CVN', 'Stennis', 'Forrestal', 'LHA', 'CV_1143' }

local function log_carriers()
  local mission = DCS.getCurrentMission()
  local coalitions = mission and mission.mission and mission.mission.coalition or {}
  for _, side in pairs(coalitions) do
    for _, country in ipairs(side.country or {}) do
      for _, group in ipairs((country.ship or {}).group or {}) do
        for _, unit in ipairs(group.units or {}) do
          local type_name = unit.type or ''
          for _, pattern in ipairs(CARRIER_TYPES) do
            if type_name:find(pattern, 1, true) then
              local fields = { event = 'carrier', t = DCS.getModelTime(), name = unit.name, type = type_name,
                               frequency = unit.frequency or group.frequency,
                               modulation = unit.modulation or group.modulation }
              log.write('DCSLSO', log.INFO, 'DCSLSO ' .. net.lua2json(fields))
              break
            end
          end
        end
      end
    end
  end
end

function callbacks.onMissionLoadEnd()
  local ok, result = pcall(net.dostring_in, 'mission', 'a_do_script([====[' .. HANDLER .. ']====])')
  log.write('DCSLSO', ok and log.INFO or log.ERROR, 'handler injection: ' .. tostring(ok) .. ' ' .. tostring(result))
  pcall(log_carriers)
end

function callbacks.onPlayerChangeSlot(player_id)
  pcall(log_slot, player_id)
end

-- Players already in their slots when a mission starts (and the local player, also in single player).
function callbacks.onSimulationStart()
  pcall(log_carriers)
  for _, player_id in ipairs(net.get_player_list() or {}) do
    pcall(log_slot, player_id)
  end
end

DCS.setUserCallbacks(callbacks)
log.write('DCSLSO', log.INFO, 'dcs-lso hook loaded')
