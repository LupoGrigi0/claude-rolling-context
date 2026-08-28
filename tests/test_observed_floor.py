#!/usr/bin/env python3
"""Regression: the unevictable floor must be DERIVED, not estimated.

THE BUG, caught live on 2026-08-28. Every curation logged:

    TARGET UNREACHABLE: system prompt + tool definitions are ~102,444
    tokens, already above the target of 100,000. Even evicting every
    message leaves ~102,444. Raise ROLLING_CONTEXT_TARGET above 102,444.

Twenty minutes later the same proxy landed a curation at 97,486.

You cannot land BELOW a floor that is genuinely unevictable. The instrument
contradicted itself inside one run, and its advice -- raise the target above
104,072 -- would have made things strictly worse: a higher target evicts
LESS, so context would have settled higher still.

THE CAUSE. `unevictable = real_token_count - msg_tokens`, where msg_tokens is
a chars/4 estimate. Markdown prose tokenises nearer 3.2 chars/token, so
msg_tokens comes out LOW, so unevictable comes out HIGH.

    An estimate used as a subtrahend inherits its error with the sign flipped.

THE FIX. Ferry already sees the REAL token count on every request. The
smallest context it has ever observed is a hard upper bound on the floor:
context = floor + messages, and messages is never negative, so
floor <= min(observed context). That is a measurement, not a guess, and it
costs one integer.

Estimate and bound are combined by taking the SMALLER. The bound cannot be
too low; the estimate has been proven to run high.

Run: python3 tests/test_observed_floor.py
Crossing-2d23. Stdlib only.
"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.floor-test-home")
os.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-test-debug.log")

spec = importlib.util.spec_from_file_location(
    "ferry_compressor", HERE.parent / "proxy" / "compressor.py")
comp = importlib.util.module_from_spec(spec)
sys.modules["ferry_compressor"] = comp
spec.loader.exec_module(comp)

spec_s = importlib.util.spec_from_file_location(
    "ferry_server2", HERE.parent / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec_s)
sys.modules["ferry_server2"] = srv
spec_s.loader.exec_module(srv)

PASSED = 0
FAILED = []


def check(name, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


# ---- 1. The server tracks the smallest real context it has seen ------------

check("floor tracker exists", hasattr(srv, "_note_observed_floor"), True)

if hasattr(srv, "_note_observed_floor"):
    srv._observed["floor"] = None
    check("starts unknown, NOT zero — blank is not zero",
          srv._observed["floor"], None)

    srv._note_observed_floor(150_000)
    check("first real count becomes the bound", srv._observed["floor"], 150_000)

    srv._note_observed_floor(97_486)
    check("a smaller context lowers the bound", srv._observed["floor"], 97_486)

    srv._note_observed_floor(181_891)
    check("a LARGER context does not raise it", srv._observed["floor"], 97_486)

    # Garbage must not poison the bound. A 0 or None from a failed parse would
    # otherwise pin the floor at zero forever and silently disable the fix.
    # Junk inputs are fed through a try/except deliberately: without the
    # tracker's type guard, `None < int` raises TypeError, and an uncaught
    # raise ABORTS the run so the suite reports nothing at all rather than one
    # red assertion. That is how a mutation hides. Fail by assertion, always.
    junk_raised = None
    for bad in (0, None, -5, "97486", 12.5):
        try:
            srv._note_observed_floor(bad)
        except Exception as e:                   # noqa: BLE001 - the point
            junk_raised = f"{bad!r} -> {e!r}"
            break
    check("junk observations never raise", junk_raised, None)
    check("and junk never moves the floor", srv._observed["floor"], 97_486)


# ---- 2. The compressor prefers the bound when it is tighter ----------------
# Reproduces the live numbers exactly. real=181,891 with 308,446 chars of
# messages gave unevictable~104,780 by subtraction; the same proxy later
# landed at 97,486, so the true floor is at most that.

def unevictable_for(real, msg_chars, observed_floor):
    """Call the module's own helper so this tests the shipped arithmetic."""
    return comp.effective_unevictable(real, msg_chars, observed_floor)


check("helper exists", hasattr(comp, "effective_unevictable"), True)

if hasattr(comp, "effective_unevictable"):
    est_only = unevictable_for(181_891, 308_446, None)
    check("with no observation, behaviour is unchanged (the old estimate)",
          est_only, max(0, 181_891 - (308_446 // 4)))

    bounded = unevictable_for(181_891, 308_446, 97_486)
    check("an observed landing below the estimate WINS", bounded, 97_486)
    check("and it is genuinely lower than the estimate",
          bounded < est_only, True)

    # The bound must never INFLATE the estimate. If we have only ever seen
    # large contexts, the bound is loose and the estimate stays.
    loose = unevictable_for(181_891, 308_446, 175_000)
    check("a loose bound does not raise the estimate",
          loose, est_only)

    # Degenerate inputs must not produce a negative or absurd floor.
    check("floor is never negative",
          unevictable_for(1_000, 400_000, None) >= 0, True)
    check("zero observation is ignored by the helper",
          unevictable_for(181_891, 308_446, 0), est_only)


# ---- 3. THE LIVE CONTRADICTION MUST NOW BE IMPOSSIBLE ----------------------
# The whole point. Given a landing that was actually observed, the reported
# floor must never exceed it -- because claiming a floor above a context the
# proxy has already reached is a self-contradiction, and it is what sent an
# operator an instruction to raise the target.

if hasattr(comp, "effective_unevictable"):
    OBSERVED_LANDINGS = [100_172, 97_486, 99_614, 117_072, 98_847, 97_572]
    lowest = min(OBSERVED_LANDINGS)
    reported = unevictable_for(181_891, 308_446, lowest)
    check("reported floor never exceeds the lowest context actually reached",
          reported <= lowest, True)
    check("specifically: not the 104,780 that was logged live",
          reported < 104_780, True)


print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
