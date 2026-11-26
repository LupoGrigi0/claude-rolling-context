#!/usr/bin/env python3
"""The mind's session on disk is the mind's alone (Zara, 2026-11-26).

The archive, proxy-state.json and metrics are a mind's session, much of it
verbatim. Under umask 0002 and a 0755 home they were readable by every local
user, and /debug/compressions served the curated prefix to anyone on loopback.
"""
import http.client
import importlib.util
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


os.umask(0o002)                                   # the box's default
work = tempfile.mkdtemp(prefix="ferry-privacy-")
data = os.path.join(work, "ferry-data")
os.makedirs(data, mode=0o775)                     # an earlier Ferry's world-readable dir
os.chmod(data, 0o775)
os.makedirs(os.path.join(data, "archive"), mode=0o775)
for f in ("metrics.csv", "proxy-state.json", "archive/archive_20261101.jsonl"):
    open(os.path.join(data, f), "w").close()
    os.chmod(os.path.join(data, f), 0o664)        # left world-readable by an older Ferry
os.makedirs(os.path.join(work, ".claude"))
open(os.path.join(work, "debug.log"), "w").close()   # a pre-existing log...
os.chmod(os.path.join(work, "debug.log"), 0o664)      # ...left world-readable
os.environ.update(HOME=work, FERRY_DATA=data, ROLLING_CONTEXT_CURATION="ferry",
                  ROLLING_CONTEXT_PORT="5799", ROLLING_CONTEXT_UPSTREAM="http://127.0.0.1:9",
                  ROLLING_CONTEXT_LOG=os.path.join(work, "debug.log"))
os.environ.pop("FERRY_DEBUG_ENDPOINTS", None)
sys.path.insert(0, str(REPO / "proxy"))
spec = importlib.util.spec_from_file_location("ferry_server_priv", REPO / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


check("existing world-readable data dir is tightened to 0700", mode(data) == 0o700, oct(mode(data)))
for f in ("metrics.csv", "proxy-state.json", "archive/archive_20261101.jsonl"):
    check(f"pre-existing {f} tightened to 0600", mode(os.path.join(data, f)) == 0o600,
          oct(mode(os.path.join(data, f))))
check("pre-existing archive/ dir tightened to 0700", mode(os.path.join(data, "archive")) == 0o700)
probe = os.path.join(data, "probe.jsonl")
open(probe, "w").close()
check("files created by the proxy process are 0600", mode(probe) == 0o600, oct(mode(probe)))
d2 = os.path.join(data, "archive2")
os.makedirs(d2)
check("dirs created by the proxy process are 0700", mode(d2) == 0o700, oct(mode(d2)))
log = os.path.join(work, "debug.log")
check("a pre-existing world-readable debug log is tightened", os.path.exists(log) and not (mode(log) & 0o044), oct(mode(log)) if os.path.exists(log) else "absent")

proxy = srv.ThreadedHTTPServer(("127.0.0.1", 0), srv.ProxyHandler)
threading.Thread(target=proxy.serve_forever, daemon=True).start()
port = proxy.server_address[1]


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


st, body = get("/debug/compressions")
check("/debug/compressions is OFF by default (404)", st == 404, st)
check("...answered locally, not forwarded upstream", b"FERRY_DEBUG_ENDPOINTS" in body, body[:80])
srv.DEBUG_ENDPOINTS = True
st, body = get("/debug/compressions")
check("control: FERRY_DEBUG_ENDPOINTS=1 serves it (200)", st == 200, st)
st, _ = get("/health")
check("control: /health still served", st == 200, st)
proxy.shutdown()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
