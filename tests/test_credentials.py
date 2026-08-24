#!/usr/bin/env python3
"""Credential termination — the proxy holds the upstream key, minds do not.

WHY THIS EXISTS: the alternative for the Phase E fleet was giving every
instance that runs Ferry a copy of the upstream API key. A key that lives in
N home directories is a key with N ways to leak — and these minds publish
tool output to a live web mirror, so one unscrubbed traceback puts it on the
internet. With the proxy terminating credentials, a mind talks plain HTTP to
localhost and holds no secret at all.

Every assertion here is a security property. If one goes red, do not "fix
the test".

Run: python3 tests/test_credentials.py
Crossing-2d23 <crossing-2d23@smoothcurves.nexus>. Stdlib only.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_CURATION", "ferry")
os.environ.setdefault("FERRY_DATA", tempfile.mkdtemp())

import server  # noqa: E402

passed = 0
failed = []


def check(name, cond, detail=None):
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


def clear_env():
    os.environ.pop("FERRY_UPSTREAM_KEY", None)
    os.environ.pop("FERRY_UPSTREAM_KEY_FILE", None)


CLIENT = {"authorization": "Bearer client-own-key",
          "x-api-key": "client-own-key",
          "content-type": "application/json",
          "host": "should-be-dropped"}
SECRET = "sk-or-v1-REALUPSTREAMSECRET"

print("no proxy key configured (transparent pass-through):")
clear_env()
h = server._forward_headers(dict(CLIENT))
check("client's own credentials pass through untouched",
      h.get("authorization") == "Bearer client-own-key")
check("host is still dropped", "host" not in {k.lower() for k in h})

print("\nproxy key via env:")
clear_env()
os.environ["FERRY_UPSTREAM_KEY"] = SECRET
h = server._forward_headers(dict(CLIENT))
check("authorization is replaced with the PROXY's key",
      h["authorization"] == f"Bearer {SECRET}", h.get("authorization"))
check("x-api-key is replaced too (Anthropic-format upstreams read this one)",
      h["x-api-key"] == SECRET)
check("the client's own key is GONE, not merely shadowed — a stale key must "
      "never reach upstream",
      "client-own-key" not in repr(h), h)
check("exactly one authorization header survives",
      len([k for k in h if k.lower() == "authorization"]) == 1, list(h))

print("\ncase-insensitive replacement (clients vary):")
clear_env()
os.environ["FERRY_UPSTREAM_KEY"] = SECRET
h = server._forward_headers({"Authorization": "Bearer client-own-key",
                             "X-Api-Key": "client-own-key"})
check("a capitalised Authorization is still replaced, not duplicated",
      len([k for k in h if k.lower() == "authorization"]) == 1
      and "client-own-key" not in repr(h), list(h))

print("\nkey file is preferred over env (files are not in /proc/environ):")
clear_env()
kf = Path(tempfile.mkdtemp()) / "key"
kf.write_text("sk-or-v1-FROMFILE\n")
os.environ["FERRY_UPSTREAM_KEY"] = SECRET
os.environ["FERRY_UPSTREAM_KEY_FILE"] = str(kf)
h = server._forward_headers(dict(CLIENT))
check("file wins over env var", h["x-api-key"] == "sk-or-v1-FROMFILE",
      h.get("x-api-key"))
check("trailing newline is stripped (a \\n in a header is a protocol error)",
      "\n" not in h["x-api-key"])

print("\nfailure modes stay LOUD, never silently wrong:")
clear_env()
os.environ["FERRY_UPSTREAM_KEY_FILE"] = "/nonexistent/nope"
h = server._forward_headers(dict(CLIENT))
check("an unreadable key file falls back to the caller rather than sending "
      "an empty credential", h["authorization"] == "Bearer client-own-key")

clear_env()
os.environ["FERRY_UPSTREAM_KEY"] = "   "
h = server._forward_headers(dict(CLIENT))
check("a whitespace-only key is treated as ABSENT, never injected as empty",
      h["authorization"] == "Bearer client-own-key")

print("\nthe secret never reaches the logs:")
clear_env()
os.environ["FERRY_UPSTREAM_KEY"] = SECRET
records = []


class Grab(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


lg = logging.getLogger("rolling-context")
old_level = lg.level
h_ = Grab()
lg.addHandler(h_)
lg.setLevel(logging.DEBUG)
server._forward_headers(dict(CLIENT))
lg.removeHandler(h_)
lg.setLevel(old_level)
check("no log line contains the upstream secret",
      not any(SECRET in m for m in records), records)
check("no log line contains the client's key either",
      not any("client-own-key" in m for m in records), records)
check("the log DOES say a key was injected (silent security is unauditable)",
      any("upstream key injected" in m for m in records), records)

clear_env()
print(f"\n{passed} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
