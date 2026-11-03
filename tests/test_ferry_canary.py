#!/usr/bin/env python3
"""ferry-canary (S6), the lockout surface (S7) and debug-log rotation (S9) --
each at BOTH polarities. The canary is a MANUAL tool (no unit, no timer, no
--pid): a human runs it once at install, as the mind's user.

A canary that has never failed is not a canary: every OK below has a planted
wrong world next to it (nothing listening, not Ferry, another mind's proxy,
upstream dead, curation locked out, config unreadable) that must NOT be OK.

Everything binds port 0. The proxy runs IN-PROCESS on an OS-chosen port, in
front of a stub upstream on another; no live proxy and no real API is touched.
The canary itself runs as a subprocess, exactly as a human would run it.

Run: python3 tests/test_ferry_canary.py
"""
import json, logging, os, re, shutil, socket, subprocess, sys, tempfile, threading
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CANARY = REPO / "tools" / "ferry-canary"
FORBIDDEN = [(5588, 5588), (5610, 5613), (21000, 21005), (21097, 21097),
             (21099, 21099), (22000, 22999)]

PASSED = 0; FAILED = []
def check(name, got, want):
    global PASSED
    if got == want: PASSED += 1
    else: FAILED.append(f"{name}: expected {want!r}, got {got!r}")

SECRET = "sk-stub-SECRET-must-not-leak"


class Stub(BaseHTTPRequestHandler):
    """Upstream stand-in. Puts a fake secret in a header AND the body, so the
    no-leak check has something to find if the canary ever printed either."""
    protocol_version = "HTTP/1.1"
    status = 401
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"error": SECRET}).encode()
        self.send_response(Stub.status)
        self.send_header("content-type", "application/json")
        self.send_header("x-stub-token", SECRET)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(httpd):
    port = httpd.server_address[1]
    assert not any(lo <= port <= hi for lo, hi in FORBIDDEN), f"port {port} is reserved"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


work = tempfile.mkdtemp(prefix="ferry-canary-test-")
stub = HTTPServer(("127.0.0.1", 0), Stub)
stub_port = start(stub)

home = os.path.join(work, "home")
data = os.path.join(work, "ferry-data")
os.makedirs(os.path.join(home, ".claude"))
for k in ("ROLLING_CONTEXT_CURATION", "FERRY_UPSTREAM_KEY", "FERRY_UPSTREAM_KEY_FILE",
          "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"):
    os.environ.pop(k, None)
os.environ.update(HOME=home, FERRY_DATA=data, ROLLING_CONTEXT_PORT="5799",
                  ROLLING_CONTEXT_UPSTREAM=f"http://127.0.0.1:{stub_port}",
                  ROLLING_CONTEXT_LOG=os.path.join(work, "debug.log"))

sys.path.insert(0, str(REPO / "proxy"))
spec = importlib.util.spec_from_file_location("ferry_server", REPO / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec)
sys.modules["ferry_server"] = srv
spec.loader.exec_module(srv)

proxy = srv.ThreadedHTTPServer(("127.0.0.1", 0), srv.ProxyHandler)
proxy_port = start(proxy)


def canary(*args, env=None):
    r = subprocess.run([sys.executable, str(CANARY), *args], capture_output=True,
                       text=True, timeout=120, env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


def free_closed_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


try:
    # ---- S6: the healthy control ------------------------------------------
    code, out = canary("--port", str(proxy_port), "--data", data)
    check("healthy proxy -> exit 0", code, 0)
    check("healthy proxy -> says OK", out.startswith("ferry-canary: OK"), True)
    check("upstream's status reached the canary", "HTTP 401" in out, True)
    check("no header or body value leaks into output", SECRET in out, False)

    # ---- S6: each wrong world must NOT be OK --------------------------------
    code, out = canary("--port", str(free_closed_port()), "--data", data)
    check("nothing listening -> exit 1", code, 1)
    check("nothing listening -> says so", "nothing listening" in out, True)

    code, out = canary("--port", str(stub_port), "--data", data)
    check("a non-Ferry server on the port -> exit 1", code, 1)
    check("non-Ferry -> 'did not echo'", "did not echo" in out, True)
    check("non-Ferry -> no leak either", SECRET in out, False)

    other = os.path.join(work, "another-mind")
    os.makedirs(other)
    code, out = canary("--port", str(proxy_port), "--data", other)
    check("another mind's proxy (wrong data dir) -> exit 1", code, 1)
    check("wrong data dir -> 'another mind'", "another mind" in out, True)

    Stub.status = 503
    code, out = canary("--port", str(proxy_port), "--data", data)
    check("upstream 5xx -> exit 1", code, 1)
    Stub.status = 401

    # Upstream gone entirely: the proxy answers 502 itself.
    saved = srv._parsed_upstream
    srv._parsed_upstream = srv.urlparse(f"http://127.0.0.1:{free_closed_port()}")
    code, out = canary("--port", str(proxy_port), "--data", data)
    srv._parsed_upstream = saved
    check("upstream unreachable -> exit 1", code, 1)
    check("upstream unreachable -> 'HTTP 502'", "HTTP 502" in out, True)

    # COULD-NOT-LOOK: no flags, and no ~/preferences.json declares a port.
    prefs = os.path.join(home, "preferences.json")
    cfg_env = {k: v for k, v in os.environ.items() if k != "ROLLING_CONTEXT_PORT"}
    code, out = canary(env=cfg_env)
    check("config without a port -> exit 2 (COULD-NOT-LOOK)", code, 2)
    check("COULD-NOT-LOOK is labelled", "COULD-NOT-LOOK" in out, True)

    # The env is NOT a config source: a port only in the env is still no config.
    code, out = canary(env=dict(os.environ, ROLLING_CONTEXT_PORT=str(proxy_port)))
    check("port in the env only (no preferences) -> exit 2, env is not config", code, 2)

    # ...and the same resolver, given preferences.json, finds the proxy with no flags.
    with open(prefs, "w") as f:
        json.dump({"ferry": {"port": proxy_port, "data_dir": data}}, f)
    code, out = canary(env=cfg_env)
    check("preferences-declared port + data dir -> exit 0", code, 0)
    os.remove(prefs)

    # --pid (the systemd unit's $MAINPID coupling) is gone.
    code, out = canary("--port", str(proxy_port), "--data", data, "--pid", str(os.getpid()))
    check("--pid is no longer an option -> argparse error (exit 2)", code, 2)

    # ---- S7: lockout is ERROR, reported by /health, and fails the canary ---
    class Cap(logging.Handler):
        def __init__(self): super().__init__(); self.recs = []
        def emit(self, r): self.recs.append(r)
    cap = Cap(); lg = logging.getLogger("canary-test"); lg.addHandler(cap)
    lg.setLevel(logging.DEBUG); lg.propagate = False
    srv.TARGET_TOKENS, srv.THRASH_TOLERANCE, srv.THRASH_STRIKES = 12000, 1.25, 3
    srv._convergence.update(awaiting=False, strikes=0, locked_out=False,
                            last_landing=None, lockout_note=None)
    for v in (83840, 84684, 84884):            # the real 2026-08-26 thrash
        srv._convergence["awaiting"] = True
        srv._thrash_note_landing(v, lg, None)
    check("three bad landings -> locked out", srv._convergence["locked_out"], True)
    errs = [r for r in cap.recs if r.levelno >= logging.ERROR
            and "CURATION DISABLED" in r.getMessage()]
    check("the lockout transition is logged at ERROR, exactly once", len(errs), 1)

    code, out = canary("--port", str(proxy_port), "--data", data)
    check("locked-out proxy -> exit 1", code, 1)
    check("locked-out proxy -> 'curation disabled'", "curation disabled" in out, True)
    check("the lockout note reaches the canary", "floor >= 84,884" in out, True)

    # Healthy control AFTER lockout: clearing it must clear the alarm too.
    srv._thrash_maybe_clear(5000, lg)
    check("lockout cleared", srv._convergence["locked_out"], False)
    code, out = canary("--port", str(proxy_port), "--data", data)
    check("cleared lockout -> canary back to exit 0", code, 0)

    # Requests WITHOUT the canary header are untouched (every real request).
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    c.request("GET", "/health"); r = c.getresponse(); r.read(); c.close()
    check("no canary header -> no X-Ferry-Data added",
          r.getheader("X-Ferry-Data"), None)

    # ---- S9: the debug log rotates ------------------------------------------
    h = srv._log_handler
    check("debug log handler is a RotatingFileHandler",
          isinstance(h, logging.handlers.RotatingFileHandler), True)
    check("rotation is bounded (maxBytes > 0)", h.maxBytes > 0, True)
    check("a few backups are kept", 1 <= h.backupCount <= 5, True)
    h.maxBytes = 200
    for i in range(20):
        srv.log.debug(f"rotation filler line {i} " + "x" * 40)
    check("writing past maxBytes produced a .1 backup",
          os.path.exists(os.environ["ROLLING_CONTEXT_LOG"] + ".1"), True)
    check("the live log stayed bounded",
          os.path.getsize(os.environ["ROLLING_CONTEXT_LOG"]) <= 400, True)

    # ---- no service-manager coupling left in the tool -----------------------
    src = CANARY.read_text(encoding="utf-8")
    check("canary names no MAINPID / systemd / timer",
          any(w in src for w in ("MAINPID", "systemd", "ExecStartPost", "timer")), False)
    check("deploy/ (systemd units) is gone from the repo", (REPO / "deploy").exists(), False)
finally:
    proxy.shutdown(); stub.shutdown()
    shutil.rmtree(work, ignore_errors=True)

for f in FAILED:
    print("FAIL:", f)
print(f"\n{PASSED} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
