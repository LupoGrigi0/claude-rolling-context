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
  S4  preferences.json "ferry" is read, env overrides it, a value it declares is
      not copied into settings; CLAUDE_CONFIG_DIR honoured; supervised -> the
      hook starts nothing and writes nothing; a loopback base URL Ferry did not
      write is chained, not treated as "already set", and uninstall leaves it.

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

# ---- S4: preferences.json + supervised + CLAUDE_CONFIG_DIR -----------------
print("S4 preferences.json, supervised, CLAUDE_CONFIG_DIR")
port = free_port()
b = Box(prefs={"ferry": {"port": port, "supervised": True}})
r = b.sh("install.sh")
check("supervised install with no proxy up: refuses, says why",
      r.returncode != 0 and "settings.json NOT pointed" in r.stdout,
      r.stdout[-400:] + r.stderr[-400:])
env = b.settings_env() or {}
check("...and writes NO ANTHROPIC_BASE_URL (nothing proved the port is ours)",
      "ANTHROPIC_BASE_URL" not in env, env)
check("settings.json written under CLAUDE_CONFIG_DIR", b.settings_env() is not None)
check("...and NOT under $HOME/.claude", not (b.home / ".claude" / "settings.json").exists())
check("plugin linked under CLAUDE_CONFIG_DIR",
      (b.cfg / "plugins" / "rolling-context").is_symlink())
check("curation=ferry written", env.get("ROLLING_CONTEXT_CURATION") == "ferry", env)
check("per-mind FERRY_DATA written", env.get("FERRY_DATA") == str(b.home / "ferry-data"), env)
check("port NOT copied into settings (preferences declares it)",
      "ROLLING_CONTEXT_PORT" not in env, env)
check("supervised install started no proxy", b.pid() is None)
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

before = (b.cfg / "settings.json").read_text()
r = b.sh("hooks/start-proxy.sh")
check("supervised hook exits 0", r.returncode == 0, r.stdout + r.stderr)
check("supervised hook started no proxy", b.pid() is None)
check("supervised hook did not rewrite settings.json",
      (b.cfg / "settings.json").read_text() == before)
other = free_port()
r = b.sh("hooks/start-proxy.sh", FERRY_SUPERVISED=1, ROLLING_CONTEXT_PORT=other)
check("FERRY_SUPERVISED=1 env alone: no proxy, no settings change",
      r.returncode == 0 and b.pid() is None
      and (b.cfg / "settings.json").read_text() == before, r.stdout)
s = socket.socket()
try:
    s.bind(("127.0.0.1", port))
    check("nothing is listening on the supervised port", True)
except OSError:
    check("nothing is listening on the supervised port", False)
finally:
    s.close()

# Positive: THIS mind's proxy on the port (launched here as an unsupervised
# stand-in for the unit), so the supervised install's canary passes and points.
started = None
try:
    r = b.sh("hooks/start-proxy.sh", FERRY_SUPERVISED=0, ROLLING_CONTEXT_UPSTREAM=UPSTREAM_URL)
    started = b.pid()
    b.conf("withdraw")
    r = b.sh("install.sh")
    env = b.settings_env() or {}
    check("supervised install, canary passes -> points at the preferences port",
          started and r.returncode == 0
          and env.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}",
          (r.stdout[-400:], env))
finally:
    if started and alive(started):
        b.stop_started(started)
b.close()

print("S4 precedence")
b = Box(prefs={"ferry": {"port": 5711, "data_dir": "/tmp/x"}},
        settings={"env": {"ROLLING_CONTEXT_PORT": "5588"}})
out = b.conf("env").stdout
check("preferences beats a settings.json copy", "ROLLING_CONTEXT_PORT=5711" in out, out)
out = b.conf("env", ROLLING_CONTEXT_PORT=5588).stdout
check("env equal to settings' value is settings, not an override",
      "ROLLING_CONTEXT_PORT=5711" in out, out)
out = b.conf("env", ROLLING_CONTEXT_PORT=5713).stdout
check("a real env override beats preferences", "ROLLING_CONTEXT_PORT=5713" in out, out)
check("data_dir from preferences", "FERRY_DATA=/tmp/x" in out, out)
check("the hook's env does NOT default curation (only install does)",
      "ROLLING_CONTEXT_CURATION=''" in out, out)
b.close()
b = Box()
(b.home / "preferences.json").write_text("{not json")
out = b.conf("env").stdout
check("malformed preferences.json is an error, not silence",
      "FERRY_CONFIG_ERROR='config unreadable" in out, out)
b.close()

# ---- S4: loopback base URL Ferry did not write ----------------------------
print("S4 foreign / legacy base URLs")
b = Box(settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}})
r = b.conf("point", ROLLING_CONTEXT_PORT=5714)
env = b.settings_env()
check("foreign loopback base URL is chained, not 'already'",
      r.stdout.strip() == "chained" and env.get("ROLLING_CONTEXT_UPSTREAM") == "http://127.0.0.1:8080"
      and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:5714", (r.stdout, env))
b.conf("unpoint")
env = b.settings_env()
check("uninstall restores the chained upstream",
      env == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}, env)
b.close()

b = Box(settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
                          "ROLLING_CONTEXT_PORT": "5711", "KEEP": "1"}})
r = b.conf("unpoint")
env = b.settings_env()
check("uninstall leaves a base URL Ferry did not write",
      env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8080" and env.get("KEEP") == "1"
      and "ROLLING_CONTEXT_PORT" not in env, env)
b.close()

b = Box(prefs={"ferry": {"port": 5712, "supervised": True}},
        settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
                          "ROLLING_CONTEXT_PORT": "5588"}})
r = b.conf("install")
r = b.conf("point") if r.returncode == 0 else r
env = b.settings_env()
check("legacy 5588 install moves to the declared port, no self-chain",
      r.returncode == 0 and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:5712"
      and "ROLLING_CONTEXT_UPSTREAM" not in env and "ROLLING_CONTEXT_PORT" not in env,
      (r.stdout[-300:], env))
b.close()

# ---- S3: a real launch, verified, then uninstalled -----------------------
print("S3 real launch on a per-mind port")
port = free_port()
b = Box()
started = None
try:
    r = b.sh("install.sh", ROLLING_CONTEXT_PORT=port)
    started = b.pid()
    check("install starts the proxy", r.returncode == 0 and started,
          r.stdout[-600:] + r.stderr[-300:])
    env = b.settings_env() or {}
    check("per-mind port honoured: the pid we started holds it",
          started and b.conf("holds-port", started, port).returncode == 0)
    check("settings point at that port only after verify",
          env.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}", env)
    check("env-sourced port persisted for the hook", env.get("ROLLING_CONTEXT_PORT") == str(port), env)
    check("curation=ferry written", env.get("ROLLING_CONTEXT_CURATION") == "ferry", env)
    if started:
        r = b.sh("hooks/start-proxy.sh")
        check("second hook run reuses the verified proxy, starts no other",
              r.returncode == 0 and b.pid() == started, r.stdout)
    r = b.sh("uninstall.sh")
    for _ in range(40):
        if not started or not alive(started):
            break
        time.sleep(0.25)
    check("uninstall stops the proxy it started", started and not alive(started), r.stdout)
    env = b.settings_env() or {}
    check("uninstall removes the base URL Ferry wrote", "ANTHROPIC_BASE_URL" not in env, env)
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
b = Box()
try:
    r = b.sh("hooks/start-proxy.sh", ROLLING_CONTEXT_PORT=port)
    check("hook fails non-zero", r.returncode != 0, r.stdout)
    check("...loudly, naming the port", f"did not take port {port}" in r.stdout, r.stdout)
    check("...and does NOT point settings at a port it does not own",
          not (b.settings_env() or {}).get("ANTHROPIC_BASE_URL"), b.settings_env())
    check("...and leaves no pidfile", b.pid() is None)
finally:
    b.close()

# An EARLIER run's pointer must not survive a failed start: the next session
# would go to a dead port or to whoever now holds it. A URL Ferry did not
# write is left alone (inverse control).
url = f"http://127.0.0.1:{port}"
b = Box(settings={"env": {"ANTHROPIC_BASE_URL": url, MARK: url, "KEEP": "1"}})
try:
    r = b.sh("hooks/start-proxy.sh", ROLLING_CONTEXT_PORT=port)
    env = b.settings_env() or {}
    check("failed start withdraws the pointer Ferry wrote, says so",
          r.returncode != 0 and "ANTHROPIC_BASE_URL" not in env and env.get("KEEP") == "1"
          and "Removed ANTHROPIC_BASE_URL" in r.stdout, (r.stdout, env))
finally:
    b.close()
b = Box(settings={"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}})
try:
    r = b.sh("hooks/start-proxy.sh", ROLLING_CONTEXT_PORT=port)
    env = b.settings_env() or {}
    check("...but a base URL Ferry did not write is left alone",
          r.returncode != 0 and env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8080",
          (r.stdout, env))
finally:
    squatter.close()
    b.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
