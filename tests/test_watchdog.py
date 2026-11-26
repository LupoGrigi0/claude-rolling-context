#!/usr/bin/env python3
"""tools/ferry-watchdog: the outside actor repairs and ALERTS; when healthy it is silent.

Temp HOME, stub upstream, Ferry port 5700-5799, a fake `hacs` that records
alerts instead of sending them. Stops only PIDs it saw.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolate import home_env  # noqa: E402

if not hasattr(os, "getuid"):
    print("SKIP(env): bash watchdog + cron")
    print("0 passed, 0 failed")
    raise SystemExit(0)

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


class Stub(BaseHTTPRequestHandler):
    def _a(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"type":"error"}')

    do_GET = do_POST = _a

    def log_message(self, *a):
        pass


def free_port():
    for p in range(5700, 5800):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit("no free port")


stub = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
port = free_port()
root = Path(tempfile.mkdtemp(prefix="ferry-wd-"))
home, cfg = root / "home", root / "cfg"
home.mkdir(); cfg.mkdir()
(home / "preferences.json").write_text(json.dumps(
    {"ferry": {"port": port, "upstream": f"http://127.0.0.1:{stub.server_port}"}}))
alerts = root / "alerts.txt"
fake = root / "hacs"
fake.write_text(f'#!/bin/sh\n[ "$1" = whoami ] && echo Test-0000 && exit 0\n'
                f'echo "TO=$2 SUBJ=$3" >> {alerts}\ncat >> {alerts}\n')
fake.chmod(0o755)
env = {k: v for k, v in os.environ.items()
       if not k.startswith(("ROLLING_CONTEXT_", "FERRY_", "ANTHROPIC_")) and k != "CLAUDE_CONFIG_DIR"}
env.update(home_env(home), CLAUDE_CONFIG_DIR=str(cfg), FERRY_HACS=str(fake))
seen = set()


def wd():
    r = subprocess.run(["bash", str(REPO / "tools" / "ferry-watchdog")], env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def holder():
    r = subprocess.run([sys.executable, str(REPO / "hooks" / "ferry_config.py"), "who-holds", str(port)],
                       env=env, capture_output=True, text=True)
    p = r.stdout.strip()
    if p.isdigit():
        seen.add(int(p))
        return int(p)
    return None


try:
    code, out = wd()                         # no proxy at all -> repair + alert
    check("dead Ferry -> watchdog repairs it (exit 0)", code == 0, out[-300:])
    check("...a proxy now holds the port", holder() is not None)
    a = alerts.read_text() if alerts.exists() else ""
    check("...and ALERTS the mind itself", "TO=Test-0000" in a and "RESTARTED" in a, a[:200])

    alerts.unlink(missing_ok=True)
    code, out = wd()                         # healthy -> silent
    check("control: healthy Ferry -> exit 0", code == 0, out[-200:])
    check("control: healthy Ferry -> NO alert", not alerts.exists())

    # Forge's blocker: the UPSTREAM dies (network blip), Ferry is fine. The
    # watchdog must wait -- no restart, no rollback, no withdraw, no alert.
    before = holder()
    stub.shutdown(); stub.server_close()
    _c = subprocess.run([sys.executable, str(REPO / "tools" / "ferry-canary")], env=env,
                        capture_output=True, text=True, timeout=120)
    check("canary says UPSTREAM-DOWN with exit 3 (not a crash, not FAIL)",
          _c.returncode == 3 and "UPSTREAM-DOWN" in _c.stdout + _c.stderr,
          (_c.returncode, (_c.stdout + _c.stderr)[-200:]))
    code, out = wd()
    check("upstream down -> watchdog exits 0 and WAITS", code == 0 and "waiting" in out, out[-300:])
    check("...the SAME proxy pid still serves (no restart)", holder() == before, (before, holder()))
    check("...no alert for a blip that is not Ferry's", not alerts.exists())
    sj = json.loads((cfg / "settings.json").read_text()).get("env", {})
    check("...and Ferry is NOT withdrawn", sj.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}", sj)
finally:
    for pid in seen:
        try:
            if "server.py" in Path(f"/proc/{pid}/cmdline").read_text():
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        stub.shutdown()
    except Exception:
        pass

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
