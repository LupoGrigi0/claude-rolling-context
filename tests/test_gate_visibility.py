#!/usr/bin/env python3
"""Regression: a curation Ferry DECLINED must be visible, and every tuning
parameter must be on the record.

THE PROBLEM this exists for. Hysteresis works by NOT curating. That means a
healthy run and a broken trigger produce the same picture -- a graph with
fewer curation marks on it. "Ferry held fire correctly" and "Ferry never
noticed it was over trigger" are the same absence, and an absence is exactly
the kind of evidence that fooled us before (§20: "Ferry did nothing" was
invisible at INFO and cost a whole run; the zero-fetch headline was a
permission error read as a finding).

So the decision to hold must leave a POSITIVE trace. `gate` rows are that
trace: one row each time the proxy was over trigger and chose not to act,
carrying the context it saw and the reason it gave.

AND the watermarks must be self-describing. A graph drawn with a trigger line
at 100k is a lie if the proxy was started with a trigger of 150k. The
parameters are emitted by the proxy that actually used them, into the same
append-only file as the data -- never typed into the visualizer by hand, and
never inferred from the shape of the curve.

THE DERIVE RULE. Every assertion below reads the module constant and compares
it to what the proxy EMITS. None of them restate a number. A test that says
`trigger=100000` passes happily after someone changes the default, and then
the graph and the test agree with each other and disagree with reality.

Run: python3 tests/test_gate_visibility.py
Crossing-2d23. Stdlib only.
"""
import csv
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.gate-test-home")
# Keep the suite OUT of the production debug log. Importing server.py
# installs a FileHandler; without this every test run appended THRASH
# WARNINGS to the log a real incident would be diagnosed from.
os.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-test-debug.log")

spec = importlib.util.spec_from_file_location(
    "ferry_server", HERE.parent / "proxy" / "server.py")
srv = importlib.util.module_from_spec(spec)
sys.modules["ferry_server"] = srv
spec.loader.exec_module(srv)

spec_m = importlib.util.spec_from_file_location(
    "ferry_metrics", HERE.parent / "proxy" / "metrics.py")
met = importlib.util.module_from_spec(spec_m)
sys.modules["ferry_metrics"] = met
spec_m.loader.exec_module(met)

PASSED = 0
FAILED = []


def check(name, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


def parse_note(note):
    """The contract the visualizer builds to: 'k=v, k=v, ...' -> dict.

    Deliberately the dumbest possible parser. If this cannot read the note,
    neither can the page, and the page must never guess.
    """
    out = {}
    for part in note.split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


# ---- 1. `gate` is a first-class event, and the whitelist still bites -------

check("gate is a contract event", "gate" in met.EVENTS, True)
check("proxy_start still a contract event", "proxy_start" in met.EVENTS, True)
check("typo events are still rejected", "gated" in met.EVENTS, False)

# ---- 2. The parameter note carries EVERY knob ------------------------------
# Each expected key is paired with the module attribute that owns it, so the
# test compares emitted-vs-actual and cannot drift into restating a literal.

KNOBS = {
    "trigger":          (srv, "TRIGGER_TOKENS"),
    "target":           (srv, "TARGET_TOKENS"),
    "landing_timeout":  (srv, "LANDING_TIMEOUT"),
    "min_interval":     (srv, "MIN_CYCLE_INTERVAL"),
    "max_cycles":       (srv, "MAX_CYCLES_PER_WINDOW"),
    "cycle_window":     (srv, "CYCLE_WINDOW_SECONDS"),
    "thrash_tolerance": (srv, "THRASH_TOLERANCE"),
    "thrash_strikes":   (srv, "THRASH_STRIKES"),
}

# REBIND EVERY KNOB TO A NON-DEFAULT, DISTINCTIVE VALUE FIRST.
#
# Without this the test is worthless and looks fine. Mutation M4 replaced
# `trigger={TRIGGER_TOKENS}` with a hardcoded `trigger=100000` and the suite
# stayed green -- because 100000 IS the default, so the emitted literal
# matched the constant it was supposed to be reading. A hardcoded parameter
# in the note is precisely the bug that would draw a 100k watermark over a
# run started at 150k. The values below are deliberately ugly and prime-ish
# so that nothing in the codebase can coincide with them.
srv.TRIGGER_TOKENS = 163841
srv.TARGET_TOKENS = 97393
srv.LANDING_TIMEOUT = 73.5
srv.MIN_CYCLE_INTERVAL = 17.25
srv.MAX_CYCLES_PER_WINDOW = 9
srv.CYCLE_WINDOW_SECONDS = 411.0
srv.THRASH_TOLERANCE = 1.37
srv.THRASH_STRIKES = 7

note = srv._params_note()
parsed = parse_note(note)

check("note reports the REBOUND trigger, not a default",
      parsed.get("trigger"), "163841")

for key, (mod, attr) in KNOBS.items():
    check(f"note carries {key}", key in parsed, True)
    if key in parsed:
        # str() of the live constant -- derived, never typed in.
        check(f"note {key} matches {attr}",
              parsed[key], str(getattr(mod, attr)))

check("note carries min_keep", "min_keep" in parsed, True)
check("note carries mode", parsed.get("mode"), "ferry")

# The note must survive being written into a CSV field. A comma-separated
# k=v string inside a comma-separated file is precisely the shape that gets
# silently mangled, so prove the round trip rather than assuming quoting.
with tempfile.TemporaryDirectory() as d:
    # Metrics are a Ferry feature and the writer is gated on curation mode --
    # get_writer returns None otherwise, by design. Turn it on explicitly
    # rather than depending on the ambient environment: a test that only
    # passes when someone else exported the right variable is not a test.
    os.environ["ROLLING_CONTEXT_CURATION"] = "ferry"
    check("writer is available in curation mode",
          met.get_writer(d) is not None, True)
    w = met.get_writer(d)
    w.row("proxy_start", window=262144, note=note)

    # EXERCISE THE PRODUCTION PATH, not a fixture. The first version of this
    # test wrote its own gate rows with w.row(...) and then asserted on them,
    # which tested the CSV writer and nothing else -- mutation M3, making a
    # real gate row claim `tokens_evicted=0`, survived a green suite. Call
    # the function the proxy actually calls.
    srv._metrics = w
    srv._note_gate("held: previous cycle has not landed yet (12s)",
                   161234, model="claude-haiku-4-5-20251001", window=262144)
    srv._note_gate("held: 5 cycles in the last 300s", 158000)

    rows = list(csv.DictReader(open(w.path)))

    # A gate row must never be written when metrics are off -- the helper is
    # called unconditionally from the request path, so its own guard is the
    # only thing standing between "metrics disabled" and an AttributeError
    # inside the hot path.
    srv._metrics = None
    try:
        srv._note_gate("this must not raise and must not appear", 999999)
        _raised = None
    except Exception as e:                       # noqa: BLE001 - that's the point
        _raised = repr(e)
    srv._metrics = w
    # Caught deliberately: removing the helper's `if not _metrics` guard makes
    # this line raise, and an uncaught raise ABORTS the run -- the suite then
    # reports nothing at all instead of one red assertion, which is how a
    # mutation hides. Fail by assertion, always.
    check("metrics-off gate call does not raise", _raised, None)
    check("no row written while metrics are off",
          len(list(csv.DictReader(open(w.path)))), len(rows))

check("three rows written", len(rows), 3)
if len(rows) == 3:
    check("proxy_start survives CSV", rows[0]["event"], "proxy_start")
    check("note survives CSV round trip",
          parse_note(rows[0]["note"]).get("trigger"),
          str(srv.TRIGGER_TOKENS))
    check("gate row is event=gate", rows[1]["event"], "gate")
    check("gate row carries the context it saw",
          rows[1]["context_tokens"], "161234")
    check("gate row carries a reason", bool(rows[1]["note"].strip()), True)
    # BLANK IS NOT ZERO: a gate row evicted nothing, and "nothing" must not
    # be written as 0 -- a 0 in tokens_evicted is a curation that achieved
    # nothing, which is a completely different and much worse event.
    check("gate row does NOT claim zero eviction",
          rows[1]["tokens_evicted"], "")
    check("gate row does not fake a cycle number", rows[1]["cycle"], "")

# ---- 3. Every reason the proxy can decline must be expressible -------------
# Not a string-match on the wording (that would restate the implementation) --
# a check that the gate's own reasons are non-empty and distinct, so two
# different declines never render as the same marker on the page.

import logging
log = logging.getLogger("t")
log.addHandler(logging.NullHandler())


def reset(trigger=150000, interval=10.0, maxcycles=4, window=300.0):
    srv.TRIGGER_TOKENS = trigger
    srv.MIN_CYCLE_INTERVAL = interval
    srv.MAX_CYCLES_PER_WINDOW = maxcycles
    srv.CYCLE_WINDOW_SECONDS = window
    srv._hysteresis.update(last_cycle_at=0.0, recent=[], last_trigger_ctx=None)
    srv._convergence.update(awaiting=False, strikes=0, locked_out=False,
                            last_landing=None, floor_at_lockout=None)


reasons = set()

# decline: the previous cycle has not landed
reset()
srv._hysteresis_note_cycle(160000, 100.0)
srv._convergence["awaiting"] = True
ok, why_landing = srv._hysteresis_gate(160000, 101.0, log)
check("landing check declines", ok, False)
reasons.add(why_landing)

# decline: minimum interval
reset()
srv._hysteresis_note_cycle(160000, 100.0)
ok, why_interval = srv._hysteresis_gate(160000, 102.0, log)
check("min-interval declines", ok, False)
reasons.add(why_interval)

# decline: too many cycles in the window
reset()
for i in range(4):
    srv._hysteresis_note_cycle(160000, 100.0 + i * 20)
ok, why_window = srv._hysteresis_gate(160000, 200.0, log)
check("per-window cap declines", ok, False)
reasons.add(why_window)

check("three declines give three distinct reasons", len(reasons), 3)
check("no decline reason is blank",
      all(r and r.strip() for r in reasons), True)
check("no decline reason is the success token", "ok" in reasons, False)

# ---- 4. THE PAGE MUST BE ABLE TO READ WHAT THE PROXY WRITES ---------------
#
# The visualizer draws its watermark lines and parameter panel from the
# proxy_start note. Two independent files, one undocumented handshake -- and
# the failure mode is silent: a note the page cannot parse yields a graph with
# no guide lines and no panel, which looks like "this run had no parameters"
# rather than "the page and the proxy disagree".
#
# So do not restate the page's regex here. LIFT IT OUT OF THE PAGE and run it
# against the note the proxy actually produces. If either side is edited
# without the other, this fails.

PAGE = HERE.parent / "tools" / "ferry-metrics.html"
check("visualizer exists where expected", PAGE.exists(), True)

if PAGE.exists():
    import re as _re
    html = PAGE.read_text(encoding="utf-8")

    # The k=v splitter, as written in the page.
    m_kv = _re.search(r"var mm = /\^(.+?)/\.exec\(part\);", html)
    check("the page's k=v regex is findable", bool(m_kv), True)

    if m_kv:
        # The decline tests above call reset(), which rebinds TRIGGER_TOKENS.
        # Set it again to something no default can coincide with, and compare
        # against the live constant rather than a literal typed up there --
        # a literal here would have to be kept in sync by hand, which is the
        # exact class of drift this section exists to catch.
        srv.TRIGGER_TOKENS = 163841
        js = m_kv.group(1)
        # JS -> Python: the page escapes \s as \\s inside an HTML-embedded
        # regex literal; nothing else in this pattern differs between the
        # two flavours.
        py = js.replace("\\\\", "\\")
        rx = _re.compile("^" + py)
        got = {}
        for part in srv._params_note().split(","):
            mm = rx.match(part)
            if mm:
                got[mm.group(1)] = mm.group(2)
        for key in list(KNOBS) + ["mode", "min_keep", "port"]:
            check(f"page regex reads {key} from the real note",
                  key in got, True)
        check("page regex reads the LIVE trigger, not a default",
              got.get("trigger"), str(srv.TRIGGER_TOKENS))

    # The trigger/target sniffers that place the watermark lines.
    for name in ("trigger", "target"):
        m_g = _re.search(name + r"\[\\s=:\]\+\(\[0-9\]\+\)", html)
        check(f"page still sniffs {name} for its guide line", bool(m_g), True)

    # gate must be bucketed, drawn, and given a colour in EVERY theme --
    # a colour defined once renders invisible in the other two palettes.
    check("page buckets the gate event",
          'r.event === "gate"' in html, True)
    check("hold colour is defined in all three palettes",
          html.count("--s-hold:"), 3)

# ---- report ---------------------------------------------------------------
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
