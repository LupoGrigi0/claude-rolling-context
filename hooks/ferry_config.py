#!/usr/bin/env python3
"""Ferry's ONE config resolver. install.sh, uninstall.sh and start-proxy.sh all
call this; none of them decides a port, a data dir or a settings path itself.

THE DECLARED SOURCE is ~/preferences.json, key "ferry" (it may be root-owned and
read-only to the mind -- write-once, read-many is the point):

    {"ferry": {"port": 5701, "data_dir": "/home/me/ferry-data",
               "curation": "ferry", "upstream": "...", "supervised": true}}

Every field is optional and each has an env override. Order, per field:

    1. process env           (but see below)
    2. ~/preferences.json "ferry"
    3. CLAUDE_CONFIG_DIR/settings.json `env`  (what the installer persisted)
    4. default               (port has NONE -- no shared 5588, ever; curation
                             defaults to ferry at `install` only, persisted)

A process-env value IDENTICAL to settings.json's is the copy Claude Code itself
injected from settings, not an override, so it ranks as settings. Without this
a mind running install.sh from inside a session would have its own legacy
ROLLING_CONTEXT_PORT=5588 shadow the port preferences.json declares.

`supervised` true means a supervisor (e.g. the hacs-ferry@$USER unit) owns the
proxy: the SessionStart hook starts nothing and writes nothing. It is declared
config, not a probe of systemd -- a crashed unit must not make the hook start a
second proxy that then fights the unit's restart for the port.

    ferry_config.py env              shell `export` lines (FERRY_CONFIG_ERROR set
                                     non-empty if the config is unusable)
    ferry_config.py install          persist non-preferences values to settings
    ferry_config.py point            write ANTHROPIC_BASE_URL (we own the port)
    ferry_config.py unpoint          uninstall: remove only what Ferry wrote
    ferry_config.py withdraw         hook start failed: revert ANTHROPIC_BASE_URL
                                     only (if Ferry wrote it); keep the config
    ferry_config.py holds-port PID PORT   exit 0 iff PID listens on PORT
    ferry_config.py is-proxy PID     exit 0 iff PID is a server.py
    ferry_config.py who-holds PORT   print the PID listening on PORT, if visible
"""
import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
from endpoints import config_dir  # noqa: E402  the one CLAUDE_CONFIG_DIR rule

# preferences.json "ferry" key -> the env var the proxy (or hook) reads.
FIELDS = {
    "port": "ROLLING_CONTEXT_PORT",
    "data_dir": "FERRY_DATA",
    "curation": "ROLLING_CONTEXT_CURATION",
    "upstream": "ROLLING_CONTEXT_UPSTREAM",
    "supervised": "FERRY_SUPERVISED",
}
DEFAULTS = {
    "data_dir": os.path.join(os.path.expanduser("~"), "ferry-data"),
}
# Applied by `install` only, which then persists it: the hook must never switch
# a mind that did not run this installer out of the mode it is in.
INSTALL_DEFAULTS = dict(DEFAULTS, curation="ferry")
# The base URL Ferry itself wrote. Uninstall removes ANTHROPIC_BASE_URL only if
# it still equals this -- never "any loopback URL", which may be someone else's.
MARK = "ROLLING_CONTEXT_WROTE_BASE_URL"


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    if not isinstance(s.get("env"), dict):
        s["env"] = {}
    return s


def save_settings(s):
    os.makedirs(config_dir(), exist_ok=True)
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
        f.write("\n")


def load_prefs():
    """The "ferry" block, {} if the file is absent. A file that EXISTS but cannot
    be read or parsed is an error: a declared source must not fail silent."""
    path = os.path.join(os.path.expanduser("~"), "preferences.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        block = (json.load(f) or {}).get("ferry") or {}
    if not isinstance(block, dict):
        raise ValueError(f'{path}: "ferry" must be an object')
    return block


def resolve(defaults=DEFAULTS):
    """-> (values {key: str}, sources {key: env|preferences|settings|default|unset})."""
    s_env = load_settings()["env"]
    prefs = load_prefs()
    vals, srcs = {}, {}
    for key, var in FIELDS.items():
        ev, sv, pv = os.environ.get(var, ""), str(s_env.get(var, "")), prefs.get(key)
        if ev and ev != sv:
            vals[key], srcs[key] = ev, "env"
        elif pv not in (None, ""):
            vals[key], srcs[key] = str(pv), "preferences"
        elif sv:
            vals[key], srcs[key] = sv, "settings"
        elif key in defaults:
            vals[key], srcs[key] = defaults[key], "default"
        else:
            vals[key], srcs[key] = "", "unset"
    vals["supervised"] = "1" if vals["supervised"].lower() in ("1", "true", "yes") else "0"
    return vals, srcs


def port_error(port):
    if not port:
        return ("no Ferry port configured -- set \"port\" in the \"ferry\" block of "
                "~/preferences.json, or ROLLING_CONTEXT_PORT. There is no default: "
                "a shared 5588 routes one mind through another's proxy.")
    if not port.isdigit() or not 0 < int(port) < 65536:
        return f"Ferry port {port!r} is not a TCP port"
    return ""


def proxy_url(port):
    return f"http://127.0.0.1:{port}"


def adopt_legacy(env):
    """Pre-marker installs wrote 127.0.0.1:<ROLLING_CONTEXT_PORT>. That URL is
    Ferry's; record it as such BEFORE anything rewrites the port, or it would
    later read as a foreign proxy and be chained to as an upstream."""
    if MARK not in env and env.get("ANTHROPIC_BASE_URL") and \
            env["ANTHROPIC_BASE_URL"] == proxy_url(env.get("ROLLING_CONTEXT_PORT", "5588")):
        env[MARK] = env["ANTHROPIC_BASE_URL"]


def cmd_env():
    try:
        vals, _ = resolve()
        err = port_error(vals["port"])
    except Exception as e:  # malformed preferences.json, mostly
        vals, err = {k: "" for k in FIELDS}, f"config unreadable: {e}"
        vals["supervised"] = "0"
    out = {var: vals[key] for key, var in FIELDS.items()}
    out["FERRY_CONFIG_DIR"] = config_dir()
    out["FERRY_CONFIG_ERROR"] = err
    for var, v in out.items():
        print(f"export {var}={shlex.quote(v)}")


def cmd_install():
    vals, srcs = resolve(INSTALL_DEFAULTS)
    if port_error(vals["port"]):
        print(f"  ERROR: {port_error(vals['port'])}")
        sys.exit(1)
    s = load_settings()
    env = s["env"]
    adopt_legacy(env)
    for key, var in FIELDS.items():
        if srcs[key] == "preferences":
            # One declared source: a copy here would shadow it on the next change.
            if env.pop(var, None) is not None:
                print(f"  {var}: removed from settings.json (preferences.json declares it)")
            else:
                print(f"  {var}={vals[key]} (preferences.json)")
        elif vals[key] and not (key == "supervised" and vals[key] == "0"):
            env[var] = vals[key]
            print(f"  {var}={vals[key]} ({srcs[key]})")
    for key, value in (("ROLLING_CONTEXT_TRIGGER", "100000"),
                       ("ROLLING_CONTEXT_TARGET", "40000")):
        env.setdefault(key, value)
    # Unset ROLLING_CONTEXT_MODEL = compress with the session's own model
    # (prompt-cache hit). Migrate away the old seeded haiku default.
    if env.get("ROLLING_CONTEXT_MODEL") == "claude-haiku-4-5-20251001":
        del env["ROLLING_CONTEXT_MODEL"]
    save_settings(s)
    print(f"  Settings written to {settings_path()}")


def cmd_point():
    vals, _ = resolve()
    if port_error(vals["port"]):
        sys.exit(port_error(vals["port"]))
    ours = proxy_url(vals["port"])
    s = load_settings()
    env = s["env"]
    adopt_legacy(env)
    existing = env.get("ANTHROPIC_BASE_URL", "")
    if existing == ours:
        result = "already"
    elif not existing:
        result = "set"
    elif existing == env.get(MARK):
        result = "replaced"          # our own URL from an earlier port
    else:
        # Anything else -- a remote API OR another loopback proxy -- is a route
        # we sit in front of, not one we may assume is ours.
        env["ROLLING_CONTEXT_UPSTREAM"] = existing
        result = "chained"
    env["ANTHROPIC_BASE_URL"] = ours
    env[MARK] = ours
    save_settings(s)
    print(result)


def _withdraw(env):
    """Take back the base URL only if Ferry wrote it; restore a chained upstream."""
    adopt_legacy(env)
    current = env.get("ANTHROPIC_BASE_URL", "")
    upstream = env.get("ROLLING_CONTEXT_UPSTREAM", "")
    if current and current == env.get(MARK):
        if upstream:
            env["ANTHROPIC_BASE_URL"] = upstream
            print(f"Restored ANTHROPIC_BASE_URL to {upstream}")
        else:
            del env["ANTHROPIC_BASE_URL"]
            print("Removed ANTHROPIC_BASE_URL")
    elif current:
        print(f"Left ANTHROPIC_BASE_URL={current} (Ferry did not write it)")


def cmd_withdraw():
    """The hook could not prove a proxy of ours on the port: stop pointing the
    NEXT session at it (dead, or someone else's). Config is kept; the next
    verified start points it again."""
    if not os.path.exists(settings_path()):
        return
    s = load_settings()
    _withdraw(s["env"])
    save_settings(s)


def cmd_unpoint():
    path = settings_path()
    if not os.path.exists(path):
        return
    s = load_settings()
    env = s["env"]
    _withdraw(env)
    for key in list(env):
        if key.startswith("ROLLING_CONTEXT_") or key in ("FERRY_DATA", "FERRY_SUPERVISED"):
            del env[key]
    save_settings(s)


def _listen_inodes(port):
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            col = row.split()
            if col[3] == "0A" and int(col[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f"socket:[{col[9]}]")
    return inodes


def _pid_sockets(pid):
    out = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            out.add(os.readlink(f"/proc/{pid}/fd/{fd}"))
        except OSError:
            pass
    return out


def holds_port(pid, port):
    if os.path.isdir("/proc/net"):
        return bool(_listen_inodes(port) & _pid_sockets(pid))
    try:  # no /proc (macOS): ask lsof about exactly this pid and port
        r = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}",
                            "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
        return str(pid) in r.stdout.split()
    except OSError:
        return False


def is_proxy(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        try:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True)
            argv = [a.encode() for a in r.stdout.split()]
        except OSError:
            return False
    return any(os.path.basename(a) == b"server.py" for a in argv)


def who_holds(port):
    """Only processes whose fds we can read -- i.e. our own, the only ones we
    could stop anyway."""
    inodes = _listen_inodes(port)
    if not inodes or not os.path.isdir("/proc"):
        return None
    for name in os.listdir("/proc"):
        if name.isdigit() and inodes & _pid_sockets(name):
            return int(name)
    return None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "env":
        cmd_env()
    elif cmd == "install":
        cmd_install()
    elif cmd == "point":
        cmd_point()
    elif cmd == "unpoint":
        cmd_unpoint()
    elif cmd == "withdraw":
        cmd_withdraw()
    elif cmd == "holds-port":
        sys.exit(0 if holds_port(int(argv[2]), int(argv[3])) else 1)
    elif cmd == "is-proxy":
        sys.exit(0 if is_proxy(int(argv[2])) else 1)
    elif cmd == "who-holds":
        pid = who_holds(int(argv[2]))
        if pid is None:
            sys.exit(1)
        print(pid)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
