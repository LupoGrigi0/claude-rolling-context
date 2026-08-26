#!/usr/bin/env python3
"""Regression: Ferry must stop curating when it is provably not converging.

2026-08-26: three instances burned 12.1 MILLION input tokens in three minutes.
21 curations in two minutes, two turns evicted per cycle, context pinned in the
low 83,000s against a target of 12,000. Every cycle "succeeded". Every one was
logged at INFO. The resident set never moved, because the unevictable floor
(system prompt + MCP tool defs + CLAUDE.md, ~83,000 tokens) was far above the
target and no amount of eviction could reach it.

THE FIXTURES BELOW ARE OBSERVED, NOT INVENTED. Every landing value is a real
number from that run's metrics.csv. A test built only from fixtures I made up
tests my model of the world, not the world -- five green-but-wrong fixtures
this week taught that the hard way.

Run: python3 tests/test_thrash_detector.py
Crossing-2d23. Stdlib only.
"""
import importlib.util, logging, os, sys, types
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.thrash-test-home")

# server.py imports its siblings by bare name (`import endpoints`), so the
# proxy dir has to be on the path before it is executed.
sys.path.insert(0, str(HERE.parent / "proxy"))

# Load server.py WITHOUT running its main(): import as a module object only.
spec = importlib.util.spec_from_file_location("ferry_server", HERE.parent / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec)
sys.modules["ferry_server"] = srv
spec.loader.exec_module(srv)

log = logging.getLogger("test"); log.addHandler(logging.NullHandler())

PASSED = 0; FAILED = []
def check(name, got, want):
    global PASSED
    if got == want: PASSED += 1
    else: FAILED.append(f"{name}: expected {want!r}, got {got!r}")

def reset(target, tolerance=1.25, strikes=3):
    srv.TARGET_TOKENS = target
    srv.THRASH_TOLERANCE = tolerance
    srv.THRASH_STRIKES = strikes
    srv._convergence.update(awaiting=False, strikes=0, locked_out=False, last_landing=None)

def land(value):
    """Simulate a curation that landed at `value` tokens."""
    srv._convergence["awaiting"] = True
    srv._thrash_note_landing(value, log, None)

# ---- OBSERVED LANDINGS: these three must classify correctly ---------------

reset(target=100000)
land(96277)      # fairie   02:37:01, real
check("fairie landed 96,277 under a 100,000 target -> productive",
      srv._convergence["strikes"], 0)

reset(target=100000)
land(111007)     # passenger 03:12:27, real: SHORT but 128 turns of real progress
check("passenger landed 111,007 (11% over target) -> within tolerance, productive",
      srv._convergence["strikes"], 0)

reset(target=12000)
land(83840)      # the thrash, real
check("thrash landed 83,840 against a 12,000 target -> unproductive",
      srv._convergence["strikes"], 1)
check("one bad cycle does NOT lock out — a short cycle self-corrects",
      srv._convergence["locked_out"], False)

# ---- THE ACTUAL THRASH SEQUENCE, verbatim from metrics.csv ---------------

reset(target=12000)
for v in (83840, 84684, 84884, 87134, 85349):
    land(v)
check("the real thrash sequence locks curation out", srv._convergence["locked_out"], True)

# ---- A short cycle followed by recovery must NOT lock out ----------------

reset(target=100000)
land(140000); land(140000)          # two strikes
check("two strikes, still curating", srv._convergence["locked_out"], False)
land(96277)                          # recovers
check("a good landing resets the strike count", srv._convergence["strikes"], 0)
land(140000); land(140000)
check("strikes accumulate again from zero, no lockout yet",
      srv._convergence["locked_out"], False)

# ---- The lockout must be able to END -------------------------------------

# The lockout MUST be escapable. Observed live 2026-08-26 08:36Z: passenger
# locked out at a floor of 134,043 against a target of 100,000, and the only
# exit was "context below target" -- unreachable, because the lockout disables
# the only mechanism that lowers context. It would have climbed to the model's
# hard limit and rejected every request. A safety mechanism whose failure mode
# is worse than the failure it prevents is not one.
srv.TRIGGER_TOKENS = 150000
reset(target=100000)
for _ in range(3): land(140000)
check("locked out after three", srv._convergence["locked_out"], True)
check("the floor we locked out at is recorded",
      srv._convergence["floor_at_lockout"], 140000)

# retry_at = max(TRIGGER, floor + (TRIGGER-TARGET)//2) = max(150000, 165000)
srv._thrash_maybe_clear(150000, log)
check("above target but BELOW the retry threshold: stays locked",
      srv._convergence["locked_out"], True)
srv._thrash_maybe_clear(164999, log)
check("one token below the retry threshold: still locked",
      srv._convergence["locked_out"], True)
srv._thrash_maybe_clear(165000, log)
check("at the retry threshold there is new evictable material: unlocks",
      srv._convergence["locked_out"], False)

reset(target=100000)
for _ in range(3): land(140000)
srv._thrash_maybe_clear(40000, log)
check("a context genuinely below target clears it (new session, smaller tools)",
      srv._convergence["locked_out"], False)

reset(target=100000)
srv._thrash_maybe_clear(999999, log)
check("clearing does nothing when not locked out",
      srv._convergence["locked_out"], False)

# The not-locked-out early return is load-bearing, and mutation testing on
# 2026-08-26 found it had NO covering test: removing it left all 16 green.
# What it protects: without the guard, any context observed below target resets
# the STRIKE COUNTER even when no lockout exists -- so a thrash whose context
# dips below target between cycles could never accumulate its third strike and
# would run forever. Third time this week a surviving mutant exposed a guard
# nothing tested.
reset(target=100000)
land(140000); land(140000)
check("two strikes accumulated", srv._convergence["strikes"], 2)
srv._thrash_maybe_clear(50000, log)      # a dip below target, NOT locked out
check("a dip below target must NOT wipe strikes while not locked out",
      srv._convergence["strikes"], 2)
land(140000)
check("so the third strike still locks out", srv._convergence["locked_out"], True)

# ---- A landing nobody was awaiting must be ignored ------------------------

reset(target=12000)
srv._convergence["awaiting"] = False
srv._thrash_note_landing(83840, log, None)
check("a landing with no curation in flight is not counted",
      srv._convergence["strikes"], 0)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED: print(f"  FAIL {f}")
sys.exit(1 if FAILED else 0)
