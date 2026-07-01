#!/usr/bin/env python3
"""One-click bridge installer + live status probe (zero-dependency, stdlib only).

Replaces the manual "Actions > Load ReaScript > Run" ritual:
  1. resolve REAPER's per-user resource path (portable-aware, with manual override)
  2. copy bridge/reaper_mcp_bridge.lua into <resource>/Scripts/ + a .version stamp
  3. idempotently register auto-run via <resource>/Scripts/__startup.lua
     (MERGE a sentinel-delimited block, never clobber the user's own startup file)
  4. purge known-stale leftovers from older manual installs
Plus status(): is REAPER running? is a bridge answering ping? is our copy current?

Lives in reaper-mcp (it owns the bridge). The desktop gateway imports it by file
path via ${PRISM_HOME}/reaper-mcp/installer/install_bridge.py.

CLI:  python install_bridge.py status | install [--resource DIR] | uninstall
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))                 # .../reaper-mcp/installer
BRIDGE_SRC = os.path.normpath(os.path.join(HERE, "..", "bridge", "reaper_mcp_bridge.lua"))
SERVER_SRC = os.path.normpath(os.path.join(HERE, "..", "server", "reaper_mcp_server.py"))

BRIDGE_NAME = "reaper_mcp_bridge.lua"
STAMP_NAME = ".reaper_mcp_bridge.version"
STARTUP_NAME = "__startup.lua"
BEGIN = "-- >>> reaper-mcp-bridge (managed) >>>"
END = "-- <<< reaper-mcp-bridge (managed) <<<"


# --------------------------------------------------------------------------
# Resource-path resolution
# --------------------------------------------------------------------------
def default_resource_path():
    """Per-OS REAPER resource dir (holds Scripts/, __startup.lua, REAPER.ini).
    Portable installs keep it next to reaper.exe instead — pass a manual override."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return os.path.join(base, "REAPER") if base else None
    home = os.path.expanduser("~")
    mac = os.path.join(home, "Library", "Application Support", "REAPER")
    if os.path.isdir(mac):
        return mac
    return os.path.join(home, ".config", "REAPER")


def resolve_resource_path(override=None):
    return os.path.abspath(override) if override else default_resource_path()


def _scripts_dir(resource):
    return os.path.join(resource, "Scripts") if resource else None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _startup_block():
    return (BEGIN + "\n"
            "local ok, err = pcall(dofile, reaper.GetResourcePath() .. "
            "'/Scripts/" + BRIDGE_NAME + "')\n"
            "if not ok then reaper.ShowConsoleMsg('reaper-mcp bridge autoload "
            "failed: ' .. tostring(err) .. '\\n') end\n"
            + END + "\n")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _startup_registered(scripts):
    txt = _read(os.path.join(scripts, STARTUP_NAME)) if scripts else ""
    return BEGIN in txt and END in txt


def _register_startup(scripts):
    """Write our sentinel-delimited autoload block into __startup.lua.
    Create if absent; replace ONLY our block if present; preserve all foreign lines."""
    path = os.path.join(scripts, STARTUP_NAME)
    block = _startup_block()
    existing = _read(path)
    if BEGIN in existing and END in existing:
        pre = existing[:existing.index(BEGIN)]
        post = existing[existing.index(END) + len(END):].lstrip("\r\n")
        new = pre + block + post
    elif existing:
        sep = "" if existing.endswith("\n") else "\n"
        new = existing + sep + block
    else:
        new = block
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


def _purge_stale(scripts):
    """Remove leftovers from older bridge installs that can double-load or confuse.
    The stale reaper_mcp_bridge.lua itself is overwritten by the copy (reported
    separately as 'replaced'); here we drop the old Scripts/mcp_bridge_data dir
    (an earlier bridge's data dir; the current bridge uses %APPDATA%/reaper-mcp-ipc)."""
    purged = []
    data_dir = os.path.join(scripts, "mcp_bridge_data")
    if os.path.isdir(data_dir):
        try:
            shutil.rmtree(data_dir)
            purged.append("mcp_bridge_data/")
        except OSError:
            pass
    return purged


# --------------------------------------------------------------------------
# Live status: REAPER running? bridge answering? installed copy current?
# --------------------------------------------------------------------------
def _reaper_running():
    """Best-effort process check. None if undeterminable (never assume)."""
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                  timeout=6, creationflags=flags)
            return "reaper" in (out.stdout or "").lower()
        out = subprocess.run(["pgrep", "-i", "reaper"], capture_output=True,
                             text=True, timeout=6)
        return out.returncode == 0
    except Exception:      # noqa: BLE001 no tasklist/pgrep, timeout, etc.
        return None


_SERVER_MOD = None


def _server():
    """Load reaper_mcp_server as a module (for its Bridge = single source of IPC truth)."""
    global _SERVER_MOD
    if _SERVER_MOD is None and os.path.isfile(SERVER_SRC):
        spec = importlib.util.spec_from_file_location("reaper_mcp_server_installer", SERVER_SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SERVER_MOD = mod
    return _SERVER_MOD


def _bridge_ping(timeout=1.5):
    """True iff a loaded bridge answers ping within timeout (short — the UI polls)."""
    srv = _server()
    if not srv:
        return False
    try:
        b = srv.Bridge()
        b.timeout = float(timeout)
        return b.call("ping") == "pong"
    except Exception:      # noqa: BLE001 BridgeError timeout => not loaded
        return False


def status(resource=None, ping_timeout=1.5):
    resource = resolve_resource_path(resource)
    scripts = _scripts_dir(resource)
    installed = bool(scripts and os.path.isfile(os.path.join(scripts, BRIDGE_NAME)))
    current = False
    if installed:
        want = _sha1(BRIDGE_SRC) if os.path.isfile(BRIDGE_SRC) else None
        have = None
        try:
            have = json.loads(_read(os.path.join(scripts, STAMP_NAME))).get("sha1")
        except (ValueError, AttributeError):
            pass
        current = bool(want and have == want)
    loaded = _bridge_ping(ping_timeout)
    running = True if loaded else _reaper_running()   # a pong proves it's running
    state = ("connected" if loaded
             else "running_not_loaded" if running
             else "not_running" if running is False
             else "not_loaded")                        # running unknown, no pong
    return {
        "resource_path": resource,
        "resource_exists": bool(resource and os.path.isdir(resource)),
        "installed": installed,
        "installed_current": current,
        "autorun_registered": _startup_registered(scripts) if scripts else False,
        "reaper_running": running,
        "bridge_loaded": loaded,
        "state": state,
    }


# --------------------------------------------------------------------------
# Install / uninstall
# --------------------------------------------------------------------------
def install(resource=None):
    resource = resolve_resource_path(resource)
    if not resource:
        return {"ok": False, "error": "无法定位 REAPER 资源目录，请手动指定路径。"}
    if not os.path.isdir(resource):
        return {"ok": False,
                "error": "REAPER 资源目录不存在：%s（REAPER 装了吗？便携版请手动指定）" % resource}
    if not os.path.isfile(BRIDGE_SRC):
        return {"ok": False, "error": "找不到 bridge 源文件：%s" % BRIDGE_SRC}

    scripts = _scripts_dir(resource)
    os.makedirs(scripts, exist_ok=True)
    actions = []

    dst = os.path.join(scripts, BRIDGE_NAME)
    src_sha = _sha1(BRIDGE_SRC)
    replaced = os.path.isfile(dst) and _sha1(dst) != src_sha
    shutil.copy2(BRIDGE_SRC, dst)
    with open(os.path.join(scripts, STAMP_NAME), "w", encoding="utf-8") as f:
        json.dump({"sha1": src_sha,
                   "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": os.path.abspath(BRIDGE_SRC)}, f, ensure_ascii=False)
    actions.append(("替换旧桥并更新 " if replaced else "拷贝 ") + BRIDGE_NAME)

    for p in _purge_stale(scripts):
        actions.append("清理 " + p)

    _register_startup(scripts)
    actions.append("注册 __startup.lua 自动加载")

    return {"ok": True, "resource_path": resource, "actions": actions,
            "restart_required": True, "status": status(resource)}


def uninstall(resource=None):
    resource = resolve_resource_path(resource)
    scripts = _scripts_dir(resource)
    if not scripts:
        return {"ok": False, "error": "无法定位 REAPER 资源目录。"}
    removed = []
    for name in (BRIDGE_NAME, STAMP_NAME):
        p = os.path.join(scripts, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(name)
            except OSError:
                pass
    sp = os.path.join(scripts, STARTUP_NAME)
    txt = _read(sp)
    if BEGIN in txt and END in txt:
        new = (txt[:txt.index(BEGIN)] + txt[txt.index(END) + len(END):]).strip("\r\n")
        if new.strip():
            with open(sp, "w", encoding="utf-8") as f:
                f.write(new + "\n")
        else:
            os.remove(sp)                    # only our block was in it
        removed.append("__startup.lua block")
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    override = None
    if "--resource" in argv:
        override = argv[argv.index("--resource") + 1]
    if cmd == "install":
        out = install(override)
    elif cmd == "uninstall":
        out = uninstall(override)
    else:
        out = status(override)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
