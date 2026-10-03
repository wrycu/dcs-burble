-- dcs-lso client probe (temporary). Main question: can a multiplayer CLIENT see which arresting wire
-- was caught? (A dedicated server can't: the carrier's wire animation, draw args 141-144 on the
-- Nimitz class, stays 0 there.)
--
-- Install on the pilot's PC: copy to Saved Games/DCS/Scripts/Hooks/, join the server, trap.
-- Results: lines starting "DCSLSO-PROBE" in Saved Games/DCS/Logs/dcs.log. Remove afterwards.
--
-- From simulation start, every 0.25 s it tries every route a client might have to the carrier's wire
-- animation, and logs at once when any shows a moving wire (plus each route's raw result every 30 s,
-- so a dead route is visible). It also logs every game event the client receives (DCS's LSO grade,
-- "WIRE# n", may arrive that way with comms).

local WIRE_ARGS = { 141, 142, 143, 144 }
local SAMPLE_S = 0.25
local REPORT_S = 30

local function probe_log(msg)
  log.write('DCSLSO-PROBE', log.INFO, msg)
end

local function run_in(env, code)
  local ok, result = pcall(net.dostring_in, env, code)
  if not ok then return 'error: ' .. tostring(result) end
  return tostring(result)
end

-- Route 1: the mission environment (where getDrawArgumentValue works for any unit on a host).
local MISSION_WIRES = [[
if not coalition or not Group then return 'mission env: no coalition/Group' end
local out = {}
for _, side in ipairs({ 0, 1, 2 }) do
  local ok, groups = pcall(coalition.getGroups, side, Group.Category.SHIP)
  for _, group in ipairs(ok and groups or {}) do
    for _, unit in ipairs(group:getUnits() or {}) do
      local type_name = unit:getTypeName() or ''
      if type_name:find('CVN', 1, true) or type_name:find('Stennis', 1, true) then
        local values = {}
        for i, arg in ipairs({ 141, 142, 143, 144 }) do
          local okv, v = pcall(unit.getDrawArgumentValue, unit, arg)
          values[i] = okv and string.format('%.3f', v) or 'err'
        end
        out[#out + 1] = unit:getName() .. '=' .. table.concat(values, ',')
      end
    end
  end
end
if #out == 0 then return 'mission env: no carriers' end
return table.concat(out, ' ')
]]

-- Route 2: the export environment. Finds the carriers in LoGetWorldObjects, then tries every Lo*
-- function whose name mentions "Draw" with (object id, arg), in case one reads other objects' args.
local EXPORT_WIRES = [[
if not LoGetWorldObjects then return 'export env: no LoGetWorldObjects' end
local draw = {}
for k, v in pairs(_G) do
  if type(v) == 'function' and type(k) == 'string' and k:sub(1, 2) == 'Lo' and k:find('Draw') then draw[#draw + 1] = k end
end
table.sort(draw)
local ok, objects = pcall(LoGetWorldObjects)
if not ok or not objects then return 'export env: LoGetWorldObjects failed; draw functions: ' .. table.concat(draw, ' ') end
local out = {}
for id, o in pairs(objects) do
  local name = o.Name or ''
  if name:find('CVN', 1, true) or name:find('Stennis', 1, true) then
    for _, fname in ipairs(draw) do
      local values = {}
      for i, arg in ipairs({ 141, 142, 143, 144 }) do
        local okv, v = pcall(_G[fname], id, arg)
        values[i] = okv and tostring(v) or 'err'
      end
      out[#out + 1] = (o.UnitName or tostring(id)) .. ' ' .. fname .. '=' .. table.concat(values, ',')
    end
    if #draw == 0 then out[#out + 1] = (o.UnitName or tostring(id)) .. ' (visible; no Draw functions)' end
  end
end
if #out == 0 then return 'export env: no carriers in LoGetWorldObjects; draw functions: ' .. table.concat(draw, ' ') end
return table.concat(out, ' | ')
]]

-- Route 3: this (GUI) environment's own Export table, if DCS provides one here.
local function gui_wires()
  if type(Export) ~= 'table' then return 'gui env: no Export table' end
  local get_objects = Export.LoGetWorldObjects
  local draw = {}
  for k, v in pairs(Export) do
    if type(v) == 'function' and type(k) == 'string' and k:find('Draw') then draw[#draw + 1] = k end
  end
  if type(get_objects) ~= 'function' then return 'gui env: Export has no LoGetWorldObjects; draw: ' .. table.concat(draw, ' ') end
  local ok, objects = pcall(get_objects)
  if not ok or not objects then return 'gui env: LoGetWorldObjects failed' end
  local out = {}
  for id, o in pairs(objects) do
    local name = o.Name or ''
    if name:find('CVN', 1, true) or name:find('Stennis', 1, true) then
      for _, fname in ipairs(draw) do
        local values = {}
        for i, arg in ipairs(WIRE_ARGS) do
          local okv, v = pcall(Export[fname], id, arg)
          values[i] = okv and tostring(v) or 'err'
        end
        out[#out + 1] = (o.UnitName or tostring(id)) .. ' ' .. fname .. '=' .. table.concat(values, ',')
      end
      if #draw == 0 then out[#out + 1] = (o.UnitName or tostring(id)) .. ' (visible; no Draw functions)' end
    end
  end
  return #out > 0 and table.concat(out, ' | ') or 'gui env: no carriers'
end

-- A wire is moving when any value parsed from a route's result is a number other than 0.
local function moving(result)
  for number in result:gmatch('[=,]([%-%d%.]+)') do
    local v = tonumber(number)
    if v and v ~= 0 then return true end
  end
  return false
end

local active, last_sample, last_report = false, 0, -1e9
local last = {}

local function sample(now)
  local results = {
    mission = run_in('mission', MISSION_WIRES),
    export = run_in('export', EXPORT_WIRES),
    gui = select(2, pcall(gui_wires)) or 'gui env: error',
  }
  local report = now - last_report >= REPORT_S
  for route, result in pairs(results) do
    result = tostring(result)
    if moving(result) and result ~= last[route] then
      probe_log(string.format('t=%.2f WIRE MOVING via %s: %s', now, route, result))
    elseif report then
      probe_log(string.format('t=%.2f %s: %s', now, route, result))
    end
    last[route] = result
  end
  if report then last_report = now end
end

local callbacks = {}

function callbacks.onSimulationStart()
  active = true
  last_report = -1e9
  probe_log('simulation started; server=' .. tostring(DCS.isServer and DCS.isServer()) ..
            ' multiplayer=' .. tostring(DCS.isMultiplayer and DCS.isMultiplayer()))
end

function callbacks.onSimulationStop()
  active = false
end

function callbacks.onSimulationFrame()
  if not active then return end
  local now = DCS.getModelTime()
  if now - last_sample < SAMPLE_S then return end
  last_sample = now
  local ok, err = pcall(sample, now)
  if not ok then probe_log('sample error: ' .. tostring(err)) end
end

-- Every game event the client receives (e.g. DCS's LSO grade with "WIRE# n", if it reaches clients).
function callbacks.onGameEvent(name, ...)
  local args = {}
  for i = 1, select('#', ...) do args[#args + 1] = tostring((select(i, ...))) end
  probe_log(string.format('t=%.2f game event %s: %s', DCS.getModelTime(), tostring(name), table.concat(args, ' | ')))
end

DCS.setUserCallbacks(callbacks)
probe_log('client probe loaded (wire focus)')
