#!/usr/bin/env python3
"""Ferry's ONE config resolver. install.sh, uninstall.sh and start-proxy.sh all
call this; none of them decides a port, a data dir, an upstream or a settings
path itself.

ONE SOURCE, per unix user, in the home: ~/preferences.json, key "ferry":

    {"ferry": {"port": 5701,                               REQUIRED, no default
               "upstream": "https://api.anthropic.com",    optional, this default
               "data_dir": "/home/me/ferry-data"}}         optional, ~/ferry-data

Nothing else is a config source: not the process env, not settings.json `env`.
Ferry runs AS THE USER, started by Claude Code's own SessionStart hook -- no
service manager, no root, nothing under /etc. Curation mode is always "ferry".
Env vars are only the TRANSPORT from the hook to the proxy process it starts.

ONE ROUTE: the proxy forwards to "upstream". The model is never Ferry's
business; it rides in each request untouched. settings.json gets only
ANTHROPIC_BASE_URL=http://127.0.0.1:<port> plus MARK (so uninstall removes only
what Ferry wrote).

SAFETY GUARD (install and point): if this mind is already routed somewhere
that is not Anthropic -- a non-Ferry ANTHROPIC_BASE_URL in settings.json or the
launcher env, an apiKeyHelper, or an ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY --
and preferences declares no "upstream", refuse, and say which value to put there. Otherwise Ferry would forward that key to Anthropic.
preferences.json is never written by Ferry.

    ferry_config.py env              shell `export` lines (FERRY_CONFIG_ERROR set
                                     non-empty if the config is unusable)
    ferry_config.py install          guard, then drop Ferry's legacy settings keys
    ferry_config.py point            write ANTHROPIC_BASE_URL (we own the port)
    ferry_config.py unpoint          uninstall: remove only what Ferry wrote
    ferry_config.py withdraw         hook start failed: revert ANTHROPIC_BASE_URL
                                     only (if Ferry wrote it)
    ferry_config.py holds-port PID PORT   exit 0 iff PID listens on PORT
    ferry_config.py is-proxy PID     exit 0 iff PID is a server.py
    ferry_config.py proxy-matches PORT   exit 0 iff the proxy on PORT forwards to
                                     and writes where preferences say (else why)
    ferry_config.py who-holds PORT   print the PID listening on PORT, if it is
                                     one of this uid's processes
"""
import json
import os
import shlex
import subprocess
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
from endpoints import config_dir, is_anthropic, DEFAULT_UPSTREAM  # noqa: E402

# preferences.json "ferry" key -> the env var that carries it to the proxy.
FIELDS = {
    "port": "ROLLING_CONTEXT_PORT",
    "data_dir": "FERRY_DATA",
    "upstream": "ROLLING_CONTEXT_UPSTREAM",
}
DEFAULTS = {
    "data_dir": os.path.join(os.path.expanduser("~"), "ferry-data"),
    "upstream": DEFAULT_UPSTREAM,
}
CURATION = "ferry"           # always, for an installed Ferry -- not a field
# The base URL Ferry itself wrote. Uninstall removes ANTHROPIC_BASE_URL only if
# it still equals this -- never "any loopback URL", which may be someone else's.
MARK = "ROLLING_CONTEXT_WROTE_BASE_URL"
# Keys earlier installs persisted into settings.json `env`. They are no longer
# read by anything; install drops them so settings.json cannot look like a
# second source of the same truth. (TRIGGER/TARGET only at the values WE seeded.)
LEGACY_KEYS = ("ROLLING_CONTEXT_PORT", "FERRY_DATA", "ROLLING_CONTEXT_CURATION",
               "ROLLING_CONTEXT_UPSTREAM")
LEGACY_SEEDED = {"ROLLING_CONTEXT_TRIGGER": "100000", "ROLLING_CONTEXT_TARGET": "40000",
                 "ROLLING_CONTEXT_MODEL": "claude-haiku-4-5-20251001"}


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


def prefs_path():
    return os.path.join(os.path.expanduser("~"), "preferences.json")


def load_prefs():
    """The "ferry" block, {} if the file is absent. A file that EXISTS but cannot
    be read or parsed is an error: a declared source must not fail silent."""
    path = prefs_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        block = (json.load(f) or {}).get("ferry") or {}
    if not isinstance(block, dict):
        raise ValueError(f'{path}: "ferry" must be an object')
    return block


def resolve():
    """-> {key: str}. preferences.json "ferry", else the built-in default.
    Port has no default ("")."""
    prefs = load_prefs()
    vals = {}
    for key in FIELDS:
        pv = prefs.get(key)
        vals[key] = str(pv) if pv not in (None, "") else DEFAULTS.get(key, "")
    return vals


def port_error(port):
    if not port:
        return ('no Ferry port configured -- set "port" in the "ferry" block of '
                "~/preferences.json. There is no default: a shared 5588 routes "
                "one mind through another's proxy.")
    if not port.isdigit() or not 0 < int(port) < 65536:
        return f"Ferry port {port!r} is not a TCP port"
    return ""


def config_error(vals):
    err = port_error(vals["port"])
    if err:
        return err
    u = urlparse(vals["upstream"])
    if u.scheme not in ("http", "https") or not u.hostname:
        return f'ferry "upstream" {vals["upstream"]!r} is not an http(s) URL'
    if u.hostname in ("127.0.0.1", "localhost", "::1") and \
            (u.port or (443 if u.scheme == "https" else 80)) == int(vals["port"]):
        return (f'ferry "upstream" {vals["upstream"]} is Ferry\'s own port -- '
                "every request would loop back into the proxy")
    return ""


def proxy_url(port):
    return f"http://127.0.0.1:{port}"


def adopt_legacy(env):
    """Pre-marker installs wrote 127.0.0.1:<ROLLING_CONTEXT_PORT>. That URL is
    Ferry's; record it as such BEFORE anything rewrites the port, or it would
    later read as a foreign route."""
    if MARK not in env and env.get("ANTHROPIC_BASE_URL") and \
            env["ANTHROPIC_BASE_URL"] == proxy_url(env.get("ROLLING_CONTEXT_PORT", "5588")):
        env[MARK] = env["ANTHROPIC_BASE_URL"]


def route_guard(s, prefs, port):
    """'' if Ferry may take this mind's route, else why not and what to do.

    The one case refused: settings.json already sends this mind somewhere that is
    not Anthropic and preferences.json does not say where Ferry should forward.
    Proceeding would forward that mind's key to DEFAULT_UPSTREAM."""
    if prefs.get("upstream") not in (None, ""):
        return ""
    env = s["env"]
    base = env.get("ANTHROPIC_BASE_URL", "")
    ours = base and base in (env.get(MARK), proxy_url(port))
    route = ""
    if base and not ours and not is_anthropic(base):
        route = base
    elif ours and env.get("ROLLING_CONTEXT_UPSTREAM") and \
            not is_anthropic(env["ROLLING_CONTEXT_UPSTREAM"]):
        route = env["ROLLING_CONTEXT_UPSTREAM"]      # an older Ferry chained it
    # The launcher's env is the route Claude Code would use with no settings
    # base URL -- and `point` writes one that outranks it. Not a config source:
    # a fact the guard must see, or that route's key goes to DEFAULT_UPSTREAM.
    launch = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not route and launch and launch not in (env.get(MARK), proxy_url(port), base) \
            and not is_anthropic(launch):
        route = launch
    helper = s.get("apiKeyHelper")
    # A key in settings or the launcher env may belong to a route Ferry already
    # replaced (or never saw): without "upstream" it would go to Anthropic.
    keyvar = next((k for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
                   if env.get(k) or os.environ.get(k)), "")
    if not route and not helper and not keyvar:
        return ""
    why = (f"this mind is routed to {route}" if route else
           "settings.json has an apiKeyHelper (a key that may not be Anthropic's)"
           if helper else
           f"{keyvar} is set (a key that may not be Anthropic's)")
    want = route or "<the URL that key belongs to; https://api.anthropic.com if it is an Anthropic key>"
    return (f"{why}, and ~/preferences.json declares no ferry \"upstream\". Ferry "
            f"would forward this mind's key to {DEFAULT_UPSTREAM}. Add  "
            f"\"upstream\": \"{want}\"  to the \"ferry\" block of "
            f"{prefs_path()}, then run it again. ({settings_path()})")


def cmd_env():
    try:
        prefs, vals = load_prefs(), resolve()
        err = config_error(vals)
        if not err:      # refuse BEFORE the hook starts anything
            s = load_settings()
            adopt_legacy(s["env"])
            err = route_guard(s, prefs, vals["port"])
    except Exception as e:  # malformed preferences.json, mostly
        vals, err = {k: "" for k in FIELDS}, f"config unreadable: {e}"
    out = {var: vals[key] for key, var in FIELDS.items()}
    out["ROLLING_CONTEXT_CURATION"] = CURATION
    out["FERRY_CONFIG_DIR"] = config_dir()
    out["FERRY_CONFIG_ERROR"] = err
    for var, v in out.items():
        print(f"export {var}={shlex.quote(v)}")


def _checked():
    """-> (vals, prefs, settings) or exit non-zero with the reason. Writes nothing."""
    try:
        prefs, vals = load_prefs(), resolve()
        err = config_error(vals)
    except Exception as e:
        vals, err = None, f"config unreadable: {e}"
    s = load_settings()
    adopt_legacy(s["env"])
    err = err or route_guard(s, prefs, vals["port"])
    if err:
        print(f"  ERROR: {err}")
        sys.exit(1)
    return vals, prefs, s


def cmd_install():
    vals, _, s = _checked()
    env = s["env"]
    for key in LEGACY_KEYS:
        if env.pop(key, None) is not None:
            print(f"  {key}: removed from settings.json (~/preferences.json is the one source)")
    for key, seeded in LEGACY_SEEDED.items():
        if env.get(key) == seeded:
            del env[key]
    if os.path.exists(settings_path()):
        save_settings(s)
    for key, var in FIELDS.items():
        print(f"  {key} = {vals[key]}")


def cmd_point():
    vals, _, s = _checked()
    ours = proxy_url(vals["port"])
    env = s["env"]
    existing = env.get("ANTHROPIC_BASE_URL", "")
    if existing == ours:
        result = "already"
    elif not existing:
        result = "set"
    elif existing == env.get(MARK):
        result = "replaced"          # our own URL from an earlier port
    else:
        result = f"replaced {existing} (preferences declares the upstream)"
    env["ANTHROPIC_BASE_URL"] = ours
    env[MARK] = ours
    save_settings(s)
    print(result)


def _withdraw(env):
    """Take back the base URL only if Ferry wrote it. If the mind had its own
    route, put it back: preferences' "upstream" (the one source), else the one
    an older Ferry chained into settings."""
    adopt_legacy(env)
    current = env.get("ANTHROPIC_BASE_URL", "")
    try:
        declared = load_prefs().get("upstream") or ""
    except Exception:
        declared = ""
    restore = declared or env.get("ROLLING_CONTEXT_UPSTREAM", "")
    if restore and is_anthropic(restore):
        restore = ""       # no base URL IS Anthropic; never pin one Ferry did not find
    if current and current == env.get(MARK):
        if restore:
            env["ANTHROPIC_BASE_URL"] = restore
            print(f"Restored ANTHROPIC_BASE_URL to {restore}")
        else:
            del env["ANTHROPIC_BASE_URL"]
            print("Removed ANTHROPIC_BASE_URL")
    elif current:
        print(f"Left ANTHROPIC_BASE_URL={current} (Ferry did not write it)")


def cmd_withdraw():
    """The hook could not prove a proxy of ours on the port: stop pointing the
    NEXT session at it (dead, or someone else's)."""
    if not os.path.exists(settings_path()):
        return
    s = load_settings()
    before = dict(s["env"])
    _withdraw(s["env"])
    if s["env"] != before:       # never rewrite a file Ferry did not change
        save_settings(s)


def cmd_unpoint():
    path = settings_path()
    if not os.path.exists(path):
        return
    s = load_settings()
    env = s["env"]
    _withdraw(env)
    for key in list(env):
        if key.startswith("ROLLING_CONTEXT_") or key == "FERRY_DATA":
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
    """The PID listening on PORT -- only if it is THIS uid's process (the only
    kind whose fds we can read, and the only kind the hook may reuse)."""
    inodes = _listen_inodes(port)
    if not inodes or not os.path.isdir("/proc"):
        return None
    for name in os.listdir("/proc"):
        if name.isdigit() and inodes & _pid_sockets(name):
            try:
                if os.stat(f"/proc/{name}").st_uid != os.getuid():
                    return None
            except OSError:
                return None
            return int(name)
    return None


def proxy_matches(port):
    """'' if the proxy answering on PORT forwards where preferences say and
    writes where preferences say; else what differs. A reused proxy keeps the
    upstream it was STARTED with -- it must not become a second source."""
    import urllib.request
    vals = resolve()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                 headers={"X-Ferry-Canary": "hookreuse"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            up = json.load(r).get("upstream_url", "")
            data = r.headers.get("X-Ferry-Data", "")
    except Exception as e:
        return f"its /health did not answer ({e})"
    diffs = []
    if up.rstrip("/") != vals["upstream"].rstrip("/"):
        diffs.append(f"forwards to {up}, preferences say {vals['upstream']}")
    if data != os.path.realpath(os.path.expanduser(vals["data_dir"])):
        diffs.append(f"data dir {data or '(not reported: older Ferry code)'}, "
                     f"preferences say {vals['data_dir']}")
    return "; ".join(diffs)


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
    elif cmd == "proxy-matches":
        why = proxy_matches(int(argv[2]))
        if why:
            print(why)
            sys.exit(1)
    elif cmd == "who-holds":
        pid = who_holds(int(argv[2]))
        if pid is None:
            sys.exit(1)
        print(pid)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
