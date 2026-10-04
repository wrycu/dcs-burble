-- dcs-lso pilot hook: the recorder (runs in DCS's Export.lua environment).
--
-- Records your own jet (position, attitude, AOA) about 50 times a second, finds each approach to deck
-- height, and writes it to Saved Games/DCS/Logs/dcs-lso/ as a CSV file. The pilot hook's uploader
-- (Scripts/Hooks/dcs-lso-pilot-hook.lua) sends those files to the hubs set in Options > Special > DCS-LSO.
--
-- Install: add this line at the END of Saved Games/DCS/Scripts/Export.lua (after Tacview's and SRS's):
--   pcall(function() dofile(lfs.writedir() .. [[Scripts\dcs-lso-pilot-recorder.lua]]) end)
-- It calls the export functions defined before it, so Tacview and SRS keep working; any error here is
-- logged and switches the recorder off rather than affecting them.

do
  local VERSION = 1
  local RATE_S = 0.02                -- sample interval (50 Hz)
  local LEAD_S, TAIL_S = 45, 10      -- seconds kept before an approach starts, and recorded after it ends
  -- Approaches, as the hub finds them (dcs_lso.detect.approaches): from above pattern altitude to below
  -- APPROACH_BELOW_M, counting only if it gets down near deck height; it ends climbing away or stopped.
  local ARMED_ABOVE_M, APPROACH_BELOW_M, DECK_BELOW_M = 170, 120, 60
  local STOPPED_SPEED_MS, STOPPED_DURATION_S = 3, 5
  local MAX_APPROACH_S = 600
  local AIRCRAFT = { ['FA-18C_hornet'] = true }  -- aircraft the hubs grade
  local HEADER = 't,x,y,z,heading,pitch,bank,aoa,lat,lon'

  local dir = lfs.writedir() .. 'Logs/dcs-lso/'
  local rows = {}          -- { t = model time, line = CSV row }, oldest first
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
    local name = string.format('%sapproach-%d-%d.csv', dir, os.time(), math.floor(a.from))
    local f = io.open(name .. '.part', 'w')
    if not f then note('cannot write ' .. name) return end
    f:write(table.concat(out, '\n'), '\n')
    f:close()
    os.remove(name)
    os.rename(name .. '.part', name)  -- the uploader only picks up complete files
    note(string.format('approach %.0f-%.0f s written (%d samples)', a.from + LEAD_S, a.to - TAIL_S, #out - 6))
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
    local first = 1
    while first <= #rows and rows[first].t < keep_from do first = first + 1 end
    if first > 1 then
      local kept = {}
      for i = first, #rows do kept[#kept + 1] = rows[i] end
      rows = kept
    end
  end

  local function flush(t)
    if seg.start and seg.low and seg.low <= DECK_BELOW_M and seg.aircraft then
      pending[#pending + 1] = { from = seg.start - LEAD_S, to = t, aircraft = seg.aircraft, pilot = seg.pilot }
    end
    for _, a in ipairs(pending) do write_approach(a, t) end
    rows, pending, seg = {}, {}, { armed = false }
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
