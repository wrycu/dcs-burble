-- dcs-lso export probe (temporary, for measuring the server's real position update rate).
--
-- Logs the position of every aircraft near a carrier, every frame, to
-- Saved Games/<DCS dir>/Logs/dcs-lso-probe.csv, so `dcs-lso export-rate` can measure how
-- often the server really receives positions for client aircraft (DCS fills the gaps
-- between network updates by extrapolation).
--
-- Install: copy to Saved Games/<DCS dir>/Scripts/ and add this line at the END of
-- Scripts/Export.lua (after Tacview's and SRS's lines):
--   pcall(function() dofile(lfs.writedir() .. [[Scripts\dcs-lso-export-probe.lua]]) end)
-- Remove the line again after the test. It chains to the export functions defined
-- before it, so Tacview and SRS keep working; any error here is logged and ignored.

do
  local RANGE_M = 20000                 -- aircraft within this of a carrier are logged
  local MAX_BYTES = 200 * 1024 * 1024   -- stop logging past this file size
  local AIR, NAVY = 1, 3                -- wsType level1

  local path = lfs.writedir() .. [[Logs\dcs-lso-probe.csv]]
  local file, written, frames, failed = nil, 0, 0, false
  local ships_seen = {}

  local previous_after = LuaExportAfterNextFrame
  local previous_stop = LuaExportStop

  local function out(line)
    file:write(line, "\n")
    written = written + #line + 1
  end

  local function is_carrier(o)
    local name = o.Name or ""
    return o.Type and o.Type.level1 == NAVY and
      (name:find("CVN") or name:find("Stennis") or name:find("Forrestal") or name:find("LHA"))
  end

  local function frame()
    if written > MAX_BYTES then return end
    if file == nil then
      file = io.open(path, "a")  -- appended per mission (model time restarts at each)
      out("t,id,unit,x,y,z,heading,pitch,bank,lat,lon,alt")
    end
    local t = LoGetModelTime()
    local objects = LoGetWorldObjects() or {}
    local carriers = {}
    for id, o in pairs(objects) do
      if o.Type and o.Type.level1 == NAVY and not ships_seen[id] then
        ships_seen[id] = true
        out(string.format("# ship %d %s %s carrier=%s", id, tostring(o.Name), tostring(o.UnitName),
          tostring(is_carrier(o) and true or false)))
      end
      if is_carrier(o) and o.Position then carriers[#carriers + 1] = o end
    end
    for id, o in pairs(objects) do
      if o.Type and o.Type.level1 == AIR and o.Position then
        local p = o.Position
        for _, c in ipairs(carriers) do
          local dx, dz = p.x - c.Position.x, p.z - c.Position.z
          if dx * dx + dz * dz < RANGE_M * RANGE_M then
            local g = o.LatLongAlt or {}
            out(string.format("%.9f,%d,%s,%.6f,%.6f,%.6f,%.5f,%.5f,%.5f,%.7f,%.7f,%.2f",
              t, id, (o.UnitName or ""):gsub(",", " "), p.x, p.y, p.z, o.Heading or 0, o.Pitch or 0,
              o.Bank or 0, g.Lat or 0, g.Long or 0, g.Alt or 0))
            break
          end
        end
      end
    end
    frames = frames + 1
    if frames % 60 == 0 then file:flush() end
  end

  LuaExportAfterNextFrame = function()
    if previous_after then previous_after() end
    if failed then return end
    local ok, err = pcall(frame)
    if not ok then
      failed = true
      if file then out("# error: " .. tostring(err)); file:flush() end
    end
  end

  LuaExportStop = function()
    if previous_stop then previous_stop() end
    if file then file:close(); file = nil end
  end
end
