#!/usr/bin/env python3
"""Regression: `seq` makes a LOST ROW visible. Zara's proposal, 2026-09-09.

THE LIMIT IT CLOSES, and Messenger found it one abstraction below everything
else we had been chasing:

    A reader cannot distinguish "no event" from "an event silently lost
    before it was logged."

A dropped write leaves NO ROW -- indistinguishable from nothing having
happened. So a page infers "I have the whole record" from "I see no holes",
and **a silent loss is not a hole, it is the ABSENCE of a hole.** Every
failure class of that fortnight was defined by leaving no gap: the deaf
channel emitted nothing to plot, the stripped bytes logged as fine, the
mangled glyph rendered as a message.

`seq` asserts COMPLETENESS. The liveness pulse asserts LIVENESS. They are
different claims: a pulse makes silence BETWEEN pulses meaningful, but a
single lost row between two pulses stays invisible to it.

TWO DESIGN DECISIONS THIS FILE EXISTS TO PIN DOWN:

1. **APPENDED, NEVER INSERTED.** Zara proposed position 1 (ts_iso,seq,event).
   That shifts every positional reader by one -- ferry-watch's
   `$2=="request"` would silently begin matching the sequence number, and
   nothing would report it. Six live readers split on commas. Appending costs
   adjacency to ts_iso; inserting costs correctness everywhere.

2. **IT MUST SURVIVE A RESTART.** A counter reset to 0 makes every
   post-restart row look like a loss -- the instrument manufacturing the
   exact defect it exists to detect. Seeded from the file at boot, the same
   way the cycle counter already was.
"""
import csv, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import metrics

PASSED = 0
FAILED = []
def check(label, actual, expected):
    global PASSED
    if actual == expected: PASSED += 1
    else: FAILED.append(f"{label}: got {actual!r}, expected {expected!r}")

def fresh():
    d = tempfile.mkdtemp()
    return os.path.join(d, "metrics.csv")

# ---- 1. the column exists, and is APPENDED ------------------------------
check("seq is in the contract", "seq" in metrics.HEADER, True)
check("seq is LAST (appended, not inserted)", metrics.HEADER[-1], "seq")
check("ts_iso still index 0", metrics.HEADER[0], "ts_iso")
check("event still index 1 — positional readers unbroken", metrics.HEADER[1], "event")
check("cycle still index 2", metrics.HEADER[2], "cycle")

# ---- 2. monotonic and gap-free ------------------------------------------
p = fresh()
w = metrics.MetricsWriter(p)
for i in range(5):
    w.row("request", context_tokens=1000 + i)
rows = list(csv.reader(open(p)))[1:]
seqs = [int(r[12]) for r in rows]
check("seq numbers every row", len(seqs), 5)
check("seq starts at 1", seqs[0], 1)
check("seq is gap-free and monotonic", seqs, [1, 2, 3, 4, 5])

# ---- 3. IT SURVIVES A RESTART. The bug that would fake a loss. ----------
w2 = metrics.MetricsWriter(p)      # new writer, same file = proxy restart
w2.row("request", context_tokens=9999)
rows = list(csv.reader(open(p)))[1:]
check("seq CONTINUES across a restart (does not reset to 1)",
      int(rows[-1][12]), 6)

# ---- 4. every event type is numbered, not just requests ----------------
p2 = fresh()
w3 = metrics.MetricsWriter(p2)
w3.row("proxy_start"); w3.row("alive"); w3.row("curation", cycle=1)
w3.row("archive_write", cycle=1); w3.row("error", note="x")
rows = list(csv.reader(open(p2)))[1:]
check("all event types carry seq", [int(r[12]) for r in rows], [1, 2, 3, 4, 5])

# ---- 5. A LOST ROW IS DETECTABLE — the whole point ----------------------
# Simulate the failure: delete a row from the middle, as a dropped write
# would. Before seq this was invisible; now a reader can name it.
lines = open(p2).read().splitlines()
del lines[3]                                    # drop one data row
open(p2, "w").write("\n".join(lines) + "\n")
got = [int(r[12]) for r in list(csv.reader(open(p2)))[1:]]
gaps = [b - a for a, b in zip(got, got[1:]) if b - a != 1]
check("a dropped row leaves a DETECTABLE gap", len(gaps), 1)
check("and the gap is exactly one row wide", gaps[0], 2)

# ---- 6. old rows (no seq) read as BLANK, not zero ----------------------
# Mixed files are guaranteed: every existing archive predates this column.
p3 = fresh()
open(p3, "w").write(metrics.HEADER_LINE + "\n"
                    + "2026-09-01T00:00:00Z,request,,100,,,,,,haiku,,\n")
w4 = metrics.MetricsWriter(p3)
w4.row("request", context_tokens=200)
rows = list(csv.reader(open(p3)))[1:]
check("legacy row has no seq field at all", len(rows[0]) < 13, True)
check("new row after legacy rows still numbers from 1",
      int(rows[1][12]), 1)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED: print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
