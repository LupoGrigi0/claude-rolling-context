#!/usr/bin/env python3
"""Regression: ferry-watch must NOT report an interleaved row as context.

THE BUG (found 2026-09-05, by reading a heartbeat instead of skimming it).
ferry-watch reported `ferry ctx=135602`. That number was a SUBAGENT SIDE-QUERY.
ferry's last real main-thread request was 10:39:00Z at 139,421 -- the watcher
was off by 3,819 tokens and had been for as long as the expression existed.

WHY IT HAPPENS. Claude Code gives a subagent its PARENT's session id and fires
small internal side-queries on it, so ~10k-token requests appear interleaved
among ~139k ones. The proxy marks them `interleaved` in `note`. They are REAL
TRAFFIC and must be visible -- but they are a different conversation, and they
must never enter the context curve. Drawn naively they produce a cliff and an
equal wall, neither of which happened.

WHERE THE RULE CAME FROM. I wrote it myself, in section 4 of the Ferry UI spec
for Zara, as a rule she must not break -- and then broke it in my own watcher,
and read the wrong number off it every two hours for days without noticing.
Adjacency did not save me because I was the author: proximity makes a catch
cheap for a SECOND reader and does nothing for the first.

MEASURED, not estimated: 824 of 11,703 of ferry's request rows are interleaved
(7.0%). This was never a rare edge case.

THE TEST READS THE REAL SCRIPT. It extracts the awk expression from
tools/testbed/ferry-watch.sh and runs it, rather than embedding a copy of the
logic -- a test carrying its own copy can pass while the script it guards is
wrong. That failure mode has already cost us once this fortnight.
"""
import os, re, subprocess, sys, tempfile

PASSED = 0
FAILED = []

def check(label, actual, expected):
    global PASSED
    if actual == expected:
        PASSED += 1
    else:
        FAILED.append(f"{label}: got {actual!r}, expected {expected!r}")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "tools", "testbed", "ferry-watch.sh")

check("ferry-watch.sh exists", os.path.isfile(SCRIPT), True)
src = open(SCRIPT, encoding="utf-8").read()

# ---- 1. the buggy expression must be gone from EVERY call site -----------
# Two sites existed: the per-instance check (~line 58) and the quiet-report
# summary (~line 286). Fixing one and leaving the other is exactly the
# "fix the case, leave the class" failure this file is about.
naive = re.findall(r'\$2=="request"\s*\{c=\$4\}\s*END', src)
check("no naive $2==\"request\"{c=$4} remains (both call sites fixed)",
      len(naive), 0)

guarded = re.findall(r'\$2=="request"[^\n]*interleaved[^\n]*c=\$4', src)
check("every context extraction guards against interleaved", len(guarded), 2)

# ---- 2. run the REAL expression from the script -------------------------
m = re.search(r"awk -F, '([^']*c=\$4[^']*)'", src)
check("extracted an awk program from the script", bool(m), True)
prog = m.group(1) if m else None
if prog is None:
    FAILED.append("could not extract the awk program; refusing to test a guess")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for f in FAILED: print(f"  FAIL  {f}")
    sys.exit(1)

HDR = ("ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,"
       "archive_lines,archive_bytes,carry_chars,model,window,note\n")

def ctx(rows):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(HDR)
        f.writelines(r if r.endswith("\n") else r + "\n" for r in rows)
        path = f.name
    try:
        out = subprocess.run(["awk", "-F,", prog, path],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    finally:
        os.unlink(path)

R  = "2026-09-05T10:39:00Z,request,,139421,239,,,,,haiku,,injected"
IL = "2026-09-05T10:41:41Z,request,,135602,,,,,,haiku,,injected; interleaved"

# THE EXACT PRODUCTION FAILURE: main-thread turn, then trailing subagent rows.
check("THE ferry CASE: interleaved trailer does not become ctx",
      ctx([R, IL]), "139421")
check("a plain run reports the last main-thread request", ctx([R]), "139421")
check("several interleaved trailers still do not win",
      ctx([R, IL, IL, IL]), "139421")
check("interleaved BETWEEN main rows is skipped, later main row wins",
      ctx([R, IL, R.replace("139421", "141000")]), "141000")
check("non-request events never set ctx",
      ctx([R, "2026-09-05T10:42:00Z,curation,,,,50000,,,,haiku,,cycle"]),
      "139421")

# ---- 3. STALENESS IS A TIME PROPERTY, NOT A ROW-COUNT ONE ---------------
# Found in production twice in one morning.
#
# FIRST: after curation 151 every request was tagged interleaved, so "first
# non-interleaved" latched a row 2 hours old and 44,000 tokens wrong (149,854
# vs a true 105,476). The original bug was ~3,800. Staleness was 10x worse.
#
# SECOND: my fix for that counted ROWS ("fall back if 3+ interleaved follow"),
# and it broke the FROZEN case. ferry died mid-burst with 3 interleaved rows
# after its last main-thread one -- 2m41s apart, nothing stale at all. The
# row-count rule fell back and reported the interleaved value, reintroducing
# the exact bug this file exists to prevent, on a different instance.
#
# The two cases separate cleanly on TIME and not at all on row position:
#     ferry      last_ni 10:39:00  last_any 10:41:41   gap 2m41s  -> use ni
#     passenger  last_ni 09:00:12  last_any 13:00:15   gap 4h     -> use latest
#
# A filter can be correct about which rows are untrustworthy, wrong about
# whether any remain, and then wrong again about how to tell.

T0 = "2026-09-05T10:39:00Z"
def row(ts, ctx, il=False):
    return f"{ts},request,,{ctx},,,,,,haiku,,injected" + ("; interleaved" if il else "")

# FROZEN: interleaved trailers seconds later. Purity wins.
check("frozen instance: seconds-old trailers do NOT displace main-thread",
      ctx([row("2026-09-05T10:39:00Z", 139421),
           row("2026-09-05T10:41:27Z", 134802, True),
           row("2026-09-05T10:41:34Z", 135030, True),
           row("2026-09-05T10:41:41Z", 135602, True)]), "139421")

# LIVE + STALE: hours of interleaved traffic. Freshness wins.
check("live instance: hours-stale main-thread row IS displaced",
      ctx([row("2026-09-08T09:00:12Z", 149854),
           row("2026-09-08T09:30:05Z",  99002, True),
           row("2026-09-08T13:00:15Z", 111669, True)]), "111669")

check("no non-interleaved row at all -> latest, not 0",
      ctx([row("2026-09-08T09:30:05Z", 99002, True),
           row("2026-09-08T13:00:15Z", 111669, True)]), "111669")

check("a later main-thread row always wins outright",
      ctx([row("2026-09-08T09:00:12Z", 149854),
           row("2026-09-08T09:30:05Z",  99002, True),
           row("2026-09-08T13:00:15Z", 105000)]), "105000")

# ---- 4. KNOWN LIMITATION, asserted so it is visible not hidden ----------
# A file with ONLY interleaved traffic yields 0. That is blank-is-not-zero in
# another hat: "no main-thread reading" renders identically to "context is
# empty". Pre-existing, unchanged by this fix, and low severity (it needs a
# proxy that has never seen one ordinary request). Documented deliberately --
# an accepted limitation stated out loud beats one discovered later.
# superseded 2026-09-08: an all-interleaved file no longer reports 0, it
# reports the latest row. The old assertion documented a limitation that the
# staleness fix removed.
check("SHORT all-interleaved file reports the row, not 0",
      ctx([IL, IL]), "135602")

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
