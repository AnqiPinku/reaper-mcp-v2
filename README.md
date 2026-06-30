# REAPER MCP (v2)

[![CI](https://github.com/AnqiPinku/reaper-mcp-v2/actions/workflows/ci.yml/badge.svg)](https://github.com/AnqiPinku/reaper-mcp-v2/actions/workflows/ci.yml)

Drive [REAPER](https://reaper.fm) from an MCP client (Claude Code, Claude
Desktop, …). Two pieces:

```
 MCP client  ──stdio JSON-RPC──►  server/reaper_mcp_server.py
                                          │  (single-file IPC)
                                          ▼
                              %APPDATA%\reaper-mcp-ipc\{request,response}.json
                                          ▲
                                          │  reaper.defer poll
                              bridge/reaper_mcp_bridge.lua  (runs inside REAPER)
```

The IPC "mailbox" lives at a **fixed ASCII path** (`%APPDATA%\reaper-mcp-ipc`,
e.g. `C:\Users\you\AppData\Roaming\reaper-mcp-ipc`). Both sides derive it
independently from `%APPDATA%`, so it does **not** depend on where REAPER is
installed — a non-ASCII install/project path can never
corrupt the file channel, and the portable vs. installed REAPER can't end up
watching different directories.

The server is **pure Python stdlib — no `pip install` needed**. The bridge is a
single self-contained Lua ReaScript.

## Why this is better than the original bridge

| | Original | v2 |
|---|---|---|
| Polling | scans `request_1.json … request_1000.json` every defer tick | one `request.json`, one `file_exists` per tick |
| JSON | hand-rolled, broke on Windows paths | correct recursive-descent parser (`\uXXXX`, nesting, control chars) |
| Dispatch | 212 hand-written `elseif` handlers + a generic fallback | curated DSL + generic `reaper.*` + `run_lua`; ~500 lines total |
| Pointers | "Cannot use track pointer from previous call" | validated **handle registry**, pointers round-trip |
| Noise | `ShowConsoleMsg` on every request | quiet by default (`DEBUG=true` to trace) |
| Server | external Python package, lost | zero-dependency stdlib, included + tested |
| Time | raw seconds/PPQ | musical **beats** in the high-level tools |

## Setup

### 1. Start the bridge in REAPER
1. Open REAPER.
2. `Actions` → `Show action list` → `New action` → `Load ReaScript…`
3. Choose `bridge/reaper_mcp_bridge.lua`, then **Run** it.
   (It registers itself and keeps running via `reaper.defer`.)
   You'll see `REAPER MCP Bridge v2 ready` in the ReaScript console once.

Re-run the action any time to restart the bridge.

### 2. Register the server with your MCP client

**Claude Code** — add to `.mcp.json` (project) or your user config:

```json
{
  "mcpServers": {
    "reaper": {
      "command": "python",
      "args": ["C:\\path\\to\\reaper-mcp-v2\\server\\reaper_mcp_server.py"]
    }
  }
}
```

Or via CLI:

```
claude mcp add reaper -- python "C:\path\to\reaper-mcp-v2\server\reaper_mcp_server.py"
```

### 3. Try it
Ask the client: *"Use reaper_status to check REAPER, then make a 4-bar
MIDI drum loop at 124 BPM."*

## Tools

High-level (think in beats; index = 0-based):
`reaper_status`, `list_tracks`, `add_track`, `delete_track`, `update_track`,
`set_tempo`, `set_time_signature`, `transport`, `create_midi_item`,
`add_midi_notes`, `get_midi_notes`, `add_track_fx`, `list_track_fx`,
`get_fx_params`, `set_fx_param`, `set_time_selection`, `add_marker`,
`render_project`.

Render to a known file (for analysis by another tool):
- `render_to_wav` — render a single stereo WAV to a given path and get the
  path back. `source` = `time_selection` (default) | `master` | `track:N`
  (soloed through master) | `region:N`. Saves/restores your render settings.

Escape hatches:
- `reaper_call` — call any one of REAPER's 600+ ReaScript functions by name.
- `run_lua` — execute an arbitrary Lua snippet inside REAPER (`return` a value).
  Best for multi-step work that would otherwise need many round-trips.

## Environment variables

| var | meaning | default |
|---|---|---|
| `REAPER_MCP_IPC_DIR` | IPC mailbox dir (must match on both sides) | `%APPDATA%\reaper-mcp-ipc` |
| `REAPER_MCP_TIMEOUT` | seconds to wait for the bridge | `10` |

## Folder Names And Non-ASCII Paths

The IPC channel is ASCII-pinned and independent of the project path, so the
folder name is cosmetic. The MCP can work with non-ASCII REAPER/project paths,
but an ASCII path for the Python server command is still the safest option on
Windows.

Recommended Windows layout:

```text
C:\MCP\reaper-mcp-v2\
  bridge\reaper_mcp_bridge.lua
  server\reaper_mcp_server.py
```

If you keep the project somewhere else, update only the server path in your MCP
client config. The IPC directory does not need to move.

## Testing

`server/test_server.py` runs the whole server against a **fake bridge** (no
REAPER needed) and checks the JSON-RPC handshake, tool list, arg marshalling,
generic call and `run_lua`:

```
python server/test_server.py
```

## Protocol (for reference)

Request (`request.json`):
```json
{ "id": "ab12cd34", "func": "add_track", "args": ["Bass", 2] }
{ "id": "ab12cd34", "func": "run_lua", "code": "return reaper.CountTracks(0)" }
```
Response (`response.json`):
```json
{ "id": "ab12cd34", "ok": true, "ret": { "index": 2, "name": "Bass" } }
{ "id": "ab12cd34", "ok": false, "error": "no track at index 9" }
```
Pointers are serialised as `{ "__handle": "h7" }` and can be passed back in.
