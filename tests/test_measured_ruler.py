#!/usr/bin/env python3
"""F2: the keep budget is sized on a MEASURED ruler, not chars/4.

Zara-c207, first production curation (2026-11-26): chars/4 called 464k of an
816k request "unevictable"; the whole context after curation was 391k. Here a
request is built with a KNOWN chars-per-token (2.7) and the split must recover
it, while the old chars/4 path demonstrably gets it wrong on the same input.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_LOG", os.devnull)
import compressor as comp  # noqa: E402

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


CPT = 2.7                                   # Zara's measured chars per token
msg_chars, sys_chars = 1_409_284, 162_000   # her message chars; a plausible system+tools
payload = {"system": "s" * sys_chars, "tools": []}
real = int((msg_chars + sys_chars) / CPT)   # what the API would report
true_scaffold = int(sys_chars / CPT)

msg_t, unev, src = comp.measured_split(real, msg_chars, payload)
check("measured split recovers the scaffolding within 1%",
      abs(unev - true_scaffold) <= 0.01 * true_scaffold, (unev, true_scaffold))
check("...and the message tokens within 1%",
      abs(msg_t - int(msg_chars / CPT)) <= 0.01 * msg_chars / CPT, msg_t)
check("source says MEASURED and names the ratio", src.startswith("measured") and "2.70" in src, src)

old = comp.effective_unevictable(real, msg_chars)
check("control: the old chars/4 path overstates scaffolding by >100k on the same request",
      old - true_scaffold > 100_000, (old, true_scaffold))

_, unev_f, src_f = comp.measured_split(real, msg_chars, payload, observed_floor=30_000)
check("a lower observed floor (a measurement) still caps it", unev_f == 30_000 and "smallest" in src_f, (unev_f, src_f))
_, unev_j, _ = comp.measured_split(real, msg_chars, payload, observed_floor=0)
check("a junk floor (0) is ignored, not trusted", unev_j == unev, unev_j)

m2, u2, s2 = comp.measured_split(real, msg_chars, None)
check("no payload -> falls back to chars/4, and SAYS it is an estimate",
      s2.startswith("ESTIMATED") and u2 == comp.effective_unevictable(real, msg_chars), s2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
