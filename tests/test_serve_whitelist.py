#!/usr/bin/env python3
"""Regression: the viewer must serve the DATA CONTRACT, not one hardcoded name.

THE INCIDENT (2026-09-05). I added feed-event logging so the reachability
signal would be computable, wrote feeds.csv beside metrics.csv, told Zara it
was ready -- and every viewer returned 404. The server had

    if path == "/metrics.csv":

and nothing else. The file was correct, deployed, and unreadable from where the
interface lives. The data directories are group-restricted, so there was no
filesystem fallback.

Zara's diagnosis is the reason this test exists, and it is about the class
rather than the case:

    "one static server per instance, serving exactly one hardcoded filename --
     and the moment the contract grew a second file that answer broke."

So the fix is NOT to add feeds.csv. It is to serve a WHITELIST, so the next
file added to the contract needs no code change here at all.

WHY A WHITELIST AND NOT A DIRECTORY. Widening what a URL can reach is exactly
where path traversal lives. An exact-basename whitelist means no user-supplied
path component ever reaches the filesystem: the request either IS one of the
known names or it is a 404. os.path.join with attacker-influenced input is the
bug this design refuses to have.

Run: python3 tests/test_serve_whitelist.py
Crossing-2d23. Stdlib only.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "srv", HERE.parent / "tools" / "ferry-metrics-serve.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

PASSED = 0
FAILED = []


def check(label, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}: got {got!r}, want {want!r}")


# ---- 1. the contract, read from the module ------------------------------
# DERIVE RULE: assert against the module's own whitelist. A test that restates
# the filenames passes happily after someone edits one and then the test and
# the server agree with each other and disagree with the data contract.
wl = getattr(srv, "DATA_FILES", None)
check("the server exposes a DATA_FILES whitelist", wl is not None, True)
if wl is not None:
    check("metrics.csv is served", "metrics.csv" in wl, True)
    check("feeds.csv is served", "feeds.csv" in wl, True)
    check("the whitelist is exact basenames, no paths",
          all("/" not in n and "\\" not in n and ".." not in n for n in wl), True)

# ---- 2. end to end on a real socket -------------------------------------
with tempfile.TemporaryDirectory() as d:
    Path(d, "metrics.csv").write_text("ts_iso,event\n2026-01-01T00:00:00Z,request\n")
    Path(d, "feeds.csv").write_text('ts_iso,instance,kind,rc,note\n'
                                    '2026-01-01T00:00:00Z,ferry,feed,0,"slab=5k"\n')
    Path(d, "secret.txt").write_text("must never be served\n")

    httpd = srv.Server(("127.0.0.1", 0), srv.Handler)   # port 0: never collide
    httpd.csv_path = os.path.join(d, "metrics.csv")
    httpd.data_dir = d
    httpd.quiet = True                 # Handler reads self.server.quiet
    httpd.instance_name = "test"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def get(p):
        try:
            with urllib.request.urlopen(base + p, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return f"ERR {type(e).__name__}", b""

    code, body = get("/metrics.csv")
    check("GET /metrics.csv -> 200", code, 200)
    check("  and it is the metrics content", b"ts_iso,event" in body, True)

    code, body = get("/feeds.csv")
    check("GET /feeds.csv -> 200 (THE BUG THIS FIXES)", code, 200)
    check("  and it is the feed content", b"instance,kind,rc" in body, True)
    check("  quoted note survives the round trip", b'"slab=5k"' in body, True)

    code, _ = get("/healthz")
    check("GET /healthz still 200", code, 200)

    # ---- 3. everything else is refused, including traversal -------------
    for probe in ("/secret.txt", "/../secret.txt", "/..%2fsecret.txt",
                  "/metrics.csv/../secret.txt", "/etc/passwd",
                  "/%2e%2e%2fmetrics.csv", "/feeds.csv.bak"):
        code, body = get(probe)
        check(f"refused: {probe}", code == 404 or code == 400, True)
        check(f"  leaked nothing: {probe}", b"must never be served" not in body, True)

    httpd.shutdown()

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
