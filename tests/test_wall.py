#!/usr/bin/env python3
"""The window wall: a session near its OWN wall that Ferry can never curate.

Measured 2026-11-16: a 1M session adds "context-1m-<date>" to anthropic-beta; the
model id is identical. Forge (2026-11-17) came back on 200k with trigger 800k and
hit a hard wall that looked like a normal 88% from inside. Side traffic on 200k
(haiku relays, subagents) is small and must stay quiet.
"""
import importlib.util
import logging
import os
import sys
import tempfile
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


work = tempfile.mkdtemp(prefix="ferry-wall-")
os.environ.update(HOME=work, FERRY_DATA=os.path.join(work, "data"), ROLLING_CONTEXT_PORT="5799",
                  ROLLING_CONTEXT_TRIGGER="800000", ROLLING_CONTEXT_TARGET="600000",
                  ROLLING_CONTEXT_LOG=os.path.join(work, "debug.log"))
os.makedirs(os.path.join(work, ".claude"))
sys.path.insert(0, str(REPO / "proxy"))
spec = importlib.util.spec_from_file_location("ferry_server_wall", REPO / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
log = logging.getLogger("wall-test")

ONE_M = "claude-code-20250219,oauth-2025-04-20,context-1m-2025-08-07,interleaved-thinking-2025-05-14"
PLAIN = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14"

check("1M beta -> 1,000,000 window", srv._window_of(ONE_M) == 1_000_000)
check("no 1M beta -> 200,000 window", srv._window_of(PLAIN) == 200_000)

srv._wall_alarm = None
srv._check_wall(PLAIN, 53_659, log)
check("control: a 53k haiku relay on 200k stays quiet", srv._wall_alarm is None)
srv._check_wall(ONE_M, 900_000, log)
check("control: 900k on a 1M window with trigger 800k stays quiet", srv._wall_alarm is None)

srv._check_wall(PLAIN, 176_900, log)
check("Forge's case: 176.9k on 200k, trigger 800k -> ALARM", srv._wall_alarm is not None)
a = srv._wall_alarm or {}
check("...records window, context, trigger and a timestamp",
      a.get("window") == 200_000 and a.get("context_tokens") == 176_900
      and a.get("trigger_tokens") == 800_000 and a.get("at", "").endswith("Z"), a)

srv._wall_alarm = None
srv.TRIGGER_TOKENS = 150_000
srv._check_wall(PLAIN, 176_900, log)
check("control: same request but trigger 150k (Ferry CAN curate) -> quiet", srv._wall_alarm is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
