-- dcs-lso pilot hook: the recorder (runs in DCS's Export.lua environment).
--
-- Records your own jet (position, attitude, AOA) about 50 times a second, and the nearest carrier 10 times
-- a second where the server lets clients see other objects, finds each approach to deck height, and writes it
-- to Saved Games/DCS/Logs/dcs-lso/ as a CSV file. With the carrier, a hub can grade the pass on its own (e.g.
-- one flown on another community's server). The pilot hook's uploader
-- (Scripts/Hooks/dcs-lso-pilot-hook.lua) sends those files to the hubs set in Options > Special > DCS-LSO.
--
-- Run by the pilot hook (Scripts/Hooks/dcs-lso-pilot-hook.lua), every frame, with DCS's export functions
-- (Export.Lo*, which hooks can call) as its LoGet* functions: nothing needs adding to Export.lua, which SRS's
-- and others' installers rewrite. A line left in Export.lua by an older install loads it there too; it then
-- does nothing, so each approach is recorded once.

if not DCSLSO_PILOT_HOOK then
  if log and log.write then
    log.write('DCSLSO-PILOT', log.INFO, 'recorder: the line in Export.lua is no longer needed (the pilot hook runs it)')
  end
else
  local VERSION = 2
  local RATE_S = 0.02                -- sample interval (50 Hz)
  local LEAD_S, TAIL_S = 45, 10      -- seconds kept before an approach starts, and recorded after it ends
  -- Approaches, as the hub finds them (dcs_lso.detect.approaches): from above pattern altitude to below
  -- APPROACH_BELOW_M, counting only if it gets down near deck height; it ends climbing away or stopped.
  local ARMED_ABOVE_M, APPROACH_BELOW_M, DECK_BELOW_M = 170, 120, 60
  -- Stopped: on deck after a trap the jet still moves with the carrier (up to ~16 m/s), so 'stopped' is any
  -- ground speed below what an aircraft flies at.
  local STOPPED_SPEED_MS, STOPPED_DURATION_S = 25, 5
  local MAX_APPROACH_S = 600
  local AIRCRAFT = { ['FA-18C_hornet'] = true, ['F-14A-135-GR'] = true, ['F-14B'] = true, ['F-14BU'] = true }  -- graded
  local HEADER = 't,x,y,z,heading,pitch,bank,aoa,lat,lon'
  local CARRIER_HEADER = 't,x,y,z,heading,lat,lon'
  local CARRIER_RATE_S = 0.1
  local CARRIER_RANGE_M = 20000
  local NAVY = 3  -- wsType level1 of ships
  local CARRIER_NAMES = { 'CVN', 'Stennis', 'Forrestal', 'CV_1143', 'ara_vdm' }

  local dir = lfs.writedir() .. 'Logs/dcs-lso/'
  local rows = {}          -- { t = model time, line = CSV row }, oldest first
  local carrier_rows = {}  -- { t, line, id, type, unit }: the nearest carrier, when other objects are visible
  local last_carrier_t = nil
  local pending = {}       -- approaches waiting for their tail: { from, to, aircraft, pilot }
  local seg = { armed = false }
  local last_t, failed = nil, false

  local previous_after = LuaExportAfterNextFrame
  local previous_stop = LuaExportStop

  local function note(msg)
    if log and log.write then log.write('DCSLSO-PILOT', log.INFO, msg) end
  end

  local function write_approach(a, model_time)
    pcall(lfs.mkdir, dir)
    local out = { '# dcs-lso pilot hook ' .. VERSION, '# aircraft=' .. a.aircraft, '# pilot=' .. (a.pilot or ''),
                  string.format('# written_at=%d', os.time()), string.format('# model_time=%.3f', model_time),
                  HEADER }
    for _, r in ipairs(rows) do
      if r.t >= a.from and r.t <= a.to then out[#out + 1] = r.line end
    end
    -- The carrier the jet ended up nearest (its last sample in the approach), all of its samples in the window.
    local carrier
    for _, c in ipairs(carrier_rows) do
      if c.t >= a.from and c.t <= a.to then carrier = c end
    end
    if carrier then
      out[#out + 1] = '## carrier'
      out[#out + 1] = '# carrier_type=' .. carrier.type
      out[#out + 1] = '# carrier_unit=' .. carrier.unit
      out[#out + 1] = CARRIER_HEADER
      for _, c in ipairs(carrier_rows) do
        if c.id == carrier.id and c.t >= a.from and c.t <= a.to then out[#out + 1] = c.line end
      end
    end
    local name = string.format('%sapproach-%d-%d.csv', dir, os.time(), math.floor(a.from))
    local f = io.open(name .. '.part', 'w')
    if not f then note('cannot write ' .. name) return end
    f:write(table.concat(out, '\n'), '\n')
    f:close()
    os.remove(name)
    os.rename(name .. '.part', name)  -- the uploader only picks up complete files
    note(string.format('approach %.0f-%.0f s written (%d lines%s)', a.from + LEAD_S, a.to - TAIL_S, #out - 6,
      carrier and (', with ' .. carrier.unit) or ', no carrier: other objects not visible here'))
  end

  -- One sample through the approach finder; returns {start, finish} when an approach ends.
  local function segment(t, x, z, alt)
    local last = seg.last
    seg.last = { t = t, x = x, z = z }
    if not seg.start then
      if alt > ARMED_ABOVE_M then
        seg.armed = true
      elseif seg.armed and alt < APPROACH_BELOW_M then
        seg.start, seg.low, seg.stopped = t, alt, nil
      end
      return nil
    end
    seg.low = math.min(seg.low, alt)
    local function finish(armed)
      local start, low = seg.start, seg.low
      seg.start, seg.low, seg.stopped, seg.armed = nil, nil, nil, armed
      if low <= DECK_BELOW_M then return { start = start, finish = t } end
    end
    if alt > ARMED_ABOVE_M or t - seg.start > MAX_APPROACH_S then return finish(true) end
    if last and t > last.t then
      local speed = math.sqrt((x - last.x) ^ 2 + (z - last.z) ^ 2) / (t - last.t)
      if speed < STOPPED_SPEED_MS then
        seg.stopped = seg.stopped or t
        if t - seg.stopped >= STOPPED_DURATION_S then return finish(false) end
      else
        seg.stopped = nil
      end
    end
    return nil
  end

  local function trim(t)
    local keep_from = t - LEAD_S - 5
    if seg.start then keep_from = math.min(keep_from, seg.start - LEAD_S) end
    for _, a in ipairs(pending) do keep_from = math.min(keep_from, a.from) end
    local function trimmed(list)
      local first = 1
      while first <= #list and list[first].t < keep_from do first = first + 1 end
      if first == 1 then return list end
      local kept = {}
      for i = first, #list do kept[#kept + 1] = list[i] end
      return kept
    end
    rows, carrier_rows = trimmed(rows), trimmed(carrier_rows)
  end

  local function is_carrier(o)
    if not o.Type or o.Type.level1 ~= NAVY or not o.Position then return false end
    for _, pattern in ipairs(CARRIER_NAMES) do
      if (o.Name or ''):find(pattern, 1, true) then return true end
    end
    return false
  end

  -- The nearest carrier within range, if the server lets clients see other objects.
  local function sample_carrier(t, x, z)
    if not LoGetWorldObjects then return end
    local best, best_d, best_id
    for id, o in pairs(LoGetWorldObjects() or {}) do
      if is_carrier(o) then
        local d = (o.Position.x - x) ^ 2 + (o.Position.z - z) ^ 2
        if d < CARRIER_RANGE_M ^ 2 and (not best_d or d < best_d) then best, best_d, best_id = o, d, id end
      end
    end
    if not best then return end
    local p, g = best.Position, best.LatLongAlt or {}
    carrier_rows[#carrier_rows + 1] = { t = t, id = best_id, type = best.Name or '', unit = best.UnitName or best.Name or '',
      line = string.format('%.3f,%.3f,%.3f,%.3f,%.6f,%.7f,%.7f', t, p.x, p.y, p.z, best.Heading or 0, g.Lat or 0, g.Long or 0) }
  end

  local function flush(t)
    if seg.start and seg.low and seg.low <= DECK_BELOW_M and seg.aircraft then
      pending[#pending + 1] = { from = seg.start - LEAD_S, to = t, aircraft = seg.aircraft, pilot = seg.pilot }
    end
    for _, a in ipairs(pending) do write_approach(a, t) end
    rows, carrier_rows, pending, seg = {}, {}, {}, { armed = false }
  end

  local function frame()
    local t = LoGetModelTime()
    if not t then return end
    if last_t and t < last_t then flush(last_t); last_t = nil end  -- a new mission: times restart
    if last_t and t - last_t < RATE_S then return end
    local s = LoGetSelfData()
    if not s or not s.Position or not AIRCRAFT[s.Name or ''] then
      if seg.start or #pending > 0 then flush(last_t or t) end  -- respawned, ejected, other aircraft
      last_t = t
      return
    end
    last_t = t
    local p, g = s.Position, s.LatLongAlt or {}
    local aoa = LoGetAngleOfAttack and LoGetAngleOfAttack() or 0
    rows[#rows + 1] = { t = t, line = string.format('%.3f,%.3f,%.3f,%.3f,%.6f,%.6f,%.6f,%.3f,%.7f,%.7f',
      t, p.x, p.y, p.z, s.Heading or 0, s.Pitch or 0, s.Bank or 0, aoa or 0, g.Lat or 0, g.Long or 0) }
    seg.aircraft, seg.pilot = s.Name, LoGetPilotName and LoGetPilotName() or nil
    if not last_carrier_t or t < last_carrier_t or t - last_carrier_t >= CARRIER_RATE_S then
      last_carrier_t = t
      sample_carrier(t, p.x, p.z)
    end
    local found = segment(t, p.x, p.z, p.y)
    if found then
      pending[#pending + 1] = { from = found.start - LEAD_S, to = found.finish + TAIL_S,
                                aircraft = seg.aircraft, pilot = seg.pilot }
    end
    local still = {}
    for _, a in ipairs(pending) do
      if t >= a.to then write_approach(a, t) else still[#still + 1] = a end
    end
    pending = still
    trim(t)
  end

  LuaExportAfterNextFrame = function()
    if previous_after then previous_after() end
    if failed then return end
    local ok, err = pcall(frame)
    if not ok then failed = true; note('recorder stopped: ' .. tostring(err)) end
  end

  LuaExportStop = function()
    if previous_stop then previous_stop() end
    if not failed then pcall(flush, last_t or 0) end
  end

  note('recorder loaded')
end
