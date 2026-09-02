#!/usr/bin/env python3
"""Regression: the log must not bury its own emergency.

THE INCIDENT (2026-09-02). ferry's debug log held 425,249 WARNING lines.
425,141 of them were `[MATCH] No match` -- and 108 were real. Among the 108:

    *** NOT CONVERGING -- CURATION DISABLED ***
    [FERRY] 156,514 over trigger but curation is DISABLED
    [FERRY] curation landed at 152,597, target 100,000 -- strike 3/3

The lockout is the loudest event Ferry can produce. I wrote exactly that into
the UI spec for Zara -- "the only state that is genuinely an emergency" -- and
in the log it sat at one part in 3,937. Signal was 0.025% of the channel.

WHY THE NOISE WAS NEVER A BUG IN MATCHING. find_match scans EVERY stored
compression and keeps the one reaching furthest into the request. At most one
can match; the rest are older compressions whose hash chains were themselves
already replaced by pointers, so they CANNOT appear. "No match" is the
expected result for every non-current entry -- roughly 82 per request here.
Nothing was broken. The level was wrong.

That distinction is the point. A 0%-success subsystem and a normal negative
logged too loudly look identical from the outside, and I started writing up
the first one before reading the code.

THE RULE THIS ENCODES: a per-item negative that occurs on the expected path is
DEBUG. WARNING is reserved for what a human must act on. A monitor you learn
to skim is already broken, whether or not its output is correct.

Run: python3 tests/test_match_log_level.py
Crossing-2d23. Stdlib only.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_HOME", "/tmp/.matchlog-test-home")
# Keep the suite OUT of the production debug log -- importing server.py
# installs a FileHandler, and this suite exists BECAUSE that log got polluted.
os.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-test-debug.log")

spec = importlib.util.spec_from_file_location(
    "srv", HERE.parent / "proxy" / "server.py")
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


class Capture(logging.Handler):
    """Collect records at every level so we can assert on the LEVEL, not the text."""
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at(self, level):
        return [r for r in self.records if r.levelno == level]

    def atleast(self, level):
        return [r for r in self.records if r.levelno >= level]


def msg(role, text):
    return {"role": role, "content": text}


def build_store(n_stale, with_current=True):
    """A store shaped like production: many stale chains, at most one live."""
    store = srv.CompressionStore()
    for i in range(n_stale):
        e = store.add()
        # A chain that cannot appear in the request -- exactly the production
        # case, where these turns were themselves already replaced by pointers.
        e["original_hashes"] = srv._hash_messages(
            [msg("user", f"stale-{i}-a"), msg("assistant", f"stale-{i}-b")])
        e["prefix"] = [msg("user", f"pointer-{i}")]
    if with_current:
        e = store.add()
        e["original_hashes"] = srv._hash_messages(CURRENT_PREFIX)
        e["prefix"] = [msg("user", "pointer-current")]
    return store


CURRENT_PREFIX = [msg("user", "live-1"), msg("assistant", "live-2"),
                  msg("user", "live-3")]
REQUEST = CURRENT_PREFIX + [msg("assistant", "live-4"), msg("user", "live-5")]
REQ_HASHES = srv._hash_messages(REQUEST)


# ---- 1. behaviour is unchanged: the live chain still matches ---------------
store = build_store(n_stale=8)
match, end = store.find_match(REQ_HASHES, REQUEST)
check("the live compression is still found", match is not None, True)
check("it is the live one, not a stale one",
      match["prefix"][0]["content"], "pointer-current")
check("match ends where the live chain ends", end, len(CURRENT_PREFIX))

# ---- 2. the expected negatives must not shout -----------------------------
# THE DERIVE RULE: assert on the LEVEL of what the production path emits,
# never on a count I typed in. n_stale is varied so the assertion cannot
# accidentally encode today's fleet size.
for n_stale in (0, 1, 8, 82, 300):
    cap = Capture()
    srv.log.addHandler(cap)
    prev = srv.log.level
    srv.log.setLevel(logging.DEBUG)
    try:
        build_store(n_stale=n_stale).find_match(REQ_HASHES, REQUEST)
    finally:
        srv.log.removeHandler(cap)
        srv.log.setLevel(prev)
    check(f"{n_stale} stale chains produce ZERO warnings",
          len(cap.atleast(logging.WARNING)), 0)
    # Information is demoted, not destroyed -- a real matching failure must
    # still be diagnosable by turning the level down.
    if n_stale:
        check(f"{n_stale} stale chains still leave a DEBUG trail",
              len(cap.at(logging.DEBUG)) >= n_stale, True)

# ---- 3. the emergencies must STILL be warnings ----------------------------
# Cross-file contract check, same shape as test_gate_visibility: read the
# production source and assert the loud things are still loud. This is the
# half that makes the demotion safe -- otherwise "quieter" and "silent" pass
# the same test.
#
# The phrases are the ones that ACTUALLY appear in the emitted text (verified
# against ferry's real log), and the search spans all of proxy/ rather than
# naming a file -- my first draft asserted against server.py and three of the
# four live elsewhere or are split across f-string lines. A contract test that
# encodes where I think the code is tests my memory, not the code.
EMERGENCIES = ("NOT CONVERGING", "over trigger but curation is",
               "-- strike {", "TARGET UNREACHABLE")
# NOTE on "-- strike {": the bare "-- strike " ALSO matches a log.info at
# server.py:339 ("strike count reset") and the first draft of this test failed
# on it. The ambiguity was in my phrase, not in the code. Left as a comment
# because it is the same species as everything else in this file: a search
# that returns a plausible wrong hit and reads as a finding.
SOURCES = sorted((HERE.parent / "proxy").glob("*.py"))
check("there are proxy sources to scan", len(SOURCES) > 0, True)

for phrase in EMERGENCIES:
    found_at = None
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        i = text.find(phrase)
        if i > 0:
            found_at = (path.name, text, i)
            break
    check(f"{phrase!r} still exists somewhere in proxy/", found_at is not None, True)
    if found_at is None:
        continue
    name, text, idx = found_at
    # Walk back to the logging call that carries it. NOTE: an unguarded
    # lvl[0] here raised TypeError on the first run -- a CRASH, not a failed
    # assertion, which is the failure mode that let two mutants survive on
    # 2026-08-30. A test that dies is a test that reported nothing. Every
    # branch below must end in a check().
    head = text[max(0, idx - 600):idx]
    lvl, best_at = None, -1
    for cand in ("log.warning", "log.error", "log.critical",
                 "log.info", "log.debug"):
        at = head.rfind(cand)
        if at > best_at:
            lvl, best_at = cand, at
    check(f"{phrase!r} ({name}) has an identifiable log call", lvl is not None, True)
    check(f"{phrase!r} ({name}) is emitted at warning or louder",
          lvl in ("log.warning", "log.error", "log.critical"), True)

# ---- 4. the property that actually failed in production -------------------
# 82 stale entries per request is the real fleet shape. If a future change
# reintroduces a per-entry warning, this is the assertion that catches it
# before another 425,141 lines accumulate.
cap = Capture()
srv.log.addHandler(cap)
prev = srv.log.level
srv.log.setLevel(logging.DEBUG)
try:
    for _ in range(50):                      # 50 requests, fleet-shaped
        build_store(n_stale=82).find_match(REQ_HASHES, REQUEST)
finally:
    srv.log.removeHandler(cap)
    srv.log.setLevel(prev)
check("50 fleet-shaped requests emit 0 warnings (was ~4,100)",
      len(cap.atleast(logging.WARNING)), 0)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
