#!/usr/bin/env python3
"""Synthesise a plausible 3-cycle Ferry metrics.csv, to the fixed contract.

Deliberately includes the ugly cases the visualizer must survive:
  - blank numeric fields (unmeasured, not zero)
  - a note containing a comma (quoted) and a note containing a quote
  - a negative tokens_in (the request right after a curation)
  - an error row and a restart row
  - a TORN final line (a half-written append)
"""
import csv
import io
import os
import random
import sys
from datetime import datetime, timedelta, timezone

HEADER = "ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,archive_lines,archive_bytes,carry_chars,model,window,note".split(",")

out_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferry-viz-test/data"
os.makedirs(out_dir, exist_ok=True)
path = os.path.join(out_dir, "metrics.csv")

random.seed(7)
buf = io.StringIO()
w = csv.writer(buf, lineterminator="\n")
w.writerow(HEADER)

t = datetime(2026, 8, 4, 9, 14, 3, tzinfo=timezone.utc)
MODEL = "claude-opus-4-6-20260514"
WINDOW = 200000
TRIGGER = 120000
TARGET = 60000


def row(event, **kw):
    r = {k: "" for k in HEADER}
    r["ts_iso"] = t.isoformat().replace("+00:00", "Z")
    r["event"] = event
    r.update(kw)
    w.writerow([r[k] for k in HEADER])


def bump(sec):
    global t
    t += timedelta(seconds=sec)


row("proxy_start", note="mode=ferry trigger=%d target=%d" % (TRIGGER, TARGET))
bump(4)

ctx = 0
prev = None
cycle = 0
alines = 0
abytes = 0
carry = 0

# a short warm-up where the proxy has not yet seen a real token count
row("request", context_tokens=11480, model=MODEL, window=WINDOW)
ctx = 11480
prev = ctx
bump(38)
# one request where the SSE never carried message_start: context unknown, blank
row("request", model=MODEL, window=WINDOW, note="no message_start in stream")
bump(22)

for c in range(1, 4):
    # climb to the trigger with real tool traffic
    while ctx <= TRIGGER:
        step = random.randint(4200, 19000)
        ctx += step
        row("request", context_tokens=ctx, tokens_in=(ctx - prev) if prev is not None else "",
            carry_chars=carry if carry else "", model=MODEL, window=WINDOW)
        prev = ctx
        bump(random.randint(24, 74))

    cycle = c
    evicted = ctx - TARGET + random.randint(-1800, 1800)
    bump(1)
    row("curation", cycle=cycle, tokens_evicted=evicted, model=MODEL, window=WINDOW,
        note="estimate:chars/4")
    bump(1)
    turns = random.randint(4, 9)
    alines += turns
    abytes += evicted * 4 + random.randint(2000, 9000)
    row("archive_write", cycle=cycle, archive_lines=alines, archive_bytes=abytes,
        note='archived %d turns -> archive_20260804.jsonl#L%d-L%d, blobs: %d'
             % (turns, alines - turns + 1, alines, random.randint(0, 3)))
    bump(1)
    carry += random.randint(900, 2400)

    # the next real request comes back down: tokens_in is NEGATIVE here
    ctx = TARGET + random.randint(400, 3000)
    row("request", context_tokens=ctx, tokens_in=ctx - prev, carry_chars=carry,
        model=MODEL, window=WINDOW)
    prev = ctx
    bump(31)

    if c == 1:
        row("fetch", note="archive_20260804.jsonl#L1-L6")
        bump(9)
    if c == 2:
        row("error", note='metrics flush failed: [Errno 28] No space left on device, retrying')
        bump(3)
        row("restart", context_tokens=ctx, carry_chars=carry, model=MODEL, window=WINDOW,
            note="state restored from disk: 2 cycles, 11 archived lines")
        bump(6)

# a couple of trailing requests so the last segment is a partial climb
for _ in range(3):
    ctx += random.randint(5000, 16000)
    row("request", context_tokens=ctx, tokens_in=ctx - prev, carry_chars=carry,
        model=MODEL, window=WINDOW)
    prev = ctx
    bump(41)

text = buf.getvalue()
# ...and a TORN final line: the collector was mid-append when we read.
text += "2026-08-04T10:41:5"

with open(path, "w", encoding="utf-8", newline="") as fh:
    fh.write(text)

print(path)
print("%d bytes, %d newline-terminated lines + 1 torn" % (len(text), text.count("\n")))
