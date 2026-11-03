#!/usr/bin/env python3
"""Ship items S1, S3, S4 — the installer and the one launcher.

Every case runs install.sh / hooks/start-proxy.sh / uninstall.sh as a
subprocess against a TEMP $HOME and a TEMP $CLAUDE_CONFIG_DIR, with every
ROLLING_CONTEXT_* / FERRY_* / ANTHROPIC_* / CLAUDE_CONFIG_DIR var stripped from
the inherited env first — this suite must never read or write the real ones.
Ports come only from 5700-5799; the only process it stops is one it started
(cmdline verified first), and it never kills by pattern.

  S1  README clones the fork; install writes ROLLING_CONTEXT_CURATION=ferry and
      a per-mind FERRY_DATA.
  S3  no port -> refuse; the launched pid is VERIFIED to hold the port; a port
      someone else holds -> loud failure and settings.json untouched.
  S4  ~/preferences.json "ferry" is the ONE config source (no env layer, no
      settings.json layer); settings.json gets only ANTHROPIC_BASE_URL + MARK;
      CLAUDE_CONFIG_DIR honoured; the hook carries preferences "upstream" to the
      proxy; a non-Anthropic route already in settings (base URL or
      apiKeyHelper) with no preferences "upstream" -> install refuses, says
      which value to declare, writes nothing; the hook reuses a proxy of this
      uid already on the port. No systemd, no /etc, no "supervised".

  python3 tests/test_install.py
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "hooks" / "ferry_config.py"

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"\n       {ctx}" if ctx is not None else ""))


def free_port():
    """A port in 5700-5799 nothing is listening on right now."""
    for p in range(5700, 5800):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit("no free port in 5700-5799")


class Box:
    """A temp HOME + temp CLAUDE_CONFIG_DIR. Nothing real is reachable."""

    def __init__(self, prefs=None, settings=None, config_dir=True):
        self.root = Path(tempfile.mkdtemp(prefix="ferry-install-test-"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.cfg = self.root / "cfg" if config_dir else self.home / ".claude"
        self.cfg.mkdir(parents=True)
        if prefs is not None:
            (self.home / "preferences.json").write_text(json.dumps(prefs))
        if settings is not None:
            (self.cfg / "settings.json").write_text(json.dumps(settings))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ROLLING_CONTEXT_", "FERRY_", "ANTHROPIC_"))
                    and k != "CLAUDE_CONFIG_DIR"}
        self.env["HOME"] = str(self.home)
        if config_dir:
            self.env["CLAUDE_CONFIG_DIR"] = str(self.cfg)

    def run(self, *argv, **extra):
        env = dict(self.env, **{k: str(v) for k, v in extra.items()})
        return subprocess.run(list(argv), env=env, capture_output=True, text=True,
                              timeout=60, cwd=str(self.root))

    def sh(self, script, **extra):
        return self.run("bash", str(REPO / script), **extra)

    def conf(self, *args, **extra):
        return self.run(sys.executable, str(CONF), *map(str, args), **extra)

    def settings_env(self):
        p = self.cfg / "settings.json"
        return json.loads(p.read_text()).get("env", {}) if p.exists() else None

    def pid(self):
        p = self.cfg / "rolling-context-proxy.pid"
        return int(p.read_text().strip()) if p.exists() else None

    def stop_started(self, pid):
        """Stop ONLY a pid this suite started: alive, ours, cmdline is OUR server.py."""
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return
        if any(a.endswith(b"server.py") for a in argv) and \
                Path(f"/proc/{pid}/cwd").resolve() == (REPO / "proxy").resolve():
            os.kill(pid, signal.SIGTERM)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class _Upstream(BaseHTTPRequestHandler):
    """Stand-in upstream so a canary's forwarded request never leaves the box."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(401)
        self.send_header("content-length", "0")
        self.end_headers()


upstream = HTTPServer(("127.0.0.1", 0), _Upstream)
assert not 5588 <= upstream.server_address[1] <= 5613 and \
    not 21000 <= upstream.server_address[1] <= 22999, "reserved port"
threading.Thread(target=upstream.serve_forever, daemon=True).start()
UPSTREAM_URL = f"http://127.0.0.1:{upstream.server_address[1]}"
MARK = "ROLLING_CONTEXT_WROTE_BASE_URL"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---- S1: README points at the fork ---------------------------------------
print("S1 README / fork")
readme = (REPO / "README.md").read_text()
install = readme[readme.index("## Install"):readme.index("## How Compression Works")]
check("install section clones the fork",
      "git clone https://github.com/LupoGrigi0/claude-rolling-context.git" in install)
check("install section clones nothing from NodeNestor",
      "NodeNestor/" not in install and "nestor-plugins" not in install)

# ---- S3: no port -> refuse, nothing written -------------------------------
print("S3 no port configured")
b = Box()
r = b.sh("install.sh")
check("install with no port exits non-zero", r.returncode != 0, r.stdout[-300:])
check("...and says why", "no Ferry port configured" in r.stdout, r.stdout[-300:])
check("...and writes no ANTHROPIC_BASE_URL",
      not (b.settings_env() or {}).get("ANTHROPIC_BASE_URL"), b.settings_env())
r = b.sh("hooks/start-proxy.sh")
check("hook with no port exits non-zero, loudly on stdout",
      r.returncode != 0 and "[FERRY] ERROR" in r.stdout, (r.returncode, r.stdout))
check("hook with no port started nothing", b.pid() is None)
b.close()

def prefs(port, **more):
    return {"ferry": dict({"port": port}, **more)}


def health(port):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", "/health")
        return json.loads(c.getresponse().read())
    finally:
        c.close()


# ---- S4: one source, per uid, in the home --------------------------------
print("S4 config: preferences > built-in default, nothing else")
b = Box(prefs=prefs(5711, data_dir="/tmp/x"),
        settings={"env": {"ROLLING_CONTEXT_PORT": "5588",
                          "ROLLING_CONTEXT_UPSTREAM": "https://from-settings.example"}})
out = b.conf("env").stdout
check("preferences port wins over a settings.json copy", "ROLLING_CONTEXT_PORT=5711" in out, out)
out = b.conf("env", ROLLING_CONTEXT_PORT=5713, FERRY_DATA="/tmp/env",
             ROLLING_CONTEXT_UPSTREAM="https://from-env.example").stdout
check("the process env is NOT a config source (port)", "ROLLING_CONTEXT_PORT=5711" in out, out)
check("...nor data_dir", "FERRY_DATA=/tmp/x" in out, out)
check("...nor upstream; settings.json is not one either -> the default",
      "ROLLING_CONTEXT_UPSTREAM=https://api.anthropic.com" in out, out)
check("curation is always ferry (no field, no install-only default)",
      "ROLLING_CONTEXT_CURATION=ferry" in out, out)
b.close()
b = Box()
out = b.conf("env", ROLLING_CONTEXT_PORT=5713).stdout
check("a port ONLY in the env is still 'no port configured'",
      "no Ferry port configured" in out, out)
check("data_dir defaults to ~/ferry-data", f"FERRY_DATA={b.home / 'ferry-data'}" in out, out)
(b.home / "preferences.json").write_text("{not json")
out = b.conf("env").stdout
check("malformed preferences.json is an error, not silence",
      "FERRY_CONFIG_ERROR='config unreadable" in out, out)
(b.home / "preferences.json").write_text(json.dumps(prefs(5714, upstream="http://127.0.0.1:5714")))
out = b.conf("env").stdout
check("an upstream that is Ferry's own port is refused (it would loop)",
      "own port" in out, out)
b.close()

print("S4 install guard: a non-Anthropic route already in settings.json")
OR = "https://openrouter.ai/api"
for label, settings in (
        ("a non-Ferry ANTHROPIC_BASE_URL", {"env": {"ANTHROPIC_BASE_URL": OR}}),
        ("an apiKeyHelper", {"apiKeyHelper": "/bin/echo k", "env": {}}),
        ("an older Ferry's chained ROLLING_CONTEXT_UPSTREAM",
         {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
                  MARK: "http://127.0.0.1:5588", "ROLLING_CONTEXT_UPSTREAM": OR}})):
    port = free_port()
    b = Box(prefs=prefs(port), settings=settings)
    before = (b.cfg / "settings.json").read_text()
    r = b.sh("install.sh")
    check(f"{label}, no ferry.upstream: install refuses", r.returncode != 0, r.stdout[-400:])
    check("...naming the key to declare in preferences.json",
          '"upstream"' in r.stdout and "preferences.json" in r.stdout, r.stdout[-400:])
    if "apiKeyHelper" not in label:
        check("...and the exact value to put there", f'"upstream": "{OR}"' in r.stdout,
              r.stdout[-400:])
    check("...settings.json byte-identical", (b.cfg / "settings.json").read_text() == before)
    check("...no proxy started", b.pid() is None)
    r = b.sh("hooks/start-proxy.sh")
    check("...and the hook refuses the same way, starting nothing",
          r.returncode != 0 and "[FERRY] ERROR" in r.stdout and b.pid() is None,
          r.stdout[-300:])
    if MARK in settings["env"]:
        env = b.settings_env()
        check("...and takes back its own dead pointer (restoring the chained route)",
              env.get("ANTHROPIC_BASE_URL") == OR, env)
    else:
        check("...settings.json still byte-identical after the hook",
              (b.cfg / "settings.json").read_text() == before)
    if b.pid() and alive(b.pid()):      # a regression started one: stop OURS only
        b.stop_started(b.pid())
    b.close()

# The guard also sees a route Ferry already REPLACED (its key is still there)
# and the launcher's env route (point's settings value would outrank it).
for label, settings, extra in (
        ("a key left behind after Ferry replaced the route",
         {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5716",
                  MARK: "http://127.0.0.1:5716", "ANTHROPIC_AUTH_TOKEN": "sk-or-FAKE"}}, {}),
        ("a launcher-env non-Anthropic ANTHROPIC_BASE_URL", {"env": {}},
         {"ANTHROPIC_BASE_URL": OR, "ANTHROPIC_AUTH_TOKEN": "sk-or-FAKE"})):
    b = Box(prefs=prefs(5716), settings=settings)
    out = b.conf("env", **extra).stdout
    check(f"{label}, no ferry.upstream: env reports a config error",
          "FERRY_CONFIG_ERROR=''" not in out and "upstream" in out, out)
    r = b.conf("point", **extra)
    check("...and point refuses", r.returncode != 0, r.stdout)
    b.close()

# Controls: the guard must NOT fire on an Anthropic route or once upstream is declared.
b = Box(prefs=prefs(5715), settings={"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}})
r = b.conf("install")
check("control: an Anthropic base URL in settings is not refused", r.returncode == 0, r.stdout)
b.close()
b = Box(prefs=prefs(5715, upstream=OR), settings={"env": {"ANTHROPIC_BASE_URL": OR}})
r = b.conf("install")
r2 = b.conf("point")
env = b.settings_env()
check("control: ferry.upstream declared -> install passes, point takes the route",
      r.returncode == 0 and r2.returncode == 0
      and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:5715", (r.stdout, r2.stdout, env))
b.conf("withdraw")
env = b.settings_env()
check("...and withdraw puts the mind's own route (preferences upstream) back",
      env.get("ANTHROPIC_BASE_URL") == OR, env)
b.close()

b = Box(prefs=prefs(5715, upstream="https://api.anthropic.com"),
        settings={"apiKeyHelper": "/bin/echo k", "env": {}})
b.conf("point")
b.conf("unpoint")
env = b.settings_env()
check("upstream = Anthropic: unpoint leaves NO base URL (never pins one Ferry did not find)",
      "ANTHROPIC_BASE_URL" not in env, env)
b.close()

print("S4 settings.json carries only the base URL")
b = Box(prefs=prefs(5712),
        settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
                          "ROLLING_CONTEXT_PORT": "5588", "FERRY_DATA": "/old",
                          "ROLLING_CONTEXT_CURATION": "ferry",
                          "ROLLING_CONTEXT_TRIGGER": "100000",
                          "ROLLING_CONTEXT_TARGET": "40000", "KEEP": "1"}})
r = b.conf("install")
r = b.conf("point") if r.returncode == 0 else r
env = b.settings_env()
check("legacy 5588 install moves to the declared port, no self-chain",
      r.returncode == 0 and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:5712",
      (r.stdout[-300:], env))
check("...and settings.json now holds ONLY the base URL + MARK (+ the user's own keys)",
      env == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5712",
              MARK: "http://127.0.0.1:5712", "KEEP": "1"}, env)
b.close()

b = Box(settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
                          "ROLLING_CONTEXT_PORT": "5711", "KEEP": "1"}})
r = b.conf("unpoint")
env = b.settings_env()
check("uninstall leaves a base URL Ferry did not write",
      env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8080" and env.get("KEEP") == "1"
      and "ROLLING_CONTEXT_PORT" not in env, env)
b.close()

# ---- S3/S4: a real launch, verified, as the user, then uninstalled ----------
print("S3 real launch on the preferences port")
port = free_port()
# A decoy port from the TEST range, never a reserved one: run against code that
# honours the env (i.e. the bug), a decoy of 5588 would bind a live mind's port.
decoy = next(p for p in range(5799, 5699, -1) if p != port)
b = Box(prefs=prefs(port, upstream=UPSTREAM_URL))
started = None
try:
    # What Claude Code would inject into the hook's env from an old settings.json:
    # none of it may change the proxy's route or port.
    r = b.sh("install.sh", ROLLING_CONTEXT_UPSTREAM="https://wrong.example",
             ANTHROPIC_BASE_URL="https://wrong.example", ROLLING_CONTEXT_PORT=decoy)
    started = b.pid()
    check("install starts the proxy", r.returncode == 0 and started,
          r.stdout[-600:] + r.stderr[-300:])
    env = b.settings_env() or {}
    check("settings.json written under CLAUDE_CONFIG_DIR, NOT $HOME/.claude",
          env and not (b.home / ".claude" / "settings.json").exists())
    check("plugin linked under CLAUDE_CONFIG_DIR",
          (b.cfg / "plugins" / "rolling-context").is_symlink())
    check("per-mind port honoured: the pid we started holds it",
          started and b.conf("holds-port", started, port).returncode == 0)
    check("settings hold ONLY ANTHROPIC_BASE_URL + MARK",
          env == {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                  MARK: f"http://127.0.0.1:{port}"}, env)
    fetch = b.home / ".local" / "bin" / "ferry-fetch"
    check("ferry-fetch linked into ~/.local/bin, at the repo's bin/",
          fetch.is_symlink() and fetch.resolve() == (REPO / "bin" / "ferry-fetch").resolve(),
          fetch)
    r2 = b.run("bash", "-c", "command -v ferry-fetch",
               PATH=f"{b.home}/.local/bin:/usr/bin:/bin")
    check("...so a bare `ferry-fetch` resolves on a PATH with ~/.local/bin",
          r2.returncode == 0 and r2.stdout.strip() == str(fetch), (r2.returncode, r2.stdout))
    check("...and install warns when ~/.local/bin is NOT on PATH",
          "not on PATH" in r.stdout, r.stdout[-400:])
    if started:
        h = health(port)
        check("the proxy forwards to preferences' upstream, not the inherited env's",
              h.get("upstream_url") == UPSTREAM_URL, h.get("upstream_url"))
        check("the proxy is running as this uid",
              os.stat(f"/proc/{started}").st_uid == os.getuid())
        r = b.run(sys.executable, str(REPO / "tools" / "ferry-canary"))
        check("ferry-canary (manual, no flags) proves this mind's proxy",
              r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr)
        r = b.sh("hooks/start-proxy.sh")
        check("second hook run reuses the verified proxy, starts no other",
              r.returncode == 0 and b.pid() == started, r.stdout)
        (b.cfg / "rolling-context-proxy.pid").unlink()
        r = b.sh("hooks/start-proxy.sh")
        check("no pidfile: the hook still finds THIS uid's proxy on the port and reuses it",
              r.returncode == 0 and b.pid() == started, (r.stdout, b.pid(), started))
        # A changed upstream must not be silently served by the old proxy.
        (b.home / "preferences.json").write_text(
            json.dumps(prefs(port, upstream="https://changed.example")))
        r = b.sh("hooks/start-proxy.sh")
        env = b.settings_env() or {}
        check("upstream changed in preferences: the hook does NOT silently reuse",
              r.returncode != 0 and "does not match" in r.stdout and alive(started)
              and env.get("ANTHROPIC_BASE_URL") != f"http://127.0.0.1:{port}",
              (r.stdout, env))
        (b.home / "preferences.json").write_text(json.dumps(prefs(port, upstream=UPSTREAM_URL)))
        r = b.sh("hooks/start-proxy.sh")
        check("...preferences back: reused again and re-pointed",
              r.returncode == 0 and b.pid() == started
              and (b.settings_env() or {}).get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}",
              r.stdout)
    r = b.sh("uninstall.sh")
    for _ in range(40):
        if not started or not alive(started):
            break
        time.sleep(0.25)
    check("uninstall stops the proxy it started", started and not alive(started), r.stdout)
    env = b.settings_env() or {}
    check("uninstall replaces Ferry's base URL with the mind's declared route",
          env == {"ANTHROPIC_BASE_URL": UPSTREAM_URL}, env)
    check("uninstall removes the ferry-fetch link it made",
          not (b.home / ".local" / "bin" / "ferry-fetch").is_symlink())
finally:
    if started and alive(started):
        b.stop_started(started)
    b.close()

# ---- S3: port held by someone else -> loud, settings untouched -----------
print("S3 port already held by another process")
port = free_port()
squatter = socket.socket()
squatter.bind(("127.0.0.1", port))
squatter.listen(1)
b = Box(prefs=prefs(port))
try:
    r = b.sh("hooks/start-proxy.sh")
    check("hook fails non-zero", r.returncode != 0, r.stdout)
    check("...loudly, naming the port", f"did not take port {port}" in r.stdout, r.stdout)
    check("...and does NOT point settings at a port it does not own",
          not (b.settings_env() or {}).get("ANTHROPIC_BASE_URL"), b.settings_env())
    check("...and leaves no pidfile", b.pid() is None)
finally:
    b.close()

# ...nor a config error (no port in preferences): same withdrawal.
b = Box(prefs={"ferry": {}}, settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5791",
                                               MARK: "http://127.0.0.1:5791"}})
r = b.sh("hooks/start-proxy.sh")
env = b.settings_env() or {}
check("config error withdraws the pointer Ferry wrote",
      r.returncode != 0 and "ANTHROPIC_BASE_URL" not in env, (r.stdout, env))
b.close()

# An EARLIER run's pointer must not survive a failed start: the next session
# would go to a dead port or to whoever now holds it. A URL Ferry did not
# write is left alone (inverse control).
url = f"http://127.0.0.1:{port}"
b = Box(prefs=prefs(port), settings={"env": {"ANTHROPIC_BASE_URL": url, MARK: url, "KEEP": "1"}})
try:
    r = b.sh("hooks/start-proxy.sh")
    env = b.settings_env() or {}
    check("failed start withdraws the pointer Ferry wrote, says so",
          r.returncode != 0 and "ANTHROPIC_BASE_URL" not in env and env.get("KEEP") == "1"
          and "Removed ANTHROPIC_BASE_URL" in r.stdout, (r.stdout, env))
finally:
    b.close()
b = Box(prefs=prefs(port, upstream="http://127.0.0.1:8080"),
        settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}})
try:
    r = b.sh("hooks/start-proxy.sh")
    env = b.settings_env() or {}
    check("...but a base URL Ferry did not write is left alone",
          r.returncode != 0 and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8080",
          (r.stdout, env))
finally:
    squatter.close()
    b.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
