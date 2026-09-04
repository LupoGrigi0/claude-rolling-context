#!/usr/bin/env python3
"""Regression: silence in the FILE must be distinguishable from a dead writer.

THE FINDING (2026-09-04, Zara-c207 building the observation surface).
ferry's trace: 10,944 request rows and ELEVEN probe rows. The 7.6-hour and
3.2-hour silences contain NOTHING AT ALL.

  A dead proxy and an idle mind produce byte-identical output: no rows.

Zara's page reads activity and health from those rows. If the writer stops,
every cell reads "at rest, fine" -- a dead Ferry renders as the most
reassuring state on the page. That is the greyed-alarm bug one level up, and
no arrangement of the axes can catch it, because both go dark together.

WHY THIS MATTERS MORE THAN IT SOUNDS. Both silences over three hours in the
trace were FAILURES; every routine gap was under the load cadence. So
prolonged silence is not rest, it is the only signature a dead writer can
produce -- precisely because a dead writer produces nothing.

THE FIX: a pulse on a timer, independent of traffic. Then an absent row means
something. This is my own heartbeat.sh argument in the writer: a claim that
nothing happened is false unless something is alive to make it. ferry-watch
says that sentence in every quiet report; the file it reads could not.

NOT REDUNDANT WITH A READER-SIDE CLOCK. The pulse makes silence meaningful in
the FILE, for every reader including one opening the archive cold months
later. A reader's clock makes it meaningful NOW and survives the writer being
the thing that died -- which the pulse cannot cover, since a dead writer emits
no pulse either. Two witnesses, different processes, different evidence.

SUPPRESSION IS THE POINT. A pulse that fires during busy traffic is noise, and
a monitor you learn to skim is already broken regardless of whether its output
is correct.

Run: python3 tests/test_liveness_pulse.py
Crossing-2d23. Stdlib only.
"""
import csv
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.liveness-test-home")
os.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-test-debug.log")
# metrics are a Ferry feature and get_writer returns None outside curation mode
os.environ.setdefault("ROLLING_CONTEXT_CURATION", "ferry")

spec = importlib.util.spec_from_file_location(
    "metrics", HERE.parent / "proxy" / "metrics.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)

PASSED = 0
FAILED = []


def check(label, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}: got {got!r}, want {want!r}")


# ---- 1. the contract ------------------------------------------------------
# DERIVE RULE: read the module's own whitelist, never restate the list here.
check("'alive' is a contract event", "alive" in metrics.EVENTS, True)
check("adding it did not drop an existing event",
      all(e in metrics.EVENTS for e in
          ("proxy_start", "request", "probe", "curation", "archive_write",
           "fetch", "restart", "error", "gate")), True)

# ---- 2. the decision, as a pure function ---------------------------------
# Tested directly rather than through a sleep, so the assertion is about the
# PRODUCTION predicate and not about scheduler luck.
sp = getattr(metrics, "_should_pulse", None)
check("the pulse decision is an inspectable function", callable(sp), True)
if callable(sp):
    check("silent for exactly the interval -> pulse", sp(1000.0, 900.0, 100.0), True)
    check("silent for longer -> pulse",               sp(1000.0, 800.0, 100.0), True)
    check("recent write -> SUPPRESSED",               sp(1000.0, 995.0, 100.0), False)
    check("write in the future (clock skew) -> suppressed, never negative",
          sp(1000.0, 1500.0, 100.0), False)
    check("interval 0 disables the pulse",            sp(1000.0, 0.0, 0.0), False)
    check("negative interval disables the pulse",     sp(1000.0, 0.0, -5.0), False)
    # never-written case: last_write None must not crash and must pulse
    try:
        got = sp(1000.0, None, 100.0)
    except Exception as e:
        got = f"RAISED {type(e).__name__}"
    check("never-written (None) pulses rather than raising", got, True)

# ---- 3. end to end, on the real writer ------------------------------------
def rows_of(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

with tempfile.TemporaryDirectory() as d:
    w = metrics.get_writer(d)
    check("writer constructed", w is not None, True)
    if w is not None:
        started = w.start_liveness(0.15) if hasattr(w, "start_liveness") else None
        check("start_liveness exists and reports it started",
              bool(started), True)
        time.sleep(0.55)                      # several intervals of pure silence
        p = getattr(w, "path", None) or os.path.join(d, "metrics.csv")
        quiet = [r for r in rows_of(p) if r["event"] == "alive"]
        check("silence produces alive rows", len(quiet) >= 2, True)

        # BLANK IS NOT ZERO -- an alive row measured nothing and must say so.
        if quiet:
            numeric = ("context_tokens", "tokens_in", "tokens_evicted",
                       "archive_lines", "archive_bytes", "carry_chars")
            blanks = all(quiet[0][k] == "" for k in numeric)
            check("alive row leaves every numeric field BLANK, not 0", blanks, True)
            check("alive row carries a timestamp", bool(quiet[0]["ts_iso"]), True)

        # suppression: traffic must silence the pulse
        before = len([r for r in rows_of(p) if r["event"] == "alive"])
        for _ in range(6):
            w.row("request", context_tokens=100000)
            time.sleep(0.05)
        after = len([r for r in rows_of(p) if r["event"] == "alive"])
        check("busy traffic SUPPRESSES the pulse (no new alive rows)",
              after - before, 0)

# ---- 4. off by default is NOT the answer, but it must be switchable -------
check("interval is configurable via env", 
      hasattr(metrics, "LIVENESS_SEC"), True)
if hasattr(metrics, "LIVENESS_SEC"):
    check("default interval is shorter than the ~30min load cadence, so a "
          "routine gap is covered", 0 < metrics.LIVENESS_SEC <= 900, True)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
