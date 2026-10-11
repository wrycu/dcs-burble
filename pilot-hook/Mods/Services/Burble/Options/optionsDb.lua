local DbOption = require("Options.DbOption")

-- The queue on the settings page: approaches waiting to be sent, and a "Send now" button. Asks the uploader
-- hook (Scripts/Hooks/burble-pilot-hook.lua) through BURBLE_PILOT when it shares this Lua state; otherwise
-- counts the files itself (and reads each hub's share from status.txt) and leaves retry.flag for the uploader.
local lfs = require("lfs")
local dir = lfs.writedir() .. "Logs/burble/"
local HUB_LINES = 3

-- Each hub's share of the queue, as the uploader last wrote it to status.txt.
local function hubs_from_file()
  local out = {}
  local f = io.open(dir .. "status.txt", "r")
  if not f then return out end
  for line in f:lines() do
    local waiting, problem, url = line:match("^hub=(%d+)\t([^\t]*)\t(.*)$")
    if waiting then out[#out + 1] = { url = url, waiting = tonumber(waiting), problem = problem ~= "" and problem or nil } end
  end
  f:close()
  return out
end

local function queue()
  if BURBLE_PILOT and BURBLE_PILOT.status then
    return BURBLE_PILOT.status()
  end
  local queued, oldest = 0, nil
  pcall(function()
    for name in lfs.dir(dir) do
      local written = tonumber(name:match("^approach%-(%d+)%-.*%.csv$") or "")
      if written then
        queued = queued + 1
        if not oldest or written < oldest then oldest = written end
      end
    end
  end)
  return queued, oldest, hubs_from_file()
end

-- "localhost:8000: 2 waiting (not answering)", "lso.example.com: all sent"
local function hub_text(hub)
  local url = hub.url:gsub("^https?://", ""):gsub("/+$", "")
  if hub.waiting == 0 then return url .. ": all sent" end
  return string.format("%s: %d waiting%s", url, hub.waiting, hub.problem and (" (" .. hub.problem .. ")") or "")
end

local function queue_text()
  local queued, oldest = queue()
  if queued == 0 then return "Nothing waiting to send." end
  local age = oldest and math.max(0, os.time() - oldest) or nil
  local since = ""
  if age then
    since = age < 120 and " (oldest: just now)" or age < 7200 and string.format(" (oldest: %d min ago)", age / 60)
            or string.format(" (oldest: %d h ago)", age / 3600)
  end
  return string.format("%d approach%s waiting to send%s.", queued, queued == 1 and "" or "es", since)
end

local updater, last
local function showDialog(dlg)
  local function refresh()
    if dlg.queueLabel then dlg.queueLabel:setText(queue_text()) end
    local queued, _, hubs = queue()
    for i = 1, HUB_LINES do  -- each hub's share, while anything is waiting
      local label = dlg["queueHub" .. i .. "Label"]
      local hub = queued > 0 and hubs and hubs[i]
      if label then label:setText(hub and hub_text(hub) or "") end
    end
  end
  refresh()
  if dlg.sendNowButton then
    function dlg.sendNowButton:onChange()
      if BURBLE_PILOT and BURBLE_PILOT.retry then
        BURBLE_PILOT.retry()
      else
        local f = io.open(dir .. "retry.flag", "w")
        if f then f:write("retry\n") f:close() end
      end
      refresh()
    end
  end
  local ok, UpdateManager = pcall(require, "UpdateManager")
  if ok and UpdateManager and not updater then
    updater = function()
      if not last or os.time() > last then  -- once a second while the page is open
        last = os.time()
        refresh()
      end
    end
    UpdateManager.add(updater)
  end
end

local function onClose()
  local ok, UpdateManager = pcall(require, "UpdateManager")
  if ok and UpdateManager and updater then UpdateManager.delete(updater) end
  updater = nil
end

return {
  sendToAll = DbOption.new():setValue(true):checkbox(),
  hub1Url = DbOption.new():setValue(""):editbox(),
  hub1Token = DbOption.new():setValue(""):editbox(),
  hub2Url = DbOption.new():setValue(""):editbox(),
  hub2Token = DbOption.new():setValue(""):editbox(),
  hub3Url = DbOption.new():setValue(""):editbox(),
  hub3Token = DbOption.new():setValue(""):editbox(),
  callbackOnShowDialog = showDialog,
  callbackOnClose = onClose,
}
