#!/usr/bin/env python3
"""One-click bridge installer + live status probe (zero-dependency, stdlib only).

Replaces the manual "Actions > Load ReaScript > Run" ritual with a `__startup.lua`
that REAPER auto-runs at launch.

Three hard-won correctness points (all learned the hard way on a real machine):
  1. REAPER's resource path is NOT always %APPDATA%/REAPER. Portable installs put
     it next to the exe (often a non-ASCII path, e.g. A:\\科广\\Reaper). Install into
     the resource path of the *actual running* REAPER — ask it via GetResourcePath()
     through a loaded bridge. Fallbacks: a saved path, a manual override, the default.
  2. REAPER itself loads __startup.lua fine even from a non-ASCII resource path
     (it's Unicode-aware), BUT Lua's own dofile/io can fail to open some paths —
     non-ASCII ones, and (observed) files under a OneDrive-redirected %APPDATA%.
     So __startup.lua must `dofile` the bridge from a plain ASCII, non-redirected
     location. We point it straight at the shipped bridge (BRIDGE_SRC, ASCII app
     path) rather than copying into %APPDATA% (which failed on the test machine).
  3. Embed the dofile target as an absolute forward-slash path.

Lives in reaper-mcp (it owns the bridge). The desktop gateway imports it by path.
CLI:  python install_bridge.py status | install [--resource DIR] | uninstall
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))                 # .../reaper-mcp/installer
BRIDGE_SRC = os.path.normpath(os.path.join(HERE, "..", "bridge", "reaper_mcp_bridge.lua"))
SERVER_SRC = os.path.normpath(os.path.join(HERE, "..", "server", "reaper_mcp_server.py"))

STARTUP_NAME = "__startup.lua"
BEGIN = "-- >>> reaper-mcp-bridge (managed) >>>"
END = "-- <<< reaper-mcp-bridge (managed) <<<"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def _lua_path(p):
    """Absolute path with forward slashes — safe to embed in a Lua string."""
    return os.path.abspath(p).replace("\\", "/")


def _appdata_base():
    return (os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share"))


def _state_path():
    return os.path.join(_appdata_base(), "reaper-mcp", "install_state.json")


def default_resource_path():
    """Fallback only — wrong for portable installs. Prefer detect_resource_path()."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return os.path.join(base, "REAPER") if base else None
    home = os.path.expanduser("~")
    mac = os.path.join(home, "Library", "Application Support", "REAPER")
    return mac if os.path.isdir(mac) else os.path.join(home, ".config", "REAPER")


# --------------------------------------------------------------------------
# Resolve REAPER's REAL resource path (portable-safe)
# --------------------------------------------------------------------------
def detect_resource_path():
    """Authoritative: ask the running REAPER via a loaded bridge. None if not loaded."""
    try:
        b = _server().Bridge()
        b.timeout = 1.5
        rp = b.call("GetResourcePath")
        return rp if isinstance(rp, str) and rp else None
    except Exception:      # noqa: BLE001 bridge not loaded / timeout
        return None


def _saved_resource_path():
    try:
        return json.loads(_read(_state_path())).get("resource_path")
    except (ValueError, AttributeError):
        return None


def resolve_resource_path(override=None, loaded=None):
    if override:
        return os.path.abspath(override)
    if loaded:
        rp = detect_resource_path()
        if rp:
            return rp
    return _saved_resource_path() or default_resource_path()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _startup_block(bridge_lua_path):
    return (BEGIN + "\n"
            "-- Auto-loads the reaper-mcp bridge at REAPER startup. Managed by install_bridge.py.\n"
            "local ok, err = pcall(dofile, '" + bridge_lua_path + "')\n"
            "if not ok then reaper.ShowConsoleMsg('reaper-mcp bridge autoload failed: ' "
            ".. tostring(err)) end\n"
            + END + "\n")


def _startup_registered(scripts):
    txt = _read(os.path.join(scripts, STARTUP_NAME)) if scripts else ""
    return BEGIN in txt and END in txt


def _embedded_bridge_path(scripts):
    """The bridge path our managed __startup block currently dofiles (or None)."""
    txt = _read(os.path.join(scripts, STARTUP_NAME)) if scripts else ""
    if BEGIN not in txt:
        return None
    m = re.search(r"pcall\(dofile,\s*'([^']+)'\)", txt)
    return m.group(1) if m else None


def _register_startup(scripts, bridge_lua_path):
    """Write our sentinel block into <scripts>/__startup.lua. Create if absent;
    replace ONLY our block if present; preserve all foreign lines. Python writes
    Unicode paths fine even when <scripts> is non-ASCII."""
    path = os.path.join(scripts, STARTUP_NAME)
    block = _startup_block(bridge_lua_path)
    existing = _read(path)
    if BEGIN in existing and END in existing:
        pre = existing[:existing.index(BEGIN)]
        post = existing[existing.index(END) + len(END):].lstrip("\r\n")
        new = pre + block + post
    elif existing:
        new = existing + ("" if existing.endswith("\n") else "\n") + block
    else:
        new = block
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)


# --------------------------------------------------------------------------
# Live status
# --------------------------------------------------------------------------
def _reaper_running():
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                  timeout=6, creationflags=flags)
            return "reaper" in (out.stdout or "").lower()
        out = subprocess.run(["pgrep", "-i", "reaper"], capture_output=True,
                             text=True, timeout=6)
        return out.returncode == 0
    except Exception:      # noqa: BLE001
        return None


_SERVER_MOD = None


def _server():
    global _SERVER_MOD
    if _SERVER_MOD is None and os.path.isfile(SERVER_SRC):
        spec = importlib.util.spec_from_file_location("reaper_mcp_server_installer", SERVER_SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SERVER_MOD = mod
    return _SERVER_MOD


def _bridge_ping(timeout=1.5):
    srv = _server()
    if not srv:
        return False
    try:
        b = srv.Bridge()
        b.timeout = float(timeout)
        return b.call("ping") == "pong"
    except Exception:      # noqa: BLE001
        return False


def status(resource=None, ping_timeout=1.5):
    loaded = _bridge_ping(ping_timeout)
    rp = resolve_resource_path(resource, loaded=loaded)
    scripts = os.path.join(rp, "Scripts") if rp else None
    installed = _startup_registered(scripts) if scripts else False
    # current = our block points at THIS build's bridge (stale if the app moved)
    current = installed and _embedded_bridge_path(scripts) == _lua_path(BRIDGE_SRC)
    running = True if loaded else _reaper_running()
    state = ("connected" if loaded
             else "running_not_loaded" if running
             else "not_running" if running is False
             else "not_loaded")
    return {
        "resource_path": rp,
        "resource_exists": bool(rp and os.path.isdir(rp)),
        "resource_detected": bool(loaded),   # True => path came straight from REAPER
        "installed": installed,
        "installed_current": bool(current),
        "reaper_running": running,
        "bridge_loaded": loaded,
        "state": state,
    }


# --------------------------------------------------------------------------
# Install / uninstall
# --------------------------------------------------------------------------
def install(resource=None, bridge_src=None):
    bridge_src = bridge_src or BRIDGE_SRC
    if not os.path.isfile(bridge_src):
        return {"ok": False, "error": "找不到 bridge 源文件：%s" % bridge_src}

    loaded = _bridge_ping(1.5)
    resource = resolve_resource_path(resource, loaded=loaded)
    if not resource or not os.path.isdir(resource):
        return {"ok": False, "error":
                "无法定位 REAPER 资源目录（便携版？请先在 REAPER 里手动加载一次桥让我读到真实路径，"
                "或手动指定路径）。当前尝试：%s" % resource}

    scripts = os.path.join(resource, "Scripts")
    os.makedirs(scripts, exist_ok=True)
    bridge_lua = _lua_path(bridge_src)          # dofile the shipped bridge in place (ASCII)
    _register_startup(scripts, bridge_lua)

    try:                                        # persist for status() when bridge isn't loaded
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump({"resource_path": resource, "bridge_path": bridge_lua,
                       "bridge_sha": _sha1(bridge_src), "resource_detected": bool(loaded),
                       "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
    except OSError:
        pass

    return {"ok": True, "resource_path": resource, "bridge_path": bridge_lua,
            "resource_detected": bool(loaded),
            "actions": ["在资源目录注册 __startup.lua 自动加载：%s" % resource,
                        "自动加载指向 %s" % bridge_lua],
            "restart_required": True, "status": status(resource)}


def uninstall(resource=None):
    loaded = _bridge_ping(1.0)
    resource = resolve_resource_path(resource, loaded=loaded)
    removed = []
    if resource:
        sp = os.path.join(resource, "Scripts", STARTUP_NAME)
        txt = _read(sp)
        if BEGIN in txt and END in txt:
            new = (txt[:txt.index(BEGIN)] + txt[txt.index(END) + len(END):]).strip("\r\n")
            if new.strip():
                with open(sp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new + "\n")
            else:
                os.remove(sp)
            removed.append("__startup.lua block")
    if os.path.isfile(_state_path()):
        try:
            os.remove(_state_path())
            removed.append("install_state.json")
        except OSError:
            pass
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    override = argv[argv.index("--resource") + 1] if "--resource" in argv else None
    out = {"install": install, "uninstall": uninstall}.get(cmd, lambda r=None: status(r))(override)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
