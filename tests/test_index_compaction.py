#!/usr/bin/env python3
"""Regression: the pointer index must not grow without bound (DESIGN-REV2 §22).

Measured on a live passenger 2026-08-26: the index is cumulative and
unevictable, growing ~29 tokens per evicted turn, forever. At ~100 turns per
cycle that is ~2,900 tokens of PERMANENT floor added every cycle -- so Ferry's
own floor rose every time Ferry did its job, and would eventually pass target,
then trigger, and thrash BY DESIGN.

    cycle 49    2 turns evicted    index  1,414 tok
    cycle 50  114 turns evicted    index  4,658 tok
    cycle 51  104 turns evicted    index  7,303 tok

THE INVARIANT THAT MATTERS MOST HERE: compaction may collapse how an address is
WRITTEN; it may never lose an address. An archived turn whose index line is gone
is unreachable forever -- the archive is intact and nobody holds the key. That
is §19.5's "green light on a dead wire, inside the memory system", and it is
strictly worse than never having archived the turn at all.

Run: python3 tests/test_index_compaction.py
Crossing-2d23. Stdlib only.
"""
import importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "proxy"))
spec = importlib.util.spec_from_file_location("cur", HERE.parent / "proxy" / "curation.py")
cur = importlib.util.module_from_spec(spec); sys.modules["cur"] = cur
spec.loader.exec_module(cur)

PASSED = 0; FAILED = []
def check(name, got, want):
    global PASSED
    if got == want: PASSED += 1
    else: FAILED.append(f"{name}:\n      expected {want!r}\n      got      {want.__class__ is got.__class__ and got or got!r}")

F = "archive_20260826.jsonl"
def slug(n, role="user", gist="did a thing"):
    return f"{cur.SLUG_PREFIX}{role}] {gist} → {F}#L{n}"

def covered(lines):
    """Every archive line number reachable from an index, as a set."""
    s = set()
    for ln in lines:
        p = cur._parse_slug(ln)
        if p:
            _, a, b = p
            s.update(range(a, b + 1))
    return s

# ---- THE LOAD-BEARING PROPERTY: no address is ever lost -------------------

for n in (1, 5, 50, 201, 500):
    src = [slug(i) for i in range(1, n + 1)]
    out = cur.compact_index(src, keep_detail=10)
    check(f"{n} slugs: every line number still reachable after compaction",
          covered(out), covered(src))

# ---- it actually compacts -------------------------------------------------

src = [slug(i) for i in range(1, 501)]
out = cur.compact_index(src, keep_detail=10)
check("500 contiguous slugs collapse to 1 range + 10 detailed", len(out), 11)
check("the collapsed line names the full span",
      cur._parse_slug(out[0]), (F, 1, 490))
check("the collapsed line says how many turns it stands for",
      "490 turns" in out[0], True)

# ---- under the detail budget, nothing changes -----------------------------

src = [slug(i) for i in range(1, 8)]
check("fewer slugs than the budget are returned untouched",
      cur.compact_index(src, keep_detail=10), src)

# ---- gaps must NOT be merged (a gap means turns nobody indexed) -----------

src = [slug(1), slug(2), slug(9), slug(10)]
out = cur.compact_index(src, keep_detail=0)
check("a discontinuity produces TWO ranges, never one spanning the gap",
      len(out), 2)
check("the gap is preserved exactly — no phantom coverage",
      covered(out), {1, 2, 9, 10})

# ---- different archive files must not be merged --------------------------

other = "archive_20260825.jsonl"
src = [slug(1), slug(2),
       f"{cur.SLUG_PREFIX}user] x → {other}#L3",
       f"{cur.SLUG_PREFIX}user] x → {other}#L4"]
check("two different archive files produce two ranges",
      len(cur.compact_index(src, keep_detail=0)), 2)

# ---- IDEMPOTENT: compacting a compacted index merges, never nests ---------

src = [slug(i) for i in range(1, 301)]
once = cur.compact_index(src, keep_detail=0)
twice = cur.compact_index(once, keep_detail=0)
check("compaction is idempotent", twice, once)
check("re-compaction preserves coverage", covered(twice), covered(src))

# a second generation of slugs appended after a collapsed range must merge
appended = once + [slug(i) for i in range(301, 351)]
merged = cur.compact_index(appended, keep_detail=0)
check("a collapsed range MERGES with newer adjacent slugs, not stacks",
      len(merged), 1)
check("the merged range covers everything", covered(merged), covered(appended))

# ---- an unparseable line is passed through, NEVER dropped ----------------

junk = f"{cur.SLUG_PREFIX}user] a line with no pointer at all"
src = [slug(1), junk, slug(2)]
out = cur.compact_index(src, keep_detail=0)
check("an unparseable index line survives compaction verbatim",
      junk in out, True)
check("and it does not silently join two spans into one",
      covered(out), {1, 2})

# ---- a malformed reversed range is refused, not guessed ------------------

check("last < first is not a pointer",
      cur._parse_slug(f"{cur.SLUG_PREFIX}user] x → {F}#L50-L10"), None)

# ---- REAL SHAPE: the actual range the archive_write row records ----------

real = f"{cur.SLUG_PREFIX}archived] 114 turns → {F}#L564-L677"
check("the range shape Ferry actually writes parses back",
      cur._parse_slug(real), (F, 564, 677))

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED: print(f"  FAIL {f}")
sys.exit(1 if FAILED else 0)
