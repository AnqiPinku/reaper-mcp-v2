--[[
  REAPER MCP Bridge (v2)
  ----------------------
  Runs *inside* REAPER as a deferred ReaScript. Talks to the Python MCP
  server over a tiny single-file IPC protocol:

      server  ->  writes  %APPDATA%/reaper-mcp-ipc/request.json   {id, func, args|code}
      bridge  ->  writes  %APPDATA%/reaper-mcp-ipc/response.json   {id, ok, ret|error}

  Improvements over the original bridge:
    * One file_exists() check per tick instead of scanning request_1..1000.
    * A correct recursive-descent JSON parser/encoder (no Windows-path bug).
    * REAPER pointers round-trip via a validated handle registry, so results
      from one call can be passed straight into the next.
    * Generic dispatch: ANY reaper.* function is callable by name. A curated
      DSL adds ergonomic, musical-time helpers. run_lua executes arbitrary code.
    * Quiet by default (set DEBUG=true for a console trace).

  Install: load this script in REAPER (Actions > Load ReaScript) and run it.
  Keep it running while you use the MCP server. Re-run to restart.
]]--

local DEBUG = false
local POLL  = 0.03 -- seconds between polls (defer is frame-bound anyway)

-- IPC directory: a FIXED, ASCII-only path under %APPDATA% so a Chinese (or any
-- non-ASCII) install/project path can never corrupt the file channel. The
-- Python server derives the exact same path independently, so the two sides
-- always meet in the same "mailbox" regardless of where REAPER lives.
local function ipc_dir()
  local base = os.getenv('APPDATA')                          -- Windows
            or os.getenv('XDG_DATA_HOME')                    -- Linux
            or (os.getenv('HOME') and os.getenv('HOME') .. '/.local/share')
  if base and base ~= '' then
    local sep = base:find('\\') and '\\' or '/'
    return base .. sep .. 'reaper-mcp-ipc' .. sep
  end
  return reaper.GetResourcePath() .. '/reaper-mcp-ipc/'      -- last resort
end
local bridge_dir = ipc_dir()
local REQ  = bridge_dir .. 'request.json'
local RESP = bridge_dir .. 'response.json'
local TMP  = bridge_dir .. 'response.tmp'

local function log(s) if DEBUG then reaper.ShowConsoleMsg(s .. '\n') end end

----------------------------------------------------------------------
-- JSON  (correct recursive-descent; handles escapes, \uXXXX, nesting)
----------------------------------------------------------------------
local json = {}
do
  local escape_map = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
  }
  local function esc_str(s)
    return '"' .. s:gsub('[%z\1-\31\\"]', function(c)
      return escape_map[c] or string.format('\\u%04x', string.byte(c))
    end) .. '"'
  end

  local encode -- fwd decl
  local function is_array(t)
    local n = 0
    for k in pairs(t) do
      if type(k) ~= 'number' then return false end
      n = n + 1
    end
    return n == #t
  end

  -- marshal_ret hook is set later so userdata becomes a handle.
  json.userdata_hook = function(_) return nil end

  encode = function(v, seen)
    local tv = type(v)
    if v == nil then return 'null'
    elseif tv == 'boolean' then return tostring(v)
    elseif tv == 'number' then
      if v ~= v or v == math.huge or v == -math.huge then return 'null' end
      if v == math.floor(v) and math.abs(v) < 1e15 then
        return string.format('%d', v)
      end
      return string.format('%.10g', v)
    elseif tv == 'string' then return esc_str(v)
    elseif tv == 'userdata' then
      return encode(json.userdata_hook(v), seen)
    elseif tv == 'table' then
      seen = seen or {}
      if seen[v] then return 'null' end
      seen[v] = true
      local out
      if is_array(v) then
        local parts = {}
        for i = 1, #v do parts[i] = encode(v[i], seen) end
        out = '[' .. table.concat(parts, ',') .. ']'
      else
        local parts = {}
        for k, val in pairs(v) do
          parts[#parts + 1] = esc_str(tostring(k)) .. ':' .. encode(val, seen)
        end
        out = '{' .. table.concat(parts, ',') .. '}'
      end
      seen[v] = nil
      return out
    else
      return 'null'
    end
  end
  json.encode = function(v) return encode(v, nil) end

  -- decoder
  local function decode(s, i)
    -- skip ws
    local function ws(j) return s:find('[^ \t\r\n]', j) or (#s + 1) end
    i = ws(i)
    local c = s:sub(i, i)
    if c == '{' then
      local obj = {}
      i = ws(i + 1)
      if s:sub(i, i) == '}' then return obj, i + 1 end
      while true do
        i = ws(i)
        local key, val
        key, i = decode(s, i)          -- key is a string
        i = ws(i)
        assert(s:sub(i, i) == ':', 'expected :')
        val, i = decode(s, i + 1)
        obj[key] = val
        i = ws(i)
        local ch = s:sub(i, i)
        if ch == ',' then i = i + 1
        elseif ch == '}' then return obj, i + 1
        else error('expected , or }') end
      end
    elseif c == '[' then
      local arr = {}
      i = ws(i + 1)
      if s:sub(i, i) == ']' then return arr, i + 1 end
      while true do
        local val
        val, i = decode(s, i)
        arr[#arr + 1] = val
        i = ws(i)
        local ch = s:sub(i, i)
        if ch == ',' then i = i + 1
        elseif ch == ']' then return arr, i + 1
        else error('expected , or ]') end
      end
    elseif c == '"' then
      local buf, j = {}, i + 1
      while true do
        local ch = s:sub(j, j)
        if ch == '' then error('unterminated string') end
        if ch == '"' then return table.concat(buf), j + 1 end
        if ch == '\\' then
          local e = s:sub(j + 1, j + 1)
          if e == 'n' then buf[#buf+1] = '\n'
          elseif e == 't' then buf[#buf+1] = '\t'
          elseif e == 'r' then buf[#buf+1] = '\r'
          elseif e == 'b' then buf[#buf+1] = '\b'
          elseif e == 'f' then buf[#buf+1] = '\f'
          elseif e == '/' then buf[#buf+1] = '/'
          elseif e == '"' then buf[#buf+1] = '"'
          elseif e == '\\' then buf[#buf+1] = '\\'
          elseif e == 'u' then
            local hex = s:sub(j + 2, j + 5)
            local cp = tonumber(hex, 16) or 0
            -- minimal UTF-8 encode of BMP code point
            if cp < 0x80 then
              buf[#buf+1] = string.char(cp)
            elseif cp < 0x800 then
              buf[#buf+1] = string.char(0xC0 + math.floor(cp/0x40), 0x80 + cp%0x40)
            else
              buf[#buf+1] = string.char(
                0xE0 + math.floor(cp/0x1000),
                0x80 + math.floor(cp/0x40)%0x40,
                0x80 + cp%0x40)
            end
            j = j + 4
          else buf[#buf+1] = e end
          j = j + 2
        else
          buf[#buf+1] = ch
          j = j + 1
        end
      end
    elseif c:match('[%d%-]') then
      local num = s:match('^%-?%d+%.?%d*[eE]?[%+%-]?%d*', i)
      return tonumber(num), i + #num
    elseif s:sub(i, i + 3) == 'true' then return true, i + 4
    elseif s:sub(i, i + 4) == 'false' then return false, i + 5
    elseif s:sub(i, i + 3) == 'null' then return nil, i + 4
    else error('unexpected char at ' .. i .. ': ' .. c) end
  end
  json.decode = function(s)
    if not s or s == '' then return nil end
    local ok, v = pcall(function() local r = decode(s, 1) return r end)
    if ok then return v else return nil, v end
  end
end

----------------------------------------------------------------------
-- File helpers (atomic write via temp + rename)
----------------------------------------------------------------------
local function read_file(p)
  local f = io.open(p, 'rb'); if not f then return nil end
  local c = f:read('*a'); f:close(); return c
end
local function write_atomic(final, content, tmp)
  tmp = tmp or (final .. '.tmp')
  local f = io.open(tmp, 'wb'); if not f then return false end
  f:write(content); f:close()
  os.remove(final)
  local ok = os.rename(tmp, final)
  if not ok then -- fallback: direct write
    local g = io.open(final, 'wb'); if g then g:write(content); g:close() end
    os.remove(tmp)
  end
  return true
end
local function file_exists(p)
  local f = io.open(p, 'rb'); if f then f:close(); return true end
  return false
end

----------------------------------------------------------------------
-- Handle registry: userdata <-> stable string id, with validation
----------------------------------------------------------------------
local handles, handle_rev, handle_seq = {}, {}, 0
local function to_handle(ud)
  local key = tostring(ud)
  if handle_rev[key] then return handle_rev[key] end
  handle_seq = handle_seq + 1
  local id = 'h' .. handle_seq
  handles[id] = ud
  handle_rev[key] = id
  return id
end
json.userdata_hook = function(ud) return { __handle = to_handle(ud) } end

-- Recursively convert incoming args: {__handle=...} -> userdata
local function unmarshal(v)
  if type(v) == 'table' then
    if v.__handle ~= nil then
      local ud = handles[v.__handle]
      if ud == nil then error('unknown handle: ' .. tostring(v.__handle)) end
      return ud
    end
    for k, val in pairs(v) do v[k] = unmarshal(val) end
    return v
  end
  return v
end

----------------------------------------------------------------------
-- DSL: ergonomic, musical-time helpers. Indices are 0-based (REAPER native).
-- Every DSL fn returns a table that becomes the response body (ok added later).
----------------------------------------------------------------------
local function track_at(i)
  local t = reaper.GetTrack(0, i)
  if not t then error('no track at index ' .. tostring(i)) end
  return t
end
local function beats_to_time(b) return reaper.TimeMap2_beatsToTime(0, b, -1) end
local function time_to_beats(t)
  local _, _, _, fullbeats = reaper.TimeMap2_timeToBeats(0, t)
  return fullbeats
end

local DSL = {}

function DSL.ping()
  return { ret = 'pong', version = reaper.GetAppVersion(),
           api = reaper.GetAppVersion() }
end

function DSL.get_project_summary()
  local n = reaper.CountTracks(0)
  local tracks = {}
  for i = 0, n - 1 do
    local t = reaper.GetTrack(0, i)
    local _, name = reaper.GetTrackName(t)
    local vol = reaper.GetMediaTrackInfo_Value(t, 'D_VOL')
    local mute = reaper.GetMediaTrackInfo_Value(t, 'B_MUTE') == 1
    local solo = reaper.GetMediaTrackInfo_Value(t, 'I_SOLO') ~= 0
    tracks[#tracks + 1] = {
      index = i, name = name,
      volume_db = vol > 0 and 20 * math.log(vol) / math.log(10) or -150,
      mute = mute, solo = solo,
      item_count = reaper.CountTrackMediaItems(t),
      fx_count = reaper.TrackFX_GetCount(t),
    }
  end
  local bpm = reaper.Master_GetTempo()
  local pos = reaper.GetPlayState() & 1 == 1 and reaper.GetPlayPosition()
              or reaper.GetCursorPosition()
  local _, proj = reaper.EnumProjects(-1, '')
  return { ret = {
    project = proj ~= '' and proj or '(unsaved)',
    tempo_bpm = bpm,
    play_state = reaper.GetPlayState(),
    cursor_seconds = reaper.GetCursorPosition(),
    play_seconds = pos,
    track_count = n,
    tracks = tracks,
  } }
end

function DSL.list_tracks()
  return { ret = DSL.get_project_summary().ret.tracks }
end

function DSL.add_track(name, index)
  index = index or reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(index, true)
  reaper.TrackList_AdjustWindows(false)
  local t = reaper.GetTrack(0, index)
  if name and name ~= '' then
    reaper.GetSetMediaTrackInfo_String(t, 'P_NAME', name, true)
  end
  reaper.UpdateArrange()
  return { ret = { index = index, name = name or '' } }
end

function DSL.delete_track(index)
  reaper.DeleteTrack(track_at(index))
  reaper.UpdateArrange()
  return { ret = true }
end

-- update_track(index, {name=, volume_db=, pan=, mute=, solo=, color=0xRRGGBB})
function DSL.update_track(index, props)
  local t = track_at(index)
  props = props or {}
  if props.name ~= nil then
    reaper.GetSetMediaTrackInfo_String(t, 'P_NAME', tostring(props.name), true)
  end
  if props.volume_db ~= nil then
    reaper.SetMediaTrackInfo_Value(t, 'D_VOL', 10 ^ (props.volume_db / 20))
  end
  if props.pan ~= nil then
    reaper.SetMediaTrackInfo_Value(t, 'D_PAN', props.pan)
  end
  if props.mute ~= nil then
    reaper.SetMediaTrackInfo_Value(t, 'B_MUTE', props.mute and 1 or 0)
  end
  if props.solo ~= nil then
    reaper.SetMediaTrackInfo_Value(t, 'I_SOLO', props.solo and 1 or 0)
  end
  if props.color ~= nil then
    reaper.SetTrackColor(t, reaper.ColorToNative(
      (props.color >> 16) & 255, (props.color >> 8) & 255, props.color & 255))
  end
  reaper.UpdateArrange()
  return { ret = true }
end

function DSL.set_tempo(bpm)
  reaper.SetCurrentBPM(0, bpm, true)
  return { ret = bpm }
end
function DSL.get_tempo() return { ret = reaper.Master_GetTempo() } end

function DSL.set_time_signature(num, denom)
  reaper.SetTempoTimeSigMarker(0, -1, 0, -1, -1, reaper.Master_GetTempo(),
    num, denom, false)
  reaper.UpdateTimeline()
  return { ret = { num = num, denom = denom } }
end

-- create_midi_item(track_index, start_beats, length_beats)
function DSL.create_midi_item(ti, start_beats, length_beats)
  local t = track_at(ti)
  local s = beats_to_time(start_beats or 0)
  local e = beats_to_time((start_beats or 0) + (length_beats or 4))
  local item = reaper.CreateNewMIDIItemInProj(t, s, e, false)
  local n = reaper.CountTrackMediaItems(t)
  -- find item index
  local idx = -1
  for i = 0, n - 1 do
    if reaper.GetTrackMediaItem(t, i) == item then idx = i break end
  end
  reaper.UpdateArrange()
  return { ret = { item_index = idx, handle = to_handle(item),
                   start_beats = start_beats or 0,
                   length_beats = length_beats or 4 } }
end

-- add_midi_notes(track_index, item_index, notes[])
--   note = {pitch, start_beats, length_beats, velocity=96, channel=0}
function DSL.add_midi_notes(ti, ii, notes)
  local t = track_at(ti)
  local item = reaper.GetTrackMediaItem(t, ii)
  if not item then error('no item at index ' .. tostring(ii)) end
  local take = reaper.GetActiveTake(item)
  if not take or not reaper.TakeIsMIDI(take) then error('item take is not MIDI') end
  local count = 0
  for _, nt in ipairs(notes or {}) do
    -- start_beats / length_beats are absolute project quarter-notes
    local start_qn = nt.start_beats
    local end_qn = nt.start_beats + (nt.length_beats or 1)
    local sppq = reaper.MIDI_GetPPQPosFromProjQN(take, start_qn)
    local eppq = reaper.MIDI_GetPPQPosFromProjQN(take, end_qn)
    reaper.MIDI_InsertNote(take, false, false, sppq, eppq,
      nt.channel or 0, nt.pitch, nt.velocity or 96, true)
    count = count + 1
  end
  reaper.MIDI_Sort(take)
  reaper.UpdateArrange()
  return { ret = { inserted = count } }
end

function DSL.get_midi_notes(ti, ii)
  local t = track_at(ti)
  local item = reaper.GetTrackMediaItem(t, ii)
  if not item then error('no item at index ' .. tostring(ii)) end
  local take = reaper.GetActiveTake(item)
  if not take or not reaper.TakeIsMIDI(take) then error('item take is not MIDI') end
  local _, noteCount = reaper.MIDI_CountEvts(take)
  local notes = {}
  for i = 0, noteCount - 1 do
    local _, sel, muted, sppq, eppq, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
    local sqn = reaper.MIDI_GetProjQNFromPPQPos(take, sppq)
    local eqn = reaper.MIDI_GetProjQNFromPPQPos(take, eppq)
    notes[#notes + 1] = {
      index = i, pitch = pitch, velocity = vel, channel = chan,
      selected = sel, muted = muted,
      start_beats = sqn, length_beats = eqn - sqn,
    }
  end
  return { ret = notes }
end

function DSL.add_track_fx(ti, fx_name)
  local t = track_at(ti)
  local idx = reaper.TrackFX_AddByName(t, fx_name, false, 1)
  if idx < 0 then error('could not add FX: ' .. tostring(fx_name)) end
  local _, name = reaper.TrackFX_GetFXName(t, idx, '')
  return { ret = { fx_index = idx, name = name } }
end

function DSL.list_track_fx(ti)
  local t = track_at(ti)
  local n = reaper.TrackFX_GetCount(t)
  local fx = {}
  for i = 0, n - 1 do
    local _, name = reaper.TrackFX_GetFXName(t, i, '')
    fx[#fx + 1] = {
      index = i, name = name,
      enabled = reaper.TrackFX_GetEnabled(t, i),
      num_params = reaper.TrackFX_GetNumParams(t, i),
    }
  end
  return { ret = fx }
end

-- set_fx_param(track_index, fx_index, param, value)  param = index|name
function DSL.set_fx_param(ti, fxi, param, value)
  local t = track_at(ti)
  local pidx = param
  if type(param) == 'string' then
    pidx = -1
    for i = 0, reaper.TrackFX_GetNumParams(t, fxi) - 1 do
      local _, pn = reaper.TrackFX_GetParamName(t, fxi, i, '')
      if pn:lower() == param:lower() then pidx = i break end
    end
    if pidx < 0 then error('no param named ' .. param) end
  end
  reaper.TrackFX_SetParam(t, fxi, pidx, value)
  local v = reaper.TrackFX_GetParam(t, fxi, pidx)
  return { ret = { param_index = pidx, value = v } }
end

function DSL.get_fx_params(ti, fxi)
  local t = track_at(ti)
  local out = {}
  for i = 0, reaper.TrackFX_GetNumParams(t, fxi) - 1 do
    local _, pn = reaper.TrackFX_GetParamName(t, fxi, i, '')
    local val, mn, mx = reaper.TrackFX_GetParam(t, fxi, i)
    local _, fmt = reaper.TrackFX_GetFormattedParamValue(t, fxi, i, '')
    out[#out + 1] = { index = i, name = pn, value = val,
                      min = mn, max = mx, formatted = fmt }
  end
  return { ret = out }
end

-- transport(action) action: play|stop|pause|record|toggle_repeat|goto_start
function DSL.transport(action)
  if action == 'play' then reaper.Main_OnCommand(1007, 0)
  elseif action == 'stop' then reaper.Main_OnCommand(1016, 0)
  elseif action == 'pause' then reaper.Main_OnCommand(1008, 0)
  elseif action == 'record' then reaper.Main_OnCommand(1013, 0)
  elseif action == 'toggle_repeat' then reaper.Main_OnCommand(1068, 0)
  elseif action == 'goto_start' then reaper.Main_OnCommand(40042, 0)
  else error('unknown transport action: ' .. tostring(action)) end
  return { ret = { play_state = reaper.GetPlayState(),
                   position = reaper.GetPlayPosition() } }
end

-- set_time_selection(start_beats, end_beats)
function DSL.set_time_selection(sb, eb)
  reaper.GetSet_LoopTimeRange(true, false, beats_to_time(sb), beats_to_time(eb), false)
  return { ret = { start_beats = sb, end_beats = eb } }
end

-- add_marker(position_beats, name, is_region, region_end_beats, color)
function DSL.add_marker(pos_beats, name, is_region, end_beats, color)
  local native_color = 0
  if color then
    native_color = reaper.ColorToNative((color>>16)&255,(color>>8)&255,color&255)|0x1000000
  end
  local idx = reaper.AddProjectMarker2(0, is_region and true or false,
    beats_to_time(pos_beats), is_region and beats_to_time(end_beats or pos_beats) or 0,
    name or '', -1, native_color)
  reaper.UpdateTimeline()
  return { ret = { marker_index = idx } }
end

function DSL.render_project(path)
  if path and path ~= '' then
    reaper.GetSetProjectInfo_String(0, 'RENDER_FILE', path, true)
  end
  reaper.Main_OnCommand(42230, 0) -- render using most recent settings
  return { ret = { rendered_to = path or '(project render path)' } }
end

-- render_to_wav(out_path, source, sample_rate)
--   source: 'time_selection' (master mix over the time selection; default)
--         | 'master'    (master mix, entire project)
--         | 'track:N'   (track N soloed through the master, 0-based)
--         | 'region:N'  (the N-th region, 0-based in time order)
-- Renders ONE stereo WAV (default bit depth) to out_path and returns the
-- absolute path actually written. The project's own render settings (and, for
-- 'track:N', every track's solo state) are saved and restored, so the user's
-- render dialog is left untouched. This is the bridge feeding audio analysis.
function DSL.render_to_wav(out_path, source, sample_rate)
  if not out_path or out_path == '' then error('out_path is required') end
  source = source or 'time_selection'
  sample_rate = sample_rate or 48000
  if not out_path:lower():match('%.wav$') then out_path = out_path .. '.wav' end
  local dir = out_path:match('^(.*)[/\\][^/\\]+$')
  if not dir then error('out_path must be an absolute path with a directory') end
  reaper.RecursiveCreateDirectory(dir, 0)
  -- REAPER needs RENDER_FILE=directory + RENDER_PATTERN=filename-stem (no ext).
  -- A full path in RENDER_FILE with an empty pattern yields an EMPTY render
  -- target (renders nothing). The real output path is read back from
  -- RENDER_TARGETS after the settings are applied.
  local stem = (out_path:match('[/\\]([^/\\]+)$')):gsub('%.[^.]+$', '')
  local actual_path = out_path

  local gp, gps = reaper.GetSetProjectInfo, reaper.GetSetProjectInfo_String
  local saved = {
    settings = gp(0, 'RENDER_SETTINGS', 0, false),
    bounds   = gp(0, 'RENDER_BOUNDSFLAG', 0, false),
    startpos = gp(0, 'RENDER_STARTPOS', 0, false),
    endpos   = gp(0, 'RENDER_ENDPOS', 0, false),
    srate    = gp(0, 'RENDER_SRATE', 0, false),
    chans    = gp(0, 'RENDER_CHANNELS', 0, false),
  }
  local _, saved_file = gps(0, 'RENDER_FILE', '', false)
  local _, saved_pat  = gps(0, 'RENDER_PATTERN', '', false)
  local _, saved_fmt  = gps(0, 'RENDER_FORMAT', '', false)
  local saved_solo = nil

  local function restore()
    gp(0, 'RENDER_SETTINGS', saved.settings, true)
    gp(0, 'RENDER_BOUNDSFLAG', saved.bounds, true)
    gp(0, 'RENDER_STARTPOS', saved.startpos, true)
    gp(0, 'RENDER_ENDPOS', saved.endpos, true)
    gp(0, 'RENDER_SRATE', saved.srate, true)
    gp(0, 'RENDER_CHANNELS', saved.chans, true)
    gps(0, 'RENDER_FILE', saved_file, true)
    gps(0, 'RENDER_PATTERN', saved_pat, true)
    gps(0, 'RENDER_FORMAT', saved_fmt, true)
    if saved_solo then
      for i, v in pairs(saved_solo) do
        local tr = reaper.GetTrack(0, i)
        if tr then reaper.SetMediaTrackInfo_Value(tr, 'I_SOLO', v) end
      end
    end
  end

  local tnum = source:match('^track:(%d+)$')
  local rnum = source:match('^region:(%d+)$')

  local ok, err = pcall(function()
    -- common: single-file master mix, WAV, stereo, requested sample rate
    gp(0, 'RENDER_SETTINGS', 0, true)
    gp(0, 'RENDER_CHANNELS', 2, true)
    gp(0, 'RENDER_SRATE', sample_rate, true)
    gps(0, 'RENDER_FORMAT', 'evaw', true)   -- WAV, default bit depth
    gps(0, 'RENDER_FILE', dir, true)
    gps(0, 'RENDER_PATTERN', stem, true)

    if source == 'master' then
      gp(0, 'RENDER_BOUNDSFLAG', 1, true)               -- entire project
    elseif source == 'time_selection' then
      local s, e = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
      if e <= s then error('no time selection set (source="time_selection")') end
      gp(0, 'RENDER_BOUNDSFLAG', 2, true)               -- time selection
    elseif tnum then
      local idx = tonumber(tnum)
      if not reaper.GetTrack(0, idx) then
        error('no track at index ' .. tostring(idx))
      end
      saved_solo = {}
      for i = 0, reaper.CountTracks(0) - 1 do
        local t = reaper.GetTrack(0, i)
        saved_solo[i] = reaper.GetMediaTrackInfo_Value(t, 'I_SOLO')
        reaper.SetMediaTrackInfo_Value(t, 'I_SOLO', i == idx and 1 or 0)
      end
      local s, e = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
      gp(0, 'RENDER_BOUNDSFLAG', (e > s) and 2 or 1, true)
    elseif rnum then
      local want, seen = tonumber(rnum), 0
      local found, rs, re = false, 0, 0
      local mi = 0
      while true do
        local retval, isrgn, pos, rgnend = reaper.EnumProjectMarkers(mi)
        if retval == 0 then break end
        if isrgn then
          if seen == want then
            found, rs, re = true, pos, rgnend
            break
          end
          seen = seen + 1
        end
        mi = mi + 1
      end
      if not found then error('no region at index ' .. tostring(want)) end
      gp(0, 'RENDER_BOUNDSFLAG', 0, true)               -- custom bounds
      gp(0, 'RENDER_STARTPOS', rs, true)
      gp(0, 'RENDER_ENDPOS', re, true)
    else
      error('unknown source: ' .. tostring(source))
    end

    local _, targets = gps(0, 'RENDER_TARGETS', '', false)
    if targets and targets ~= '' then
      actual_path = targets:match('^[^;]+') or out_path   -- first target file
    end
    os.remove(actual_path)  -- pre-delete: REAPER pops a blocking "overwrite?"
                            -- modal if the target file already exists
    reaper.Main_OnCommand(42230, 0)  -- render, auto-close dialog (synchronous)
  end)

  restore()
  if not ok then error(tostring(err)) end
  if not reaper.file_exists(actual_path) then
    error('render finished but output file not found: ' .. actual_path)
  end
  return { ret = { path = actual_path, source = source, sample_rate = sample_rate } }
end

----------------------------------------------------------------------
-- Dispatch
----------------------------------------------------------------------
local function dispatch(req)
  local func = req.func
  if func == 'run_lua' then
    local chunk, cerr = load(req.code, 'mcp_run_lua', 't',
      setmetatable({ reaper = reaper, json = json, to_handle = to_handle,
                     handles = handles }, { __index = _G }))
    if not chunk then return { ok = false, error = 'compile error: ' .. tostring(cerr) } end
    local ok, ret = pcall(chunk)
    if not ok then return { ok = false, error = 'runtime error: ' .. tostring(ret) } end
    return { ok = true, ret = ret }
  end

  local args = req.args or {}
  for i = 1, #args do args[i] = unmarshal(args[i]) end

  if DSL[func] then
    local res = DSL[func](table.unpack(args))
    res = res or {}
    if res.ok == nil then res.ok = true end
    return res
  end

  -- generic: any reaper.* function
  local fn = reaper[func]
  if type(fn) == 'function' then
    local packed = table.pack(pcall(fn, table.unpack(args)))
    if not packed[1] then
      return { ok = false, error = 'error calling ' .. func .. ': ' .. tostring(packed[2]) }
    end
    -- collect multiple return values
    local rets = {}
    for i = 2, packed.n do rets[#rets + 1] = packed[i] end
    if #rets <= 1 then return { ok = true, ret = rets[1] }
    else return { ok = true, ret = rets } end
  end

  return { ok = false, error = 'unknown function: ' .. tostring(func) }
end

----------------------------------------------------------------------
-- Main loop
----------------------------------------------------------------------
reaper.RecursiveCreateDirectory(bridge_dir, 0)
-- clear any stale files from a previous run
os.remove(REQ); os.remove(RESP); os.remove(TMP)
reaper.ShowConsoleMsg('REAPER MCP Bridge v2 ready. Bridge dir:\n  ' .. bridge_dir .. '\n')

local function tick()
  if file_exists(REQ) then
    local raw = read_file(REQ)
    os.remove(REQ)
    if raw then
      log('REQ: ' .. raw)
      local req, derr = json.decode(raw)
      local resp
      if not req then
        resp = { ok = false, error = 'bad request json: ' .. tostring(derr) }
      else
        local ok, r = pcall(dispatch, req)
        if ok then resp = r
        else resp = { ok = false, error = 'bridge error: ' .. tostring(r) } end
        resp.id = req.id
      end
      local out = json.encode(resp)
      log('RESP: ' .. out)
      write_atomic(RESP, out, TMP)
    end
  end
  reaper.defer(tick)
end

reaper.defer(tick)
