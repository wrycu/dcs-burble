-- dcs-lso pilot hook: the uploader (runs in DCS's GUI environment, Saved Games/DCS/Scripts/Hooks/).
--
-- Sends the approaches the recorder (Scripts/dcs-lso-pilot-recorder.lua) writes to Logs/dcs-lso/ to the
-- hubs set in Options > Special > DCS-LSO:
-- - the hub of the server you're flying on (each hub is asked whether you're on one of its servers; it
--   recognises you by your DCS account and address, so no token is needed there);
-- - with "send to all hubs" (the default), also every other hub set with your pilot token (from your settings
--   page there). A hub without a token is skipped then: it only takes traps from players on its own servers.
-- - a hub that didn't answer while the approach was flown (e.g. it was down) might be the server's: it gets the
--   approach too, and decides (it recognises players from its own servers; if it doesn't, that's that).
-- Uploads use plain HTTP (DCS's Lua has no HTTPS) and never block the game: the socket is driven a little
-- each frame, in missions and in the menus (DCS's UpdateManager), so what's left when a mission ends is sent
-- straight away. After a mission, DCS's own grades from debrief.log are added to its approaches and sent again.
-- Files that were sent go to Logs/dcs-lso/sent/; ones no hub took after a day, to unsent/. The queue and a
-- "Send now" button are on the settings page (DCSLSO_PILOT.status / .retry, or status.txt and retry.flag).

package.path = package.path .. ';.\\LuaSocket\\?.lua;'
package.cpath = package.cpath .. ';.\\LuaSocket\\?.dll;'

local socket = require('socket')
local lfs = require('lfs')

local VERSION = 4
local HUB_SLOTS = 3
local SCAN_EVERY_S = 2          -- look for new approach files
local HERE_EVERY_S = 60         -- ask the hubs again whether we're on one of their servers
local RETRY_S = 30              -- after a network error
local REQUEST_TIMEOUT_S = 30
local GIVE_UP_S = 24 * 3600
local CALLS_WAIT_S = 90         -- relaying calls: how long other hubs wait for the server's hub to have them
local CALLS_POLL_S = 10
local DEBRIEF_EVERY_S = 5       -- after a mission: look for its debrief.log
local GRADE_AFTER_S = 15        -- DCS's grade can come this long after the approach's last sample

local dir = lfs.writedir() .. 'Logs/dcs-lso/'
local hubs = {}                 -- { url, host, port, path, token, ip, here, answered, here_at, retry_at }
local context = {}              -- ucid, name, server, mission, started (this session)
local previous = nil            -- the last session's context (files written as it ended), kept in session.txt
local files = {}                -- name -> { done = { [hub index] = true }, here/answered = { [url] = true } | nil, first_seen }
local request                   -- the one HTTP request in flight
local last_scan, send_to_all = -SCAN_EVERY_S, false
local last_debrief_check, debrief_seen = -DEBRIEF_EVERY_S, nil
local last_status
local update_noted              -- a hub said a newer pilot hook is out (logged once)

local function note(msg) log.write('DCSLSO-PILOT', log.INFO, msg) end

local function now() return socket.gettime() end

local function option(key)
  local ok, value = pcall(function() return require('optionsEditor').getOption('plugins.DCS-LSO.' .. key) end)
  if ok then return value end
end

-- "http://lso.example.com:8000/base" -> host, port, path prefix. https:// is sent as plain HTTP on port 80.
local function parse_url(url)
  url = (url or ''):gsub('^%s+', ''):gsub('%s+$', '')
  if url == '' then return nil end
  local rest = url:gsub('^https?://', '')
  local hostport, path = rest:match('^([^/]+)(.*)$')
  if not hostport then return nil end
  local host, port = hostport:match('^(.-):(%d+)$')
  host, port = host or hostport, tonumber(port) or 80
  return host, port, (path:gsub('/+$', ''))
end

local function load_settings()
  hubs = {}
  send_to_all = option('sendToAll') ~= false  -- on unless switched off
  for i = 1, HUB_SLOTS do
    local url = option('hub' .. i .. 'Url')
    local host, port, path = parse_url(url)
    if host then
      local token = option('hub' .. i .. 'Token')
      token = token and token:gsub('%s+', '') or ''
      hubs[#hubs + 1] = { url = url, host = host, port = port, path = path, token = token ~= '' and token or nil,
                          here = false, answered = false, here_at = -HERE_EVERY_S, retry_at = 0 }
    end
  end
  note(string.format('%d hub(s) set; sending to %s', #hubs, send_to_all and 'all of them' or "the current server's hub"))
end

local function json_string(s)
  return '"' .. tostring(s or ''):gsub('[%c"\\]', function(c)
    if c == '\n' then return '\\n' end
    if c == '"' or c == '\\' then return '\\' .. c end
    return string.format('\\u%04x', c:byte())
  end) .. '"'
end

-- An HTTP request driven without blocking: call step() each frame until it returns true.
local function new_request(hub, method, target, body, on_done, without_token)
  if not hub.ip then
    local ip = socket.dns.toip(hub.host)  -- resolved once per hub (when settings are loaded)
    if not ip then on_done(nil, 'cannot resolve ' .. hub.host) return nil end
    hub.ip = ip
  end
  local headers = { method .. ' ' .. hub.path .. target .. ' HTTP/1.1', 'Host: ' .. hub.host,
                    'User-Agent: dcs-lso-pilot-hook/' .. VERSION, 'Connection: close' }
  if hub.token and not without_token then headers[#headers + 1] = 'Authorization: Bearer ' .. hub.token end
  if body then
    headers[#headers + 1] = 'Content-Type: application/json'
    headers[#headers + 1] = 'Content-Length: ' .. #body
  end
  local data = table.concat(headers, '\r\n') .. '\r\n\r\n' .. (body or '')
  local c = socket.tcp()
  c:settimeout(0)
  c:connect(hub.ip, hub.port)  -- in progress
  local r = { conn = c, data = data, sent = 0, received = {}, started = now(), on_done = on_done }
  function r.step()
    if now() - r.started > REQUEST_TIMEOUT_S then
      c:close(); r.on_done(nil, 'timed out') return true
    end
    if r.sent < #r.data then
      local _, writable = socket.select(nil, { c }, 0)
      if not writable or #writable == 0 then return false end
      local last, err, partial = c:send(r.data, r.sent + 1)
      r.sent = last or partial or r.sent
      if err and err ~= 'timeout' then c:close(); r.on_done(nil, err) return true end
      return false
    end
    local chunk, err, partial = c:receive('*a')
    if chunk then r.received[#r.received + 1] = chunk end
    if partial and partial ~= '' then r.received[#r.received + 1] = partial end
    if err == 'timeout' then return false end
    c:close()
    local response = table.concat(r.received)
    local status = tonumber(response:match('^HTTP/%d%.%d (%d%d%d)'))
    if not status then r.on_done(nil, err or 'no response') return true end
    r.on_done(status, response:match('\r\n\r\n(.*)$') or '')
    return true
  end
  return r
end

local function read_file(path)
  local f = io.open(path, 'r')
  if not f then return nil end
  local text = f:read('*a')
  f:close()
  local meta, csv, carrier = {}, {}, {}
  local into = csv
  for line in text:gmatch('[^\n]+') do
    local key, value = line:match('^# ([%w_]+)=(.*)$')
    if line == '## carrier' then into = carrier
    elseif key then meta[key] = value
    elseif not line:match('^#') then into[#into + 1] = line end
  end
  return meta, table.concat(csv, '\n'), table.concat(carrier, '\n')
end

-- The session (mission, account, server) saved to a file, so approaches written as a mission ends (the
-- recorder's last flush comes after the uploader stops) are stamped correctly in the next session, even after
-- DCS restarts.
local KEYS = { 'mission', 'ucid', 'name', 'server', 'started', 'here', 'answered' }

local function save_session(ctx)
  pcall(lfs.mkdir, dir)
  local f = io.open(dir .. 'session.txt', 'w')
  if not f then return end
  for _, key in ipairs(KEYS) do
    if ctx[key] then f:write(key, '=', (tostring(ctx[key]):gsub('[\r\n]', ' ')), '\n') end
  end
  f:close()
end

local function load_session()
  local f = io.open(dir .. 'session.txt', 'r')
  if not f then return nil end
  local ctx = {}
  for line in f:lines() do
    local key, value = line:match('^([%w_]+)=(.*)$')
    if key then ctx[key] = value end
  end
  f:close()
  ctx.started = tonumber(ctx.started)
  return ctx.mission and ctx or nil
end

-- The sun's elevation (degrees) where and when an approach ended, as DCS itself works it out (the mission
-- editor's own call): the hub marks night passes with it when it doesn't know the mission's start in UTC (no
-- server agent of its own). Only for the mission running now.
local function sun_elevation(lat, lon, model_time)
  if not (DCS.getCurrentMission and DCS.getSunAzimuthElevation and lat and lon and model_time) then return nil end
  local current = DCS.getCurrentMission()
  local mission = current and current.mission
  if not (mission and mission.date and mission.start_time) then return nil end
  local summer = 0
  pcall(function() summer = tonumber(require('terrain').GetTerrainConfig('SummerTimeDelta')) or 0 end)
  local seconds = mission.start_time + model_time - summer * 3600
  local days = math.floor(seconds / 86400)  -- past midnight: the next day
  local date = os.date('*t', os.time({ year = mission.date.Year, month = mission.date.Month,
                                       day = mission.date.Day + days, hour = 12 }))
  local ok, _, elevation = pcall(DCS.getSunAzimuthElevation, lat, lon, date.year, date.month, date.day,
                                 seconds - days * 86400)
  if ok then return tonumber(elevation) end
end

-- The hubs that said "you're on one of our servers" this session, as "url|url" (kept with the session and each
-- file, so files sent after leaving the server still go to that server's hub).
local function here_list()
  local urls = {}
  for _, hub in ipairs(hubs) do if hub.here then urls[#urls + 1] = hub.url end end
  return table.concat(urls, '|')
end

-- The hubs that answered whether we're on one of their servers (yes or no) this session, as "url|url".
local function answered_list()
  local urls = {}
  for _, hub in ipairs(hubs) do if hub.answered then urls[#urls + 1] = hub.url end end
  return table.concat(urls, '|')
end

local function here_set(text)
  local set = {}
  for url in (text or ''):gmatch('[^|]+') do set[url] = true end
  return set
end

-- The session an approach file was flown in, written into the file when the uploader first sees it (so
-- one uploaded in a later session still names its own mission, server and account).
local function stamp(name)
  local path = dir .. name
  local meta = read_file(path)
  if not meta or meta.mission then return end
  local written = tonumber(meta.written_at) or os.time()
  local current = context.started and written >= context.started - 60
  local ctx = current and context or previous
  if not ctx or not ctx.mission then return end
  local elevation
  if current then
    local _, csv = read_file(path)
    local last = csv:match('([^\n]+)$') or ''
    local fields = {}
    for v in last:gmatch('[^,]+') do fields[#fields + 1] = tonumber(v) end
    elevation = sun_elevation(fields[9], fields[10], tonumber(meta.model_time))
  end
  local f = io.open(path, 'a')
  if not f then return end
  for _, key in ipairs({ 'mission', 'ucid', 'name', 'server' }) do
    if ctx[key] then f:write('# ', key, '=', tostring(ctx[key]):gsub('[\r\n]', ' '), '\n') end
  end
  local here = current and here_list() or ctx.here
  if here and here ~= '' then f:write('# here=', here, '\n') end
  local answered = current and answered_list() or ctx.answered
  if answered and answered ~= '' then f:write('# answered=', answered, '\n') end
  if elevation then f:write(string.format('# sun_elevation=%.2f\n', elevation)) end
  f:close()
end

local function upload_body(meta, csv, carrier, calls, here)
  return '{"version":' .. VERSION
    .. (here ~= nil and (',"here":' .. tostring(here)) or '')  -- was this hub the server's when it was flown (nil: unknown)
    .. ',"pilot":' .. json_string(meta.name or meta.pilot)
    .. ',"aircraft":' .. json_string(meta.aircraft)
    .. ',"mission":' .. json_string(meta.mission)
    .. ',"ucid":' .. json_string(meta.ucid)
    .. ',"server":' .. json_string(meta.server)
    .. ',"sent_at":' .. (tonumber(meta.written_at) or os.time())
    .. ',"sent_model_time":' .. (tonumber(meta.model_time) or 0)
    .. (tonumber(meta.sun_elevation) and (',"sun_elevation":' .. tonumber(meta.sun_elevation)) or '')
    .. ',"csv":' .. json_string(csv)
    .. (meta.dcs_grade and (',"dcs_grade":' .. json_string(meta.dcs_grade)) or '')
    .. (calls and (',"calls":' .. calls.raw .. ',"calls_from":' .. json_string(calls.from)) or '')
    .. (carrier ~= '' and meta.carrier_type and (',"carrier":{"type":' .. json_string(meta.carrier_type)
        .. ',"unit":' .. json_string(meta.carrier_unit) .. ',"csv":' .. json_string(carrier) .. '}') or '')
    .. '}'
end

local function move(name, sub)
  pcall(lfs.mkdir, dir .. sub)
  os.rename(dir .. name, dir .. sub .. '/' .. name)
  files[name] = nil
end

-- Was this hub the one for the server the file's approach was flown on? (As recorded with the file, else now.)
local function is_here(state, hub)
  if state.here then return state.here[hub.url] == true end
  return hub.here
end

-- Did this hub not answer (down, unreachable) while the file's approach was flown? Then it may be the server's
-- hub: it gets the file and decides. Files from earlier sessions without the hubs' answers (older versions)
-- count as unknown everywhere.
local function is_unknown(state, hub)
  if is_here(state, hub) then return false end
  if state.answered then return not state.answered[hub.url] end
  if state.current then return not hub.answered end
  return true
end

-- The hubs this file still has to go to: the hub of the server it was flown on first.
local function destinations(state)
  local out = {}
  for _, first in ipairs({ true, false }) do
    for i, hub in ipairs(hubs) do
      local here = is_here(state, hub)
      local wanted = here or is_unknown(state, hub) or (send_to_all and hub.token)
      if not state.done[i] and wanted and (here == first) then out[#out + 1] = i end
    end
  end
  return out
end

-- Relaying the live LSO calls: the hub of the server we flew on has them (its server agent made them), the
-- others don't. Once our upload is in there, ask it for the calls on that landing (until its server agent has
-- reported it, or CALLS_WAIT_S), and send them along to the other hubs.
local function waiting_for_calls(state, t)
  if state.calls or not state.calls_until then return false end
  return t < state.calls_until
end

local function ask_for_calls(state, t)
  local hub = hubs[state.calls_hub]
  state.calls_next = t + CALLS_POLL_S
  return new_request(hub, 'GET', string.format('/api/v1/pilot-hook/calls?pass_id=%d&ucid=%s', state.calls_pass,
                                               state.ucid or context.ucid or ''), nil, function(status, body)
    if status == 200 and body:match('"ready"%s*:%s*true') then
      state.calls = { raw = body:match('"calls"%s*:%s*(%b[])') or '[]', from = hub.host }
    elseif status and status ~= 200 then
      state.calls_until = nil  -- it can't give them: send without
    end
  end)
end

local function start_next()
  local t = now()
  -- Ask hubs whether we're on one of their servers (multiplayer only).
  if context.ucid then
    for _, hub in ipairs(hubs) do
      if t - hub.here_at >= HERE_EVERY_S and t >= hub.retry_at then
        hub.here_at = t
        return new_request(hub, 'GET', '/api/v1/pilot-hook/here?ucid=' .. context.ucid, nil, function(status, body)
          local here = status == 200 and body:match('"here"%s*:%s*true') ~= nil
          if here ~= hub.here then note(hub.url .. (here and ': you are on one of its servers' or ': not its server')) end
          hub.here, hub.answered = here, hub.answered or status == 200
          if context.started then
            context.here, context.answered = here_list(), answered_list()
            save_session(context)
            -- This session's files follow the answers as they come (their stamp was written at first sight, maybe
            -- before any hub answered): in memory, and in the file (later lines win), for when it's sent later.
            for name, state in pairs(files) do
              if state.current and state.stamp ~= context.here .. '/' .. context.answered then
                state.stamp = context.here .. '/' .. context.answered
                state.here, state.answered = here_set(context.here), here_set(context.answered)
                local f = io.open(dir .. name, 'a')
                if f then f:write('# here=', context.here, '\n# answered=', context.answered, '\n') f:close() end
              end
            end
          end
          if not status then hub.retry_at = now() + RETRY_S end
        end)
      end
    end
  end
  -- Send the next approach file to its next hub.
  for name, state in pairs(files) do
    local todo = destinations(state)
    if #todo == 0 then
      if next(state.done) ~= nil then move(name, 'sent')  -- sent wherever it should go
      elseif t - state.first_seen > GIVE_UP_S then note(name .. ': no hub took it; moved to unsent/') move(name, 'unsent') end
    else
      local here_pending = false
      for _, i in ipairs(todo) do
        -- (a hub we've stopped sending to until the next mission, e.g. a refused token, doesn't hold up the rest)
        if is_here(state, hubs[i]) and hubs[i].retry_at ~= math.huge then here_pending = true end
      end
      for _, i in ipairs(todo) do
        local hub = hubs[i]
        local wait = false
        if not is_here(state, hub) then
          if here_pending then
            wait = true  -- the server's hub first (it has the calls)
          elseif waiting_for_calls(state, t) then
            if t >= (state.calls_next or 0) then return ask_for_calls(state, t) end
            wait = true
          end
        end
        if not wait and t >= hub.retry_at then
          local meta, csv, carrier = read_file(dir .. name)
          if not meta then files[name] = nil return nil end
          if not meta.mission then return nil end  -- not stamped yet (see stamp)
          local calls = not is_here(state, hub) and state.calls or nil
          local here_flag = nil  -- unknown: the hub didn't answer while it was flown
          if not is_unknown(state, hub) then here_flag = is_here(state, hub) end
          -- Only there because it didn't answer: let it recognise us from its servers, without the token (sending
          -- with it is "send to all hubs").
          local tokenless = here_flag == nil and not (send_to_all and hub.token)
          return new_request(hub, 'POST', '/api/v1/pilot-hook/approaches', upload_body(meta, csv, carrier, calls, here_flag), function(status, body)
            if status == 200 then
              note(name .. ' -> ' .. hub.url .. ': ' .. (body:match('"text"%s*:%s*"([^"]*)"') or 'sent')
                   .. (calls and ' (with the LSO calls)' or ''))
              state.done[i] = true
              local latest = tonumber(body:match('"latest"%s*:%s*(%d+)'))
              if body:match('"update"%s*:%s*true') and latest and not update_noted then
                update_noted = true
                note(string.format('a newer pilot hook is available (version %d, this is %d): see %s', latest, VERSION,
                                   hub.url))
              end
              local pass_id = tonumber(body:match('"pass_id"%s*:%s*(%d+)'))
              if is_here(state, hub) and pass_id and not state.calls_until then
                state.calls_hub, state.calls_pass = i, pass_id
                state.calls_until, state.calls_next = now() + CALLS_WAIT_S, now()
              end
            elseif status == 403 then
              -- The hub only takes traps flown on its own servers: retrying won't change that.
              note(name .. ' -> ' .. hub.url .. ': not taken (' .. body:sub(1, 200) .. ')')
              state.done[i] = true
            elseif status == 401 and (tokenless or not hub.token) then
              -- Sent without a token because the hub didn't answer when it was flown: it doesn't know us from
              -- its servers, so it wasn't its server. Nothing more to do there.
              note(name .. ' -> ' .. hub.url .. ': not taken (not flown on its servers, and no pilot token for it)')
              state.done[i] = true
            elseif status == 401 then
              -- Not accepted (e.g. a wrong pilot token): try again next mission, after the settings are read again.
              note(name .. ' -> ' .. hub.url .. ': refused (' .. status .. ') ' .. body:sub(1, 200)
                   .. '; will try again next mission or on "Send now" (check the pilot token in Options > Special > DCS-LSO)')
              hub.retry_at = math.huge
            elseif status and status < 500 then
              note(name .. ' -> ' .. hub.url .. ': refused (' .. status .. ') ' .. body:sub(1, 200))
              state.done[i] = true  -- won't change by retrying
            else
              note(name .. ' -> ' .. hub.url .. ': ' .. tostring(status or body) .. '; retrying later')
              hub.retry_at = now() + RETRY_S
            end
          end, tokenless)
        end
      end
    end
  end
end

local function write_status(queued, oldest)
  local text = string.format('queued=%d\noldest=%s\n', queued, oldest and tostring(oldest) or '')
  if text == last_status then return end
  last_status = text
  local f = io.open(dir .. 'status.txt', 'w')
  if f then f:write(text) f:close() end
end

local function retry()
  load_settings()  -- e.g. a pilot token that was just fixed
  for _, hub in ipairs(hubs) do hub.retry_at, hub.here_at = 0, -HERE_EVERY_S end
  last_scan = -SCAN_EVERY_S
  note('sending now (asked from the settings page)')
end

local function scan()
  pcall(lfs.mkdir, dir)
  local queued, oldest = 0, nil
  local ok = pcall(function()
    for name in lfs.dir(dir) do
      if name:match('^approach%-.*%.csv$') then
        queued = queued + 1
        local written = tonumber(name:match('^approach%-(%d+)'))
        if written and (not oldest or written < oldest) then oldest = written end
        if not files[name] then
          stamp(name)
          local meta = read_file(dir .. name) or {}
          files[name] = { done = {}, first_seen = now(), here = meta.here and here_set(meta.here) or nil,
                          answered = meta.answered and here_set(meta.answered) or nil,
                          current = context.started ~= nil and (tonumber(meta.written_at) or 0) >= context.started - 60,
                          ucid = meta.ucid }
        end
      end
    end
  end)
  write_status(queued, oldest)
  if os.remove(dir .. 'retry.flag') then retry() end  -- "Send now" from a settings page that can't call us
  return ok
end

-- DCS's own LSO grades, from the debrief.log DCS writes as a mission ends: each is added to the approach it
-- belongs to (by mission time, and the pilot's name), and that approach is sent again so the hubs get it.
local function load_debrief(path)
  local f = io.open(path, 'r')
  if not f then return nil end
  local text = f:read('*a'):gsub('^\239\187\191', '')
  f:close()
  local chunk = loadstring(text)
  if not chunk then return nil end
  local env = {}
  setfenv(chunk, env)
  if not pcall(chunk) then return nil end
  return env.events
end

local function add_debrief_grades()
  local path = lfs.writedir() .. 'Logs/debrief.log'
  local attr = lfs.attributes(path)
  if not attr or attr.modification == debrief_seen then return end
  local session = previous
  if not session or not session.started or attr.modification < session.started then return end
  debrief_seen = attr.modification
  local marks = {}
  for _, e in ipairs(load_debrief(path) or {}) do
    if e.type == 'landing quality mark' and e.comment and tonumber(e.t)
        and (not session.name or e.initiatorPilotName == session.name) then
      marks[#marks + 1] = { t = tonumber(e.t), comment = tostring(e.comment) }
    end
  end
  if #marks == 0 then return end
  local added = 0
  for _, sub in ipairs({ '', 'sent/' }) do
    pcall(function()
      for name in lfs.dir(dir .. sub) do
        if name:match('^approach%-.*%.csv$') then
          local meta, csv = read_file(dir .. sub .. name)
          if meta and not meta.dcs_grade and meta.mission == session.mission
              and (tonumber(meta.written_at) or 0) >= session.started - 60 then
            local first = tonumber(csv:match('\n([%d%.%-]+),')) or 0
            local last = tonumber((csv:match('([^\n]+)$') or ''):match('^([%d%.%-]+),')) or 0
            for _, m in ipairs(marks) do
              if m.t >= first and m.t <= last + GRADE_AFTER_S then
                local out = io.open(dir .. sub .. name, 'a')
                if out then out:write('# dcs_grade=', (m.comment:gsub('[\r\n]', ' ')), '\n') out:close() end
                if sub ~= '' then os.rename(dir .. sub .. name, dir .. name) end  -- send it again
                added = added + 1
                break
              end
            end
          end
        end
      end
    end)
  end
  if added > 0 then note(string.format("DCS's grade added to %d approach(es) from debrief.log; sending again", added)) end
end

local callbacks = {}

function callbacks.onSimulationStart()
  load_settings()
  previous = load_session() or previous
  context = { mission = DCS.getMissionName and DCS.getMissionName() or '', started = os.time() }
  if DCS.isMultiplayer and DCS.isMultiplayer() then
    local me = net.get_player_info(net.get_my_player_id()) or {}
    context.ucid, context.name, context.server = me.ucid, me.name, net.get_server_host and net.get_server_host() or nil
  end
  save_session(context)
end

-- Approaches written as the mission ends belong to it: stamp them now, and keep this session for any written
-- after this (the recorder's last flush) and for its debrief.log.
function callbacks.onSimulationStop()
  pcall(scan)
  previous, context = context, {}
end

-- The uploader's work, a little at a time: from DCS's UpdateManager (every frame, in missions and in the
-- menus), or else from onSimulationFrame.
local last_tick = -1
local function tick()
  local t = now()
  last_tick = t
  if #hubs == 0 then return end
  if request then
    local ok, finished = pcall(request.step)
    if not ok or finished then request = nil end
    return
  end
  if t - last_scan >= SCAN_EVERY_S then
    last_scan = t
    scan()
  end
  if not context.started and t - last_debrief_check >= DEBRIEF_EVERY_S then
    last_debrief_check = t
    local ok, err = pcall(add_debrief_grades)
    if not ok then note('debrief.log: ' .. tostring(err)) end
  end
  local ok, r = pcall(start_next)
  if ok then request = r else note('uploader error: ' .. tostring(r)) end
end

function callbacks.onSimulationFrame()
  if now() - last_tick > 1 then tick() end  -- UpdateManager isn't driving us
end

-- For the settings page (same Lua state): the queue, and "Send now".
DCSLSO_PILOT = {
  status = function()
    local text = last_status or ''
    return tonumber(text:match('queued=(%d+)')) or 0, tonumber(text:match('oldest=(%d+)'))
  end,
  retry = retry,
}

DCS.setUserCallbacks(callbacks)
load_settings()
previous = load_session()
local ok, UpdateManager = pcall(require, 'UpdateManager')
if ok and type(UpdateManager) == 'table' and UpdateManager.add then
  UpdateManager.add(function() pcall(tick) end)  -- returns nothing: stays registered
  note('pilot hook loaded (sending in missions and menus)')
else
  note('pilot hook loaded (sending in missions only)')
end
