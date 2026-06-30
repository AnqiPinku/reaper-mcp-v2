#!/usr/bin/env python3
"""
Offline test for reaper_mcp_server.py.

Spawns the server as a subprocess, talks MCP JSON-RPC to it over stdio, and
runs a *fake bridge* in this process: a background thread that watches the
mcp_bridge_data directory, answers request.json with a canned response.json,
exactly like the real Lua bridge would. No REAPER required.

Run:  python test_server.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "reaper_mcp_server.py")


def fake_bridge(bridge_dir, stop):
    req = os.path.join(bridge_dir, "request.json")
    resp = os.path.join(bridge_dir, "response.json")
    while not stop.is_set():
        if os.path.exists(req):
            try:
                with open(req, encoding="utf-8") as f:
                    payload = json.load(f)
                os.remove(req)
            except (OSError, ValueError):
                time.sleep(0.005)
                continue
            func = payload.get("func")
            # Canned responses keyed by func
            if func == "get_project_summary":
                ret = {"project": "(unsaved)", "tempo_bpm": 120.0,
                       "track_count": 1,
                       "tracks": [{"index": 0, "name": "Drums"}]}
            elif func == "add_track":
                ret = {"index": payload["args"][1] or 0,
                       "name": payload["args"][0] or ""}
            elif func == "run_lua":
                ret = 1  # pretend the lua returned 1
            elif func == "render_to_wav":
                ret = {"path": payload["args"][0],
                       "source": payload["args"][1],
                       "sample_rate": payload["args"][2]}
            elif func == "CountTracks":
                ret = 3
            else:
                ret = {"echo": func, "args": payload.get("args")}
            out = {"id": payload.get("id"), "ok": True, "ret": ret}
            tmp = resp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(tmp, resp)
        time.sleep(0.005)


def rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if msg.get("id") is None:
        return None
    line = proc.stdout.readline()
    return json.loads(line)


def main():
    bridge_dir = tempfile.mkdtemp(prefix="reaper_ipc_")

    stop = threading.Event()
    t = threading.Thread(target=fake_bridge, args=(bridge_dir, stop), daemon=True)
    t.start()

    env = dict(os.environ, REAPER_MCP_IPC_DIR=bridge_dir, REAPER_MCP_TIMEOUT="5")
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        text=True, env=env, encoding="utf-8",
    )

    failures = []

    def check(label, cond):
        print(("  PASS" if cond else "  FAIL"), label)
        if not cond:
            failures.append(label)

    try:
        r = rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {}})
        check("initialize returns serverInfo",
              r["result"]["serverInfo"]["name"] == "reaper-mcp")

        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        r = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        check("tools/list has core tools",
              {"reaper_status", "add_track", "run_lua",
               "reaper_call"} <= names)
        print("       tools:", len(names))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "reaper_status", "arguments": {}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("reaper_status round-trips bridge", body["tempo_bpm"] == 120.0)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "add_track",
                                  "arguments": {"name": "Bass", "index": 2}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("add_track passes args positionally",
              body["name"] == "Bass" and body["index"] == 2)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "reaper_call",
                                  "arguments": {"func": "CountTracks",
                                                "args": [0]}}})
        check("reaper_call generic works",
              r["result"]["content"][0]["text"] == "3")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "run_lua",
                                  "arguments": {"code": "return 1"}}})
        check("run_lua works", r["result"]["content"][0]["text"] == "1")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "render_to_wav",
                                  "arguments": {"out_path": "A:/tmp/mix.wav"}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("render_to_wav passes path + defaults source/sample_rate",
              body["path"] == "A:/tmp/mix.wav"
              and body["source"] == "time_selection"
              and body["sample_rate"] == 48000)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
        check("unknown tool errors", "error" in r)

    finally:
        stop.set()
        proc.stdin.close()
        proc.terminate()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):", failures)
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
