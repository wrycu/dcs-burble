-- Burble server hook.
--
-- Install: copy to the DCS server's Saved Games/<DCS folder>/Scripts/Hooks/.
--
-- When a mission loads, installs an event handler inside the mission scripting
-- environment that writes carrier-relevant events (touchdown, landing, and DCS's
-- LSO grade with the wire) to dcs.log as single `BURBLE {json}` lines. The
-- server agent tails dcs.log. The mission environment is sandboxed (no sockets or
-- file I/O), so the log is the channel.

local HANDLER = [==[
if BURBLE_HANDLER then
  env.info('BURBLE {"event":"handler_already_installed"}')
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

BURBLE_HANDLER = {}
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
  env.info('BURBLE ' .. (ok and line or encode({ event = 'encode_error', error = tostring(line) })))
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

function BURBLE_HANDLER:onEvent(e)
  local name = names[e.id]
  if not name then return end
  local place = describe(e.place)
  local initiator = describe(e.initiator)
  log_event({ event = name, t = e.time, comment = e.comment, initiator = initiator, place = place })
  if (name == 'runway_touch' or name == 'land') and place and place.name then
    pcall(sample_wires, name, place.name, initiator)
  end
end
world.addEventHandler(BURBLE_HANDLER)

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

-- Turbulence in the groove: the gusts (DCS's wind with turbulence, less the steady wind) at points along the
-- glide path astern of the carrier, as an RMS speed in m/s.
local TURBULENCE_DISTANCES = { 200, 400, 600, 800, 1000, 1200, 1400, 1600 }

local function turbulence(p, forward)
  if not atmosphere.getWindWithTurbulence then return nil, 'no atmosphere.getWindWithTurbulence' end
  local sum, n = 0, 0
  for _, d in ipairs(TURBULENCE_DISTANCES) do
    local at = { x = p.x - forward.x * d, y = 20 + d * 0.061, z = p.z - forward.z * d }  -- about 3.5 degrees
    local steady = try(function() return atmosphere.getWind(at) end)
    local gusty = try(function() return atmosphere.getWindWithTurbulence(at) end)
    if steady and gusty then
      local dx, dy, dz = gusty.x - steady.x, (gusty.y or 0) - (steady.y or 0), gusty.z - steady.z
      sum, n = sum + dx * dx + dy * dy + dz * dz, n + 1
    end
  end
  if n == 0 then return nil, 'no wind samples' end
  return math.sqrt(sum / n)
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
          local pos = try(function() return unit:getPosition() end)
          local gusts, why = nil, 'no carrier heading'
          if pos then
            local ok, value, reason = pcall(turbulence, p, pos.x)
            gusts, why = ok and value or nil, ok and reason or tostring(value)
          end
          log_event({ event = 'wind', t = timer.getTime(), carrier = try(function() return unit:getName() end),
                      type = type_name, levels = levels, turbulence = gusts,
                      turbulence_error = gusts == nil and why or nil })
        end
      end
    end
  end
end

-- The mission's weather settings (once per mission), for the trap card.
local function log_weather()
  local w = env.mission and env.mission.weather
  if not w then
    log_event({ event = 'weather', t = timer.getTime(), error = env.mission and 'no env.mission.weather' or 'no env.mission' })
    return
  end
  local clouds, fog = w.clouds or {}, w.fog or {}
  log_event({ event = 'weather', t = timer.getTime(), dynamic = w.atmosphere_type == 1,
              ground_turbulence = w.groundTurbulence, temperature = w.season and w.season.temperature,
              qnh_mmhg = w.qnh, visibility_m = w.visibility and w.visibility.distance,
              clouds = { preset = clouds.preset, base_m = clouds.base, thickness_m = clouds.thickness,
                         density = clouds.density, precipitation = clouds.iprecptns },
              fog = w.enable_fog and { visibility_m = fog.visibility, thickness_m = fog.thickness } or nil,
              dust_m = w.enable_dust and w.dust_density or nil })
end
local weather_ok, weather_error = pcall(log_weather)
if not weather_ok then log_event({ event = 'weather', t = timer.getTime(), error = tostring(weather_error) }) end

-- Every airplane's landing gear and tail hook (0 up .. 1 down), from their animation, for "wave off, gear" and
-- "wave off, hook": Tacview sends the gear only for the recording PC's own jet, but on a dedicated server the
-- mission has a client jet's animation too (probe, 2026-10-28). Draw args: 0 nose, 3 and 5 mains, 25 hook. The
-- gear is the least lowered leg. Logged when either moves by GEAR_STEP or more since last logged, and only for
-- jets within GEAR_RADIUS_M of a carrier (a jet coming back into range is logged afresh).
local GEAR_ARGS = { 0, 3, 5 }
local HOOK_ARG = 25
local GEAR_INTERVAL_S = 1
local GEAR_STEP = 0.1
local GEAR_RADIUS_M = 2 * 1852  -- the gear and hook wave-offs are inside 0.5 nm
local gear_logged = {}  -- object id -> { gear, hook } as last logged

local function carrier_points()
  local points = {}
  for _, side in ipairs({ coalition.side.NEUTRAL, coalition.side.RED, coalition.side.BLUE }) do
    for _, group in ipairs(try(function() return coalition.getGroups(side, Group.Category.SHIP) end) or {}) do
      for _, unit in ipairs(try(function() return group:getUnits() end) or {}) do
        local p = is_carrier(try(function() return unit:getTypeName() end) or '') and try(function() return unit:getPoint() end)
        if p then points[#points + 1] = p end
      end
    end
  end
  return points
end

local function near_carrier(unit, carriers)
  local p = try(function() return unit:getPoint() end)
  if not p then return false end
  for _, c in ipairs(carriers) do
    local dx, dz = p.x - c.x, p.z - c.z
    if dx * dx + dz * dz <= GEAR_RADIUS_M * GEAR_RADIUS_M then return true end
  end
  return false
end

local function log_gear()
  local carriers = carrier_points()
  for _, side in ipairs({ coalition.side.NEUTRAL, coalition.side.RED, coalition.side.BLUE }) do
    for _, group in ipairs(#carriers > 0 and try(function() return coalition.getGroups(side, Group.Category.AIRPLANE) end) or {}) do
      for _, unit in ipairs(try(function() return group:getUnits() end) or {}) do
        local id = try(function() return unit.id_ end)
        if id and not near_carrier(unit, carriers) then
          gear_logged[id] = nil
        elseif id then
          local gear
          for _, arg in ipairs(GEAR_ARGS) do
            local v = try(function() return unit:getDrawArgumentValue(arg) end)
            if v and (gear == nil or v < gear) then gear = v end
          end
          local hook = try(function() return unit:getDrawArgumentValue(HOOK_ARG) end)
          local last = gear_logged[id]
          if gear and hook and (not last or math.abs(gear - last[1]) >= GEAR_STEP
                                or math.abs(hook - last[2]) >= GEAR_STEP) then
            gear_logged[id] = { gear, hook }
            log_event({ event = 'gear', t = timer.getTime(), object_id = id,
                        name = try(function() return unit:getName() end),
                        player = try(function() return unit:getPlayerName() end), gear = gear, hook = hook })
          end
        end
      end
    end
  end
end

timer.scheduleFunction(function(_, now)
  pcall(log_wind)
  return now + WIND_INTERVAL_S
end, nil, timer.getTime() + 1)
timer.scheduleFunction(function(_, now)
  pcall(log_gear)
  return now + GEAR_INTERVAL_S
end, nil, timer.getTime() + 1)
env.info('BURBLE {"event":"handler_installed","t":' .. string.format('%.17g', timer.getTime()) .. '}')
]==]

local callbacks = {}

-- The livery and side number (modex) of the slot a player takes, from the loaded mission (they're set
-- per slot by the mission designer). Logged as `BURBLE {json}`, like the mission-side events.
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
  log.write('BURBLE', log.INFO, 'BURBLE ' .. net.lua2json(fields))
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
              log.write('BURBLE', log.INFO, 'BURBLE ' .. net.lua2json(fields))
              break
            end
          end
        end
      end
    end
  end
end

-- Everyone connected (not the server itself): UCID (DCS account id), IP address, and name, so the hub can
-- recognise a pilot hook sent from a player on this server. Logged on every connect and disconnect, and
-- once a minute (so a server agent that restarts mid-mission learns it again).
local PLAYERS_EVERY_S = 60
local last_players = -PLAYERS_EVERY_S

local function log_players()
  local players = {}
  local server_id = net.get_server_id and net.get_server_id() or 1
  for _, player_id in ipairs(net.get_player_list() or {}) do
    if player_id ~= server_id then
      local info = net.get_player_info(player_id) or {}
      if info.ucid then
        local ip = tostring(info.ipaddr or ''):gsub(':%d+$', '')  -- "a.b.c.d:port" -> "a.b.c.d"
        players[#players + 1] = { id = player_id, ucid = info.ucid, ip = ip, name = info.name }
      end
    end
  end
  last_players = DCS.getRealTime and DCS.getRealTime() or 0
  log.write('BURBLE', log.INFO, 'BURBLE ' .. net.lua2json({ event = 'players', t = DCS.getModelTime(), players = players }))
end

-- The carriers' landing lights (PLAN #54). The server agent writes which carriers want them, by unit name, to
-- Logs/burble-lights.json (`{"at": <unix time>, "carriers": {"CVN-75 ...": "recovery"}}`, rewritten every few seconds
-- while it runs); those are set to AC RECOVERY, and put back as the mission had them once they're no longer wanted or
-- the file is older than LIGHTS_STALE_S (the agent stopped). Only carriers set here are put back. DCS's "SET CARRIER
-- LIGHTING MODE" trigger action is a function in the mission's own Lua state (not the scripting sandbox), by the
-- carrier's unit id: modes -2 OFF, -1 AUTO, 0 NAVIGATION, 1 AC LAUNCH, 2 AC RECOVERY
-- (MissionEditor/modules/me_trigrules.lua). DCS can't say which mode a carrier is in, so when a mission loads the
-- function is wrapped (LIGHTS_WRAPPER) to note the last mode the mission's own triggers set for each carrier; ours
-- go to the original (not noted). Put back: that mode, else AUTO (a carrier's mode until something sets it).
local LIGHTS_FILE = lfs and lfs.writedir and lfs.writedir() .. 'Logs/burble-lights.json'
local LIGHTS_EVERY_S = 1
local LIGHTS_STALE_S = 30
local LIGHTS_RECOVERY = 2
local LIGHTS_MODE_NAMES = { [-2] = 'off', [-1] = 'auto', [0] = 'navigation', [1] = 'launch', [2] = 'recovery' }
local LIGHTS_WRAPPER = [[
if BURBLE_LIGHTS == nil and a_set_carrier_illumination_mode ~= nil then
  BURBLE_LIGHTS = { set = a_set_carrier_illumination_mode, mission = {} }
  a_set_carrier_illumination_mode = function(unit_id, mode, ...)
    if tonumber(unit_id) and tonumber(mode) then BURBLE_LIGHTS.mission[tonumber(unit_id)] = tonumber(mode) end
    return BURBLE_LIGHTS.set(unit_id, mode, ...)
  end
end
return BURBLE_LIGHTS and "ok" or "no a_set_carrier_illumination_mode"
]]
-- Run in the mission's state: set one carrier's mode (%d: unit id; %s: the mode, or the mission's own), and say which.
local LIGHTS_SET = [[
local set = BURBLE_LIGHTS and BURBLE_LIGHTS.set or a_set_carrier_illumination_mode
if set == nil then return "no a_set_carrier_illumination_mode" end
local mode = %s
set(%d, mode)
return "ok " .. tostring(mode)
]]
local lights_lit = {}  -- carrier name -> true: set to recovery this mission
local lights_failed = {}  -- carrier name -> true: couldn't be set this mission (not tried again)
local last_lights = -LIGHTS_EVERY_S

local function carrier_unit_id(name)
  local mission = DCS.getCurrentMission()
  local coalitions = mission and mission.mission and mission.mission.coalition or {}
  for _, side in pairs(coalitions) do
    for _, country in ipairs(side.country or {}) do
      for _, group in ipairs((country.ship or {}).group or {}) do
        for _, unit in ipairs(group.units or {}) do
          if unit.name == name then return unit.unitId end
        end
      end
    end
  end
end

-- Recovery (`recover` true), or back as the mission had it.
local function set_lights(name, recover)
  local id = carrier_unit_id(name)
  local ok, result = false, 'no such carrier in the mission'
  if id then
    local mode = recover and tostring(LIGHTS_RECOVERY)
                 or string.format('BURBLE_LIGHTS and BURBLE_LIGHTS.mission[%d] or -1', id)
    ok, result = pcall(net.dostring_in, 'mission', string.format(LIGHTS_SET, mode, id))
    ok = ok and type(result) == 'string' and result:sub(1, 3) == 'ok '
  end
  local set = ok and tonumber(result:sub(4)) or nil
  local fields = { event = 'lights', t = DCS.getModelTime(), carrier = name, mode = LIGHTS_MODE_NAMES[set] or set,
                   restored = not recover or nil, ok = ok, error = not ok and tostring(result) or nil }
  log.write('BURBLE', log.INFO, 'BURBLE ' .. net.lua2json(fields))
  return ok
end

local function wanted_lights()
  local f = LIGHTS_FILE and io.open(LIGHTS_FILE, 'r')
  if not f then return {} end
  local text = f:read('*a')
  f:close()
  local ok, data = pcall(net.json2lua, text)
  if not ok or type(data) ~= 'table' or type(data.at) ~= 'number' or os.time() - data.at > LIGHTS_STALE_S then
    return {}
  end
  return type(data.carriers) == 'table' and data.carriers or {}
end

local function update_lights()
  local wanted = wanted_lights()
  for name, mode in pairs(wanted) do
    if mode == 'recovery' and not lights_lit[name] and not lights_failed[name] then
      if set_lights(name, true) then lights_lit[name] = true else lights_failed[name] = true end
    end
  end
  for name in pairs(lights_lit) do
    if wanted[name] ~= 'recovery' then
      set_lights(name, false)
      lights_lit[name] = nil
    end
  end
end

function callbacks.onPlayerConnect(player_id)
  pcall(log_players)
end

function callbacks.onPlayerDisconnect(player_id)
  pcall(log_players)
end

function callbacks.onSimulationFrame()
  local now = DCS.getRealTime and DCS.getRealTime() or 0
  if now - last_players >= PLAYERS_EVERY_S then
    pcall(log_players)
  end
  if now - last_lights >= LIGHTS_EVERY_S then
    last_lights = now
    pcall(update_lights)
  end
end

function callbacks.onMissionLoadEnd()
  lights_lit, lights_failed = {}, {}  -- a new mission starts with its own lighting
  local ok, result = pcall(net.dostring_in, 'mission', 'a_do_script([====[' .. HANDLER .. ']====])')
  log.write('BURBLE', ok and log.INFO or log.ERROR, 'handler injection: ' .. tostring(ok) .. ' ' .. tostring(result))
  ok, result = pcall(net.dostring_in, 'mission', LIGHTS_WRAPPER)
  log.write('BURBLE', ok and result == 'ok' and log.INFO or log.ERROR, 'lights wrapper: ' .. tostring(result))
  pcall(log_carriers)
end

function callbacks.onPlayerChangeSlot(player_id)
  pcall(log_slot, player_id)
  pcall(log_players)
end

-- Players already in their slots when a mission starts (and the local player, also in single player).
function callbacks.onSimulationStart()
  pcall(log_carriers)
  for _, player_id in ipairs(net.get_player_list() or {}) do
    pcall(log_slot, player_id)
  end
end

DCS.setUserCallbacks(callbacks)
log.write('BURBLE', log.INFO, 'Burble hook loaded')
