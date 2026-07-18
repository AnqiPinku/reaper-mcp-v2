#!/usr/bin/env python3
"""
REAPER MCP Server (zero-dependency, stdio transport)
====================================================

A Model Context Protocol server that lets an LLM client (Claude Code, Claude
Desktop, etc.) drive REAPER. It speaks newline-delimited JSON-RPC 2.0 on
stdin/stdout and talks to the in-REAPER Lua bridge over a tiny single-file
IPC protocol.

No third-party packages required -- pure Python standard library.

Pairing:
  * server  ->  writes  %APPDATA%/reaper-mcp-ipc/request.json
  * bridge  ->  writes  %APPDATA%/reaper-mcp-ipc/response.json

Run the Lua bridge (bridge/reaper_mcp_bridge.lua) inside REAPER first.

Environment:
  REAPER_MCP_IPC_DIR    Override the IPC mailbox directory. Default is
                        %APPDATA%\\reaper-mcp-ipc (a fixed ASCII path). The Lua
                        bridge uses the same default; only override if you also
                        change it on the bridge side.
  REAPER_MCP_TIMEOUT    Seconds to wait for a bridge response (default 10).
"""

import glob
import json
import os
import sys
import tempfile
import time
import uuid

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "reaper-mcp"
SERVER_VERSION = "2.0.0"

# A render is a synchronous, potentially minutes-long operation inside REAPER.
# The normal per-call timeout (seconds) would make the server report a bogus
# "timed out" while REAPER is still rendering happily, so render tools get
# their own, much longer deadline.
RENDER_TIMEOUT = float(os.environ.get("REAPER_MCP_RENDER_TIMEOUT", "300"))

# Analysis renders land in one managed temp folder and get pruned on the next
# render, so repeated listen/measure cycles never pile WAV files up anywhere.
RENDERS_DIR = os.path.join(tempfile.gettempdir(), "prism-renders")
RENDERS_KEEP = 8


def default_render_path() -> str:
    """Timestamped WAV path inside the managed folder; prunes old renders."""
    os.makedirs(RENDERS_DIR, exist_ok=True)
    old = sorted(glob.glob(os.path.join(RENDERS_DIR, "*.wav")), key=os.path.getmtime)
    for p in old[:-(RENDERS_KEEP - 1)] if len(old) >= RENDERS_KEEP else []:
        try:
            os.remove(p)
        except OSError:
            pass
    return os.path.join(
        RENDERS_DIR, time.strftime("render-%Y%m%d-%H%M%S") + ".wav")


# --------------------------------------------------------------------------
# Bridge IPC client
# --------------------------------------------------------------------------
def default_ipc_dir() -> str:
    """A fixed ASCII path that the in-REAPER Lua bridge derives identically.
    Keeping the IPC channel out of any (possibly non-ASCII) REAPER/project
    path is what makes the file transport reliable on Windows + Chinese paths.
    """
    env = os.environ.get("REAPER_MCP_IPC_DIR")
    if env:
        return env
    base = os.environ.get("APPDATA")  # Windows: C:\Users\<user>\AppData\Roaming
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or \
            os.path.expanduser("~/.local/share")
    return os.path.join(base, "reaper-mcp-ipc")


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self):
        self.dir = default_ipc_dir()
        self.req = os.path.join(self.dir, "request.json")
        self.resp = os.path.join(self.dir, "response.json")
        self.timeout = float(os.environ.get("REAPER_MCP_TIMEOUT", "10"))
        os.makedirs(self.dir, exist_ok=True)

    def call(self, func: str, args=None, code: str = None,
             timeout: float = None) -> object:
        """Send one request to the bridge and block for its response.

        `timeout` overrides the default per-call deadline; render tools pass a
        long one because a bounce can legitimately run for minutes.
        """
        rid = uuid.uuid4().hex[:12]
        payload = {"id": rid, "func": func}
        if code is not None:
            payload["code"] = code
        else:
            payload["args"] = args or []

        # clear any stale response, then write request atomically
        try:
            os.remove(self.resp)
        except OSError:
            pass
        tmp = self.req + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self.req)

        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while time.time() < deadline:
            if os.path.exists(self.resp):
                try:
                    with open(self.resp, "r", encoding="utf-8") as f:
                        raw = f.read()
                    data = json.loads(raw)
                except (OSError, ValueError):
                    time.sleep(0.01)  # mid-write; retry
                    continue
                if data.get("id") not in (rid, None):
                    # stale response from an earlier call; ignore
                    time.sleep(0.01)
                    continue
                try:
                    os.remove(self.resp)
                except OSError:
                    pass
                if not data.get("ok", False):
                    raise BridgeError(data.get("error", "unknown bridge error"))
                return data.get("ret")
            time.sleep(0.015)

        raise BridgeError(
            "Timed out waiting for REAPER. Is REAPER open with "
            "reaper_mcp_bridge.lua running? (Actions > load/run the script.)"
        )


# --------------------------------------------------------------------------
# Tool definitions
# Each tool: name, description, JSON schema, and a builder that turns the
# validated argument dict into a Bridge.call(...) invocation.
# --------------------------------------------------------------------------
TOOLS = []


def tool(name, description, schema, builder):
    TOOLS.append({
        "name": name,
        "description": description,
        "inputSchema": schema,
        "_builder": builder,
    })


def obj(props, required=None):
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


tool(
    "reaper_status",
    "Check the bridge connection and return a summary of the open REAPER "
    "project (tempo, play state, and all tracks). Call this first to confirm "
    "REAPER is reachable.",
    obj({}),
    lambda b, a: b.call("get_project_summary"),
)

tool(
    "transport",
    "Control playback. action is one of: play, stop, pause, record, "
    "toggle_repeat, goto_start.",
    obj({"action": {"type": "string",
                    "enum": ["play", "stop", "pause", "record",
                             "toggle_repeat", "goto_start"]}},
        ["action"]),
    lambda b, a: b.call("transport", [a["action"]]),
)

tool(
    "list_tracks",
    "List every track in the project with index, name, volume (dB), "
    "mute/solo state, item count and FX count.",
    obj({}),
    lambda b, a: b.call("list_tracks"),
)

tool(
    "add_track",
    "Insert a new track. Optionally give it a name and an insert index "
    "(0-based; default appends at the end).",
    obj({"name": {"type": "string"},
         "index": {"type": "integer", "minimum": 0}}),
    lambda b, a: b.call("add_track", [a.get("name"), a.get("index")]),
)

tool(
    "delete_track",
    "Delete the track at the given 0-based index.",
    obj({"index": {"type": "integer", "minimum": 0}}, ["index"]),
    lambda b, a: b.call("delete_track", [a["index"]]),
)

tool(
    "update_track",
    "Update properties of a track. Provide index plus any of: name, "
    "volume_db, pan (-1..1), mute, solo, color (0xRRGGBB integer).",
    obj({"index": {"type": "integer", "minimum": 0},
         "name": {"type": "string"},
         "volume_db": {"type": "number"},
         "pan": {"type": "number", "minimum": -1, "maximum": 1},
         "mute": {"type": "boolean"},
         "solo": {"type": "boolean"},
         "color": {"type": "integer"}},
        ["index"]),
    lambda b, a: b.call("update_track", [
        a["index"],
        {k: a[k] for k in ("name", "volume_db", "pan", "mute", "solo", "color")
         if k in a}]),
)

tool(
    "set_tempo",
    "Set the project tempo in BPM.",
    obj({"bpm": {"type": "number", "minimum": 1}}, ["bpm"]),
    lambda b, a: b.call("set_tempo", [a["bpm"]]),
)

tool(
    "set_time_signature",
    "Set the project time signature, e.g. numerator=3 denominator=4.",
    obj({"numerator": {"type": "integer", "minimum": 1},
         "denominator": {"type": "integer", "minimum": 1}},
        ["numerator", "denominator"]),
    lambda b, a: b.call("set_time_signature",
                        [a["numerator"], a["denominator"]]),
)

tool(
    "create_midi_item",
    "Create an empty MIDI item on a track. Positions are in beats "
    "(quarter notes) from the project start. Returns the item_index to use "
    "with add_midi_notes.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "start_beats": {"type": "number", "minimum": 0},
         "length_beats": {"type": "number", "minimum": 0}},
        ["track_index"]),
    lambda b, a: b.call("create_midi_item", [
        a["track_index"], a.get("start_beats", 0), a.get("length_beats", 4)]),
)

# add_midi_notes / replace_midi_notes 共用的 note 格式。刻意不设
# additionalProperties:false —— get_midi_notes 的输出（多出 index/selected 字段）
# 可以原样回灌，桥端忽略未知字段、保留 muted。
NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "pitch": {"type": "integer", "minimum": 0, "maximum": 127},
        "start_beats": {"type": "number"},
        "length_beats": {"type": "number"},
        "velocity": {"type": "integer", "minimum": 1, "maximum": 127},
        "channel": {"type": "integer", "minimum": 0, "maximum": 15},
        "muted": {"type": "boolean"},
    },
    "required": ["pitch", "start_beats"],
}

tool(
    "add_midi_notes",
    "APPEND MIDI notes to an existing MIDI item -- existing notes are kept "
    "as-is. To rewrite or correct existing notes use replace_midi_notes / "
    "update_midi_note instead (appending corrected copies duplicates notes). "
    "Each note: {pitch (0-127, 60=middle C), start_beats (absolute, from "
    "project start), length_beats, velocity (1-127, default 96), channel "
    "(0-15, default 0), muted (default false)}.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0},
         "notes": {"type": "array", "items": NOTE_SCHEMA}},
        ["track_index", "item_index", "notes"]),
    lambda b, a: b.call("add_midi_notes",
                        [a["track_index"], a["item_index"], a["notes"]]),
)

tool(
    "get_midi_notes",
    "Read all MIDI notes from an item's active take, with beats-based timing.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0}},
        ["track_index", "item_index"]),
    lambda b, a: b.call("get_midi_notes", [a["track_index"], a["item_index"]]),
)

tool(
    "update_midi_note",
    "Modify ONE existing note in place, identified by its note index from "
    "get_midi_notes. Only the provided fields change (pitch, start_beats, "
    "length_beats, velocity, channel, muted); everything else is preserved. "
    "Single REAPER undo step. Returns before/after summaries. CAUTION: notes "
    "re-sort by time after the edit, so indices may shift -- re-read "
    "get_midi_notes before further index-based edits.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0},
         "note_index": {"type": "integer", "minimum": 0},
         "pitch": {"type": "integer", "minimum": 0, "maximum": 127},
         "start_beats": {"type": "number"},
         "length_beats": {"type": "number"},
         "velocity": {"type": "integer", "minimum": 1, "maximum": 127},
         "channel": {"type": "integer", "minimum": 0, "maximum": 15},
         "muted": {"type": "boolean"}},
        ["track_index", "item_index", "note_index"]),
    lambda b, a: b.call("update_midi_note", [
        a["track_index"], a["item_index"], a["note_index"],
        {k: a[k] for k in ("pitch", "start_beats", "length_beats",
                           "velocity", "channel", "muted") if k in a}]),
)

tool(
    "delete_midi_notes",
    "Delete specific notes by their indices from get_midi_notes. "
    "DESTRUCTIVE but undoable: all deletions happen in ONE undo step, and "
    "the response includes snapshots of every deleted note plus before/after "
    "counts. Indices are resolved against the CURRENT note order -- always "
    "call get_midi_notes right before this.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0},
         "note_indices": {"type": "array",
                          "items": {"type": "integer", "minimum": 0},
                          "minItems": 1}},
        ["track_index", "item_index", "note_indices"]),
    lambda b, a: b.call("delete_midi_notes",
                        [a["track_index"], a["item_index"], a["note_indices"]]),
)

tool(
    "replace_midi_notes",
    "Atomically REPLACE the entire note content of an item's active take: "
    "deletes every existing note and inserts the provided set, all in ONE "
    "undo step. Use this (not add_midi_notes, which only APPENDS and would "
    "duplicate notes) for pitch correction and bulk rewrites. You may feed "
    "get_midi_notes output straight back in (extra fields like index/"
    "selected are ignored; muted is preserved). Refuses an empty set -- to "
    "wipe an item use delete_midi_notes with all indices. Returns removed/"
    "inserted counts.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0},
         "notes": {"type": "array", "items": NOTE_SCHEMA, "minItems": 1}},
        ["track_index", "item_index", "notes"]),
    lambda b, a: b.call("replace_midi_notes",
                        [a["track_index"], a["item_index"], a["notes"]]),
)

tool(
    "add_track_fx",
    "Add an FX to a track by name (e.g. 'ReaEQ', 'ReaComp', 'VST3:Serum'). "
    "Returns its fx_index. When picking an INSTRUMENT, first discover real "
    "ones with list_installed_fx (instruments_only=true) -- Cockos ReaSynth/"
    "ReaSynDr are bare test-grade synths that sound cheap; prefer the user's "
    "installed instruments (samplers like Kontakt, real synths) whenever any "
    "exist.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_name": {"type": "string"}},
        ["track_index", "fx_name"]),
    lambda b, a: b.call("add_track_fx", [a["track_index"], a["fx_name"]]),
)

tool(
    "list_fx_presets",
    "List the preset names an FX exposes (up to 'limit', default 50) plus the "
    "current one. WARNING: reading names steps through the presets, which "
    "loads each briefly -- avoid on heavy samplers (e.g. Kontakt with big "
    "libraries); fine for synths/ReaPlugs.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_index": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1}},
        ["track_index", "fx_index"]),
    lambda b, a: b.call("list_fx_presets",
                        [a["track_index"], a["fx_index"], a.get("limit", 50)]),
)

tool(
    "set_fx_preset",
    "Switch an FX to a named preset (string, exact name from list_fx_presets) "
    "or preset index (integer). Note: Kontakt instrument libraries do NOT "
    "load through this preset system -- the user picks those in Kontakt's own "
    "window; this works for synths and plugins with normal preset lists.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_index": {"type": "integer", "minimum": 0},
         "preset": {"type": ["string", "integer"]}},
        ["track_index", "fx_index", "preset"]),
    lambda b, a: b.call("set_fx_preset",
                        [a["track_index"], a["fx_index"], a["preset"]]),
)

tool(
    "list_track_fx",
    "List the FX on a track with index, name, enabled state and param count.",
    obj({"track_index": {"type": "integer", "minimum": 0}}, ["track_index"]),
    lambda b, a: b.call("list_track_fx", [a["track_index"]]),
)

tool(
    "list_installed_fx",
    "List FX plugins INSTALLED in this REAPER (needs REAPER >= 6.37). Use it "
    "to discover what instruments/effects exist before add_track_fx -- pass "
    "the returned name straight to add_track_fx. Optional case-insensitive "
    "'filter' substring (e.g. 'kontakt', 'synth'); instruments_only limits to "
    "virtual instruments (VSTi/VST3i/CLAPi...). Returns up to 'limit' entries "
    "(default 50) plus the total match count.",
    obj({"filter": {"type": "string"},
         "instruments_only": {"type": "boolean"},
         "limit": {"type": "integer", "minimum": 1}}),
    lambda b, a: b.call("list_installed_fx",
                        [a.get("filter", ""), a.get("instruments_only", False),
                         a.get("limit", 50)]),
)

tool(
    "get_fx_params",
    "List all parameters of one FX with current value, range and a "
    "human-readable formatted value.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_index": {"type": "integer", "minimum": 0}},
        ["track_index", "fx_index"]),
    lambda b, a: b.call("get_fx_params", [a["track_index"], a["fx_index"]]),
)

tool(
    "set_fx_param",
    "Set an FX parameter. 'param' may be the parameter index (integer) or its "
    "name (string). value is the normalized 0..1 value unless the param uses a "
    "wider range (see get_fx_params).",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_index": {"type": "integer", "minimum": 0},
         "param": {"type": ["integer", "string"]},
         "value": {"type": "number"}},
        ["track_index", "fx_index", "param", "value"]),
    lambda b, a: b.call("set_fx_param",
                        [a["track_index"], a["fx_index"], a["param"], a["value"]]),
)

tool(
    "set_time_selection",
    "Set the time/loop selection range, in beats from project start.",
    obj({"start_beats": {"type": "number", "minimum": 0},
         "end_beats": {"type": "number", "minimum": 0}},
        ["start_beats", "end_beats"]),
    lambda b, a: b.call("set_time_selection",
                        [a["start_beats"], a["end_beats"]]),
)

tool(
    "add_marker",
    "Add a project marker or region. Positions are in beats. Set is_region "
    "true (with region_end_beats) for a region. color is optional 0xRRGGBB.",
    obj({"position_beats": {"type": "number", "minimum": 0},
         "name": {"type": "string"},
         "is_region": {"type": "boolean"},
         "region_end_beats": {"type": "number", "minimum": 0},
         "color": {"type": "integer"}},
        ["position_beats"]),
    lambda b, a: b.call("add_marker", [
        a["position_beats"], a.get("name", ""), a.get("is_region", False),
        a.get("region_end_beats"), a.get("color")]),
)

tool(
    "render_project",
    "Render the project using its most recent render settings. Optionally "
    "override the output file path.",
    obj({"path": {"type": "string"}}),
    lambda b, a: b.call("render_project", [a.get("path")], timeout=RENDER_TIMEOUT),
)

tool(
    "render_to_wav",
    "Render audio to a WAV file and return the absolute path actually written "
    "-- the bridge from REAPER to audio analysis. out_path is OPTIONAL: leave "
    "it out for analysis renders and the file lands in a managed temp folder "
    "that is auto-pruned (last %d kept), so nothing accumulates; only set it "
    "when the user asked to export to a specific place. source is one of: "
    "'time_selection' (master mix over the current time selection; the "
    "default), 'master' (whole-project master mix), 'track:N' (track N soloed "
    "through the master, 0-based), or 'region:N' (the N-th region, 0-based in "
    "time order). Output is stereo WAV at sample_rate (default 48000). The "
    "project's own render settings (and solo state for 'track:N') are saved "
    "and restored, so this leaves the render dialog untouched." % RENDERS_KEEP,
    obj({"out_path": {"type": "string"},
         "source": {"type": "string"},
         "sample_rate": {"type": "integer", "minimum": 8000}}),
    lambda b, a: b.call("render_to_wav",
                        [a.get("out_path") or default_render_path(),
                         a.get("source", "time_selection"),
                         a.get("sample_rate", 48000)], timeout=RENDER_TIMEOUT),
)

tool(
    "reaper_call",
    "Escape hatch: call ANY ReaScript API function by name with positional "
    "args. Example: func='CountTracks', args=[0]. Pointers returned by earlier "
    "calls come back as {\"__handle\":\"hN\"} and can be passed straight back "
    "in. Use this for the long tail of the 600+ API functions.",
    obj({"func": {"type": "string"},
         "args": {"type": "array"}},
        ["func"]),
    lambda b, a: b.call(a["func"], a.get("args", [])),
)

tool(
    "run_lua",
    "Ultimate escape hatch: execute an arbitrary Lua snippet inside REAPER and "
    "return its value. The snippet runs with `reaper` in scope; use `return` "
    "to send a value back. Example: code='return reaper.CountTracks(0)'. "
    "Prefer this for multi-step operations that would otherwise need many "
    "round-trips.",
    obj({"code": {"type": "string"}}, ["code"]),
    lambda b, a: b.call("run_lua", code=a["code"]),
)

tool(
    "batch",
    "Run several operations in ONE round-trip instead of one MCP call each. "
    "This amortizes the file-IPC latency (~1 REAPER frame per hop), so building "
    "a 16-note drum pattern, or a whole 'add track + add FX + set params' "
    "setup, costs a single hop rather than a dozen. 'calls' is an array; each "
    "item is {\"func\": <name>, \"args\": [...]} where func is any tool/DSL or "
    "ReaScript name (exactly like reaper_call), or {\"func\": \"run_lua\", "
    "\"code\": \"...\"}. Handles returned by an earlier call in the SAME batch "
    "can be passed into a later one. Returns an array of per-call results, each "
    "{\"ok\": true, \"ret\": ...} or {\"ok\": false, \"error\": ...}; one "
    "failing call does not abort the rest.",
    obj({"calls": {"type": "array", "items": obj({
        "func": {"type": "string"},
        "args": {"type": "array"},
        "code": {"type": "string"}},
        ["func"])}},
        ["calls"]),
    lambda b, a: b.call("batch", [a["calls"]]),
)


TOOL_INDEX = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# --------------------------------------------------------------------------
def make_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_request(bridge, msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return make_result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return make_result(rid, {})

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications get no response

    if method == "tools/list":
        return make_result(rid, {
            "tools": [{"name": t["name"],
                       "description": t["description"],
                       "inputSchema": t["inputSchema"]} for t in TOOLS]
        })

    if method == "resources/list":
        return make_result(rid, {"resources": []})
    if method == "prompts/list":
        return make_result(rid, {"prompts": []})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOL_INDEX.get(name)
        if not spec:
            return make_error(rid, -32602, f"unknown tool: {name}")
        try:
            ret = spec["_builder"](bridge, args)
            text = json.dumps(ret, ensure_ascii=False, indent=2) \
                if not isinstance(ret, str) else ret
            return make_result(rid, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except BridgeError as e:
            return make_result(rid, {
                "content": [{"type": "text", "text": f"REAPER error: {e}"}],
                "isError": True,
            })
        except Exception as e:  # noqa: BLE001
            return make_result(rid, {
                "content": [{"type": "text", "text": f"Server error: {e}"}],
                "isError": True,
            })

    if rid is None:
        return None  # unknown notification
    return make_error(rid, -32601, f"method not found: {method}")


def main():
    # Force UTF-8 on the stdio transport so non-ASCII track names / project
    # paths (this matters on Windows, where stdout defaults to cp1252) don't
    # crash the server.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass
    bridge = Bridge()
    stdin = sys.stdin
    out = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle_request(bridge, msg)
        except Exception as e:  # noqa: BLE001
            response = make_error(msg.get("id"), -32603, f"internal error: {e}")
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
