#!/usr/bin/env python3
"""Regression: Ferry must not fire again before the last cycle has LANDED.

THE BUG (DESIGN-REV2 §20.3): the trigger has no memory of having fired.
Curation runs in the background and takes effect on a LATER request, so Ferry
re-triggers against the pre-curation context and evicts a handful of turns for
nothing. Observed on a live fairy, back to back: 851 / 1,182 / 1,291 / 2,320
tokens evicted, and later the degenerate form -- 21 cycles in two minutes,
turns=2 each, context pinned, 12.1 MILLION tokens spent moving nothing.

THE FIX is an observation, not a timer: `_convergence["awaiting"]` is set when a
cycle starts and cleared only when a request is seen CARRYING the curated
prefix. That is "has it landed", measured rather than guessed.

THE RATE LIMITS are two-mode, per Lupo: a limiter that is right in the steady
state can be catastrophic in a panic, so recovery mode suspends them while
context is above trigger AND still falling. It never suspends the landing check.

  A limiter must prevent THRASHING and never prevent WORKING,
  and "is it still coming down?" is what tells those apart.

Run: python3 tests/test_hysteresis.py
Crossing-2d23. Stdlib only.
"""
import importlib.util, logging, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.hyst-test-home")
spec = importlib.util.spec_from_file_location("ferry_server", HERE.parent / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec); sys.modules["ferry_server"] = srv
spec.loader.exec_module(srv)

log = logging.getLogger("t"); log.addHandler(logging.NullHandler())
PASSED = 0; FAILED = []
def check(name, got, want):
    global PASSED
    if got == want: PASSED += 1
    else: FAILED.append(f"{name}: expected {want!r}, got {got!r}")

def reset(trigger=150000, interval=10.0, maxcycles=4, window=300.0):
    srv.TRIGGER_TOKENS = trigger
    srv.MIN_CYCLE_INTERVAL = interval
    srv.MAX_CYCLES_PER_WINDOW = maxcycles
    srv.CYCLE_WINDOW_SECONDS = window
    srv._hysteresis.update(last_cycle_at=0.0, recent=[], last_trigger_ctx=None)
    srv._convergence.update(awaiting=False, strikes=0, locked_out=False,
                            last_landing=None, floor_at_lockout=None)

def gate(ctx, t): return srv._hysteresis_gate(ctx, t, log)[0]
def why(ctx, t):  return srv._hysteresis_gate(ctx, t, log)[1]

# ---- THE LANDING CHECK: the bug this exists for ---------------------------

reset()
check("first cycle is allowed", gate(160000, 100.0), True)
srv._hysteresis_note_cycle(160000, 100.0)
srv._convergence["awaiting"] = True
# NOTE: the landing check must TIME OUT. `awaiting` is cleared only when a
# request is seen carrying the curated prefix — so a compression that FAILS, or
# a client that stops talking, would pin it True forever and Ferry would never
# curate again. Third deadlock of this exact shape in one day: a guard whose
# release depends on the very thing the guard prevents. Every one needs a second
# exit that does not. Found by test_toggle regressing, not by inspection.
check("a second cycle is REFUSED while the first has not landed",
      gate(160500, 101.0), False)
check("and it says why", "has not landed" in why(160500, 101.0), True)

srv.LANDING_TIMEOUT = 90.0
check("still refused just under the landing timeout",
      gate(160500, 100.0 + 89.0), False)
srv._convergence["awaiting"] = True   # gate may have cleared it; re-arm
check("past the landing timeout it PROCEEDS rather than stalling forever",
      gate(160500, 100.0 + 91.0), True)
srv._convergence["awaiting"] = True
check("and it says the cycle was never observed",
      "never observed" in why(160500, 100.0 + 91.0), True)

srv._convergence["awaiting"] = True

# the landing arrives (a request carried the curated prefix)
srv._convergence["awaiting"] = False
check("once it lands, a later cycle is allowed again", gate(160500, 200.0), True)

# ---- THE OBSERVED THRASH, replayed ---------------------------------------
# Real contexts from 2026-08-26 01:53:30-01:53:42, seconds apart, each of which
# fired a curation that evicted ~2 turns and changed nothing.
reset(trigger=30000)
allowed = []
for i, (ctx, t) in enumerate([(86306,0.0),(83840,2.0),(84684,5.0),
                              (84884,7.0),(87134,10.0),(85349,12.0)]):
    ok = gate(ctx, t)
    allowed.append(ok)
    if ok:
        srv._hysteresis_note_cycle(ctx, t)
        srv._convergence["awaiting"] = True     # a real cycle would be in flight
check("the real thrash sequence fires ONCE, not six times",
      sum(allowed), 1)

# ---- RATE LIMITS in the steady state --------------------------------------

# NOTE ON SEMANTICS, learned by writing this test wrong first:
# the gate is only ever consulted when context is ABOVE trigger. So `falling` is
# False exactly when context grew or stalled — which means the rate limits apply
# precisely in the thrash case and nowhere else. A context that is above trigger
# and coming DOWN is Ferry working, and the limiter must never interrupt that.
# My first version asserted a falling context should be rate-limited; the code
# refused, and the code was right.
reset(interval=10.0)
srv._hysteresis_note_cycle(155000, 100.0)
check("too soon, and context is NOT falling -> refused",
      gate(160000, 105.0), False)
check("and it says why", "since the last cycle" in why(160000, 105.0), True)
check("after the interval it is allowed", gate(160000, 115.0), True)

reset(interval=10.0)
srv._hysteresis_note_cycle(160000, 100.0)
check("too soon BUT context is falling and still above trigger -> allowed, "
      "because that is Ferry working and a limiter must not stop work",
      gate(155000, 105.0), True)

reset(interval=0.0, maxcycles=3, window=300.0)
for t in (10.0, 20.0, 30.0):
    srv._hysteresis_note_cycle(155000, t)
check("the per-window cap refuses the 4th", gate(155000, 40.0), False)
check("and it says why", "in the last" in why(155000, 40.0), True)
check("outside the window it is allowed again", gate(155000, 400.0), True)

# ---- RECOVERY MODE: Lupo's panic case -------------------------------------
# Context way over trigger; Ferry needs several back-to-back maximum evictions.
# The rate limits must NOT hold it underwater while it is making progress.

reset(interval=30.0, maxcycles=2, window=300.0)
srv._hysteresis_note_cycle(400000, 100.0)          # huge context, first cycle
check("1s later, still 300k over trigger and FALLING -> recovery allows it",
      gate(300000, 101.0), True)
check("and it names recovery", "recovery mode" in why(300000, 101.0), True)
srv._hysteresis_note_cycle(300000, 101.0)
check("still falling, still above trigger -> still allowed",
      gate(220000, 102.0), True)

# but recovery is NOT a blank cheque
reset(interval=30.0)
srv._hysteresis_note_cycle(400000, 100.0)
check("above trigger but NOT falling -> that is thrashing, limits apply",
      gate(400000, 101.0), False)
check("falling but BELOW trigger -> ordinary work, limits apply",
      gate(100000, 101.0), False)

# and recovery never suspends the landing check
reset(interval=30.0)
srv._hysteresis_note_cycle(400000, 100.0)
srv._convergence["awaiting"] = True
check("recovery does NOT override the landing check",
      gate(300000, 101.0), False)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED: print(f"  FAIL {f}")
sys.exit(1 if FAILED else 0)
