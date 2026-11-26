#!/usr/bin/env python3
"""The proxy must IMPORT on a Python with no os.getuid (Windows).

2026-11-26: the privacy commits called os.getuid() at import; on Windows the
proxy died with AttributeError, never bound its port, and a mind lost its
route for hours. Simulate Windows by removing getuid before importing.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from _isolate import home_env  # HOME alone is not isolation on Windows
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


work = tempfile.mkdtemp(prefix="ferry-nouid-")
data = os.path.join(work, "ferry-data")
os.makedirs(data)
open(os.path.join(work, "debug.log"), "w").close()   # pre-existing log: the line-105 path
open(os.path.join(data, "metrics.csv"), "w").close()  # pre-existing data: the walk path
os.makedirs(os.path.join(work, ".claude"))
os.environ.update(home_env(work))
os.environ.update(FERRY_DATA=data, ROLLING_CONTEXT_CURATION="ferry",
                  ROLLING_CONTEXT_PORT="5799", ROLLING_CONTEXT_LOG=os.path.join(work, "debug.log"))
# On POSIX, SIMULATE Windows by removing getuid. On a Python that genuinely has no
# getuid (Windows) there is nothing to remove -- the import below IS the real case,
# measured directly; reaching for os.getuid here used to crash the suite before it
# measured anything (AttributeError at `saved = os.getuid`).
SIMULATED = hasattr(os, "getuid")
if SIMULATED:
    saved = os.getuid
    del os.getuid
try:
    sys.path.insert(0, str(REPO / "proxy"))
    spec = importlib.util.spec_from_file_location("ferry_server_nouid", REPO / "proxy" / "server.py")
    srv = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(srv)
        err = None
    except Exception as e:  # noqa: BLE001 -- the point is that NOTHING escapes
        err = f"{type(e).__name__}: {e}"
    check("proxy imports with no os.getuid (Windows)", err is None, err)
    check("...and skips the POSIX permission pass", err is None and srv._POSIX_UID is None)
finally:
    if SIMULATED:
        os.getuid = saved
if SIMULATED:
    check("control: os.getuid restored", hasattr(os, "getuid"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
