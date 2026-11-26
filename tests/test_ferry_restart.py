#!/usr/bin/env python3
"""bin/ferry-restart bounces THIS mind's proxy without landing the mind.

Temp HOME + a stub upstream on an OS-chosen port; Ferry's port from 5700-5799.
Never a house port, never the real API. Stops only PIDs this test saw start.

  1. nothing running        -> starts one, canary passes, exit 0
  2. its own proxy running  -> a NEW pid holds the port, the old one is gone
  3. port held by something that is NOT a Ferry server.py -> refuses, kills nothing
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# POSIX BY DESIGN. On Windows, announce a NAMED could-not-run before touching anything.
# The exemption itself lives in tests/run_all.py ENV_BOUND_WINDOWS, granted by a human --
# a suite never grants itself one. Lodestone-8ec9, 2026-11-26.
if os.name == "nt":
    print("SKIP(env): drives bin/ferry-restart (a bash script) and reads /proc/<pid>/cmdline; neither exists on Windows")
    print("0 passed, 0 failed")
    raise SystemExit(0)

REPO = Path(__file__).resolve().parent.parent
from _isolate import home_env  # HOME alone is not isolation on Windows
RESTART = REPO / "bin" / "ferry-restart"
CONF = REPO / "hooks" / "ferry_config.py"
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


def free_test_port():
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


class Stub(BaseHTTPRequestHandler):
    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"type":"error","error":{"type":"authentication_error"}}')

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


stub = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
port = free_test_port()

root = Path(tempfile.mkdtemp(prefix="ferry-restart-test-"))
home, cfg = root / "home", root / "cfg"
home.mkdir()
cfg.mkdir()
(home / "preferences.json").write_text(json.dumps(
    {"ferry": {"port": port, "upstream": f"http://127.0.0.1:{stub.server_port}"}}))
env = {k: v for k, v in os.environ.items()
       if not k.startswith(("ROLLING_CONTEXT_", "FERRY_", "ANTHROPIC_")) and k != "CLAUDE_CONFIG_DIR"}
env.update(home_env(home), CLAUDE_CONFIG_DIR=str(cfg))
started = set()


def run(*argv):
    r = subprocess.run(list(map(str, argv)), env=env, capture_output=True, text=True,
                       timeout=90, cwd=str(root))
    return r.returncode, (r.stdout + r.stderr).strip()


def holder():
    code, out = run(sys.executable, CONF, "who-holds", port)
    return int(out) if code == 0 and out.strip().isdigit() else None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


try:
    # 1. nothing running -> starts one
    code, out = run(RESTART)
    pid1 = holder()
    if pid1:
        started.add(pid1)
    check("no proxy running -> exit 0", code == 0, out[-400:])
    check("...and a proxy of ours now holds the port", pid1 is not None, out[-200:])
    check("...the canary ran and passed", "ferry-canary: OK" in out, out[-300:])

    # 2. its own proxy running -> a NEW pid
    code, out = run(RESTART)
    pid2 = holder()
    if pid2:
        started.add(pid2)
    check("bounce -> exit 0", code == 0, out[-400:])
    check("...a NEW pid holds the port", pid2 is not None and pid2 != pid1, (pid1, pid2))
    check("...and the old pid is gone", pid1 is not None and not alive(pid1), pid1)
    check("...settings still point at Ferry's port",
          json.loads((cfg / "settings.json").read_text(encoding="utf-8"))["env"]["ANTHROPIC_BASE_URL"]
          == f"http://127.0.0.1:{port}")

    # stop our proxy (verified) so the port can be taken by an impostor
    if pid2 and alive(pid2) and "server.py" in Path(f"/proc/{pid2}/cmdline").read_text(encoding="utf-8"):
        os.kill(pid2, signal.SIGTERM)
        for _ in range(50):
            if holder() is None:
                break
            time.sleep(0.2)

    # 3. an impostor (ours, but not a Ferry server.py) on the port -> refuse, kill nothing
    impostor = subprocess.Popen([sys.executable, "-c",
                                 "import socket,time; s=socket.socket(); "
                                 "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                                 f"s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(60)"])
    for _ in range(50):                       # the precondition is MEASURED, not assumed
        if holder() == impostor.pid:
            break
        time.sleep(0.2)
    if holder() != impostor.pid:
        impostor.kill()
        raise SystemExit("COULD-NOT-LOOK: the impostor never took the port")
    code, out = run(RESTART)
    p3 = holder()
    if p3 and p3 != impostor.pid:
        started.add(p3)
    check("impostor on the port -> refuses (non-zero)", code != 0, out[-300:])
    check("...says it is not a Ferry server.py", "not a Ferry server.py" in out, out[-300:])
    check("...and did NOT kill it", impostor.poll() is None)
    impostor.kill()
    impostor.wait()
finally:
    for pid in started:
        try:
            if alive(pid) and "server.py" in Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8"):
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    stub.shutdown()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
