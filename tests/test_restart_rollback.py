#!/usr/bin/env python3
"""F10: ferry-restart rolls back on its own -- the mind never has to act.

A throwaway clone of this repo gets a GOOD commit and then a BAD one whose
proxy dies at import (the Windows os.getuid shape, 2026-11-26). Stub upstream
on an OS port; Ferry's port from 5700-5799. Stops only PIDs it saw start.

  1. good code             -> exit 0, last-good-commit recorded
  2. bad code, good known  -> exit 3: ROLLED BACK, the GOOD commit serving
  3. bad code, nothing good to go back to -> exit 1: WITHDRAWN, base URL gone
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolate import home_env  # noqa: E402

if not hasattr(os, "getuid"):
    print("SKIP(env): drives bash ferry-restart and git checkout")
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
root = Path(tempfile.mkdtemp(prefix="ferry-rollback-"))
clone = root / "clone"
subprocess.run(["git", "clone", "-q", str(REPO), str(clone)], check=True)
git = lambda *a: subprocess.run(["git", "-C", str(clone), *a], capture_output=True, text=True)
git("config", "user.email", "t@t"); git("config", "user.name", "t")
GOOD = git("rev-parse", "HEAD").stdout.strip()
srv = clone / "proxy" / "server.py"
srv.write_text('raise AttributeError("simulated: os has no getuid")\n' + srv.read_text())
git("commit", "-qam", "BAD: dies at import")
BAD = git("rev-parse", "HEAD").stdout.strip()

home, cfg = root / "home", root / "cfg"
home.mkdir(); cfg.mkdir()
data = home / "ferry-data"
(home / "preferences.json").write_text(json.dumps(
    {"ferry": {"port": port, "upstream": f"http://127.0.0.1:{stub.server_port}"}}))
env = {k: v for k, v in os.environ.items()
       if not k.startswith(("ROLLING_CONTEXT_", "FERRY_", "ANTHROPIC_")) and k != "CLAUDE_CONFIG_DIR"}
env.update(home_env(home), CLAUDE_CONFIG_DIR=str(cfg))
seen = set()


def run():
    r = subprocess.run(["bash", str(clone / "bin" / "ferry-restart")], env=env,
                       capture_output=True, text=True, timeout=180, cwd=str(root))
    return r.returncode, (r.stdout + r.stderr)


def holder():
    r = subprocess.run([sys.executable, str(clone / "hooks" / "ferry_config.py"), "who-holds", str(port)],
                       env=env, capture_output=True, text=True)
    p = r.stdout.strip()
    if p.isdigit():
        seen.add(int(p))
        return int(p)
    return None


def base_url():
    try:
        return json.loads((cfg / "settings.json").read_text()).get("env", {}).get("ANTHROPIC_BASE_URL")
    except (OSError, ValueError):
        return None


try:
    # 1. good code
    git("checkout", "-q", "--detach", GOOD)
    code, out = run()
    holder()
    check("good code -> exit 0", code == 0, out[-300:])
    _lg = data / "last-good-commit"
    check("...last-good-commit records it", _lg.exists() and _lg.read_text().strip() == GOOD)

    # 2. bad code, a good commit known -> rolled back, good code serving
    git("checkout", "-q", "--detach", BAD)
    code, out = run()
    p = holder()
    check("bad code -> exit 3 (ROLLED BACK)", code == 3, out[-400:])
    check("...says ROLLED BACK", "ROLLED BACK" in out, out[-300:])
    check("...clone is back on the GOOD commit", git("rev-parse", "HEAD").stdout.strip() == GOOD)
    check("...and a proxy holds the port", p is not None)
    check("...settings still point at Ferry", base_url() == f"http://127.0.0.1:{port}", base_url())

    # 3. bad code, nothing good to go back to -> withdrawn
    git("checkout", "-q", "--detach", BAD)
    (data / "last-good-commit").write_text(BAD + "\n")
    (data / "started-commit").write_text(BAD + "\n")
    code, out = run()
    check("nothing good to roll back to -> exit 1 (WITHDRAWN)", code == 1, out[-400:])
    check("...says WITHDRAWN", "WITHDRAWN" in out, out[-300:])
    # "Direct" = the mind's own route: none for Anthropic, or the declared
    # non-Anthropic upstream restored (so its key never goes to the wrong place).
    check("...Ferry's base URL is gone: the mind goes direct to its upstream",
          base_url() in (None, f"http://127.0.0.1:{stub.server_port}"), base_url())
    check("...in particular NOT Ferry's port", base_url() != f"http://127.0.0.1:{port}")
    check("...and nothing of ours holds the port", holder() is None)
finally:
    for pid in seen:
        try:
            if "server.py" in Path(f"/proc/{pid}/cmdline").read_text():
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    stub.shutdown()
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
