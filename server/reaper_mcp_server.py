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

import json
import os
import sys
import time
import uuid

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "reaper-mcp"
SERVER_VERSION = "2.0.0"


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

    def call(self, func: str, args=None, code: str = None) -> object:
        """Send one request to the bridge and block for its response."""
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

        deadline = time.time() + self.timeout
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

tool(
    "add_midi_notes",
    "Add MIDI notes to an existing MIDI item. Each note: {pitch (0-127, "
    "60=middle C), start_beats (absolute, from project start), length_beats, "
    "velocity (1-127, default 96), channel (0-15, default 0)}.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "item_index": {"type": "integer", "minimum": 0},
         "notes": {"type": "array", "items": obj({
             "pitch": {"type": "integer", "minimum": 0, "maximum": 127},
             "start_beats": {"type": "number"},
             "length_beats": {"type": "number"},
             "velocity": {"type": "integer", "minimum": 1, "maximum": 127},
             "channel": {"type": "integer", "minimum": 0, "maximum": 15},
         }, ["pitch", "start_beats"])}},
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
    "add_track_fx",
    "Add an FX to a track by name (e.g. 'ReaEQ', 'ReaComp', 'VST3:Serum'). "
    "Returns its fx_index.",
    obj({"track_index": {"type": "integer", "minimum": 0},
         "fx_name": {"type": "string"}},
        ["track_index", "fx_name"]),
    lambda b, a: b.call("add_track_fx", [a["track_index"], a["fx_name"]]),
)

tool(
    "list_track_fx",
    "List the FX on a track with index, name, enabled state and param count.",
    obj({"track_index": {"type": "integer", "minimum": 0}}, ["track_index"]),
    lambda b, a: b.call("list_track_fx", [a["track_index"]]),
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
    lambda b, a: b.call("render_project", [a.get("path")]),
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
