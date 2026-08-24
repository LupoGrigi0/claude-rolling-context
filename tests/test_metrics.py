#!/usr/bin/env python3
"""Property tests for the Ferry metrics collector.

The metrics file is evidence: if it can lie (a blank rendered as 0, a row
torn in half by the curation thread, a note whose comma shifts every later
column) then the graph built from it lies too. These tests are written
against the metrics CONTRACT, not the implementation.

Offline, stdlib only, no framework. Run: python3 tests/test_metrics.py
"""

import csv
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))

import metrics  # noqa: E402
from metrics import (EVENTS, HEADER, HEADER_LINE, ArchiveTotals,  # noqa: E402
                     MetricsWriter, get_writer, metrics_enabled)

CONTRACT_HEADER = ("ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,"
                   "archive_lines,archive_bytes,carry_chars,model,window,note")

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{' — ' + str(detail) if detail else ''}")


def read_rows(path):
    """Parse the file the way the visualizer will: stdlib csv, header first."""
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


tmp = Path(tempfile.mkdtemp(prefix="ferry-metrics-"))

print("ferry metrics collector property tests\n")

# ── the header IS the contract ──────────────────────────────────────────
print("header:")
p1 = tmp / "h" / "metrics.csv"
w1 = MetricsWriter(p1)
check("constructing the writer creates the file", p1.exists())
check("header line matches the contract byte for byte",
      p1.read_text(encoding="utf-8").splitlines()[0] == CONTRACT_HEADER,
      p1.read_text(encoding="utf-8").splitlines()[:1])
check("HEADER_LINE constant agrees with the contract",
      HEADER_LINE == CONTRACT_HEADER, HEADER_LINE)
check("the contract events are the ones the module knows",
      set(EVENTS) == {"proxy_start", "request", "probe", "curation",
                      "archive_write", "fetch", "restart", "error"}, EVENTS)
check("'request' and 'probe' are distinct events (a turn is not a probe)",
      "request" in EVENTS and "probe" in EVENTS)

w1.row("proxy_start", note="mode=ferry")
w2 = MetricsWriter(p1)          # same path again (a restart)
w2.row("request", context_tokens=1000)
rows = read_rows(p1)
check("header written exactly once across two writers",
      sum(1 for r in rows if r and r[0] == "ts_iso") == 1, rows)
check("every row has all 12 columns",
      all(len(r) == len(HEADER) for r in rows), [len(r) for r in rows])
check("rows are appended in order (append-only)",
      [r[1] for r in rows[1:]] == ["proxy_start", "request"],
      [r[1] for r in rows[1:]])

# ── blank is not zero ───────────────────────────────────────────────────
print("\nblank-not-zero:")
p2 = tmp / "blank" / "metrics.csv"
wb = MetricsWriter(p2)
wb.row("request", context_tokens=51234, model="claude-opus-4-5")
r = read_rows(p2)[1]
col = dict(zip(HEADER, r))
check("measured field carries its value", col["context_tokens"] == "51234", col)
check("unmeasured numeric fields are EMPTY, never 0",
      col["cycle"] == "" and col["tokens_evicted"] == ""
      and col["archive_lines"] == "" and col["archive_bytes"] == ""
      and col["carry_chars"] == "" and col["window"] == "", col)
check("explicit None is blank too",
      dict(zip(HEADER, (wb.row("request", context_tokens=None,
                               carry_chars=None) or read_rows(p2)[2])))
      ["context_tokens"] == "")
wb.row("curation", cycle=1, tokens_evicted=0)
z = dict(zip(HEADER, read_rows(p2)[3]))
check("a REAL zero is still written as 0 (blank and zero stay distinct)",
      z["tokens_evicted"] == "0" and z["carry_chars"] == "", z)
check("ts_iso is stamped on every row and ends in Z",
      all(row[0].endswith("Z") and len(row[0]) == 20 for row in read_rows(p2)[1:]),
      [row[0] for row in read_rows(p2)[1:]])

# ── tokens_in = delta vs the previous request ───────────────────────────
print("\ntokens_in delta:")
p3 = tmp / "delta" / "metrics.csv"
wd = MetricsWriter(p3)
# Ordered the way a curation cliff ACTUALLY happens: the cycle advances, THEN
# the next request carries the smaller context. The previous version of this
# fixture put the curation row BEFORE the growth and asserted a drop with no
# curation between the two requests — a sequence that never occurs live. It
# was invented, not observed, and it disagreed with the real Phase D data.
wd.row("request", context_tokens=10000)
wd.row("request", context_tokens=14200)
wd.next_cycle()                                   # a curation cycle fires
wd.row("curation", cycle=1, tokens_evicted=500)   # must not disturb the delta
wd.row("request", context_tokens=9000)            # after a curation: negative
d = [dict(zip(HEADER, row)) for row in read_rows(p3)[1:]]
check("first request has BLANK tokens_in (no previous — not 0)",
      d[0]["tokens_in"] == "", d[0])
check("second request records the growth", d[1]["tokens_in"] == "4200", d[1])
check("a drop is recorded as a negative delta (the curation cliff)",
      d[3]["tokens_in"] == "-5200", d[3])
check("non-request rows never carry a synthetic delta", d[2]["tokens_in"] == "")

# ── the phantom sawtooth (observed in the real Phase D run, 2026-08-23) ──
# Claude Code hands a subagent its PARENT'S session id, and fires its own
# small side-queries on that same id. The live graph drew a 20,000-token
# cliff and an equal wall, neither of which happened. Lupo found it by
# reading the graph and asking the obvious question.
print("\nphantom sawtooth:")
pph = tmp / "phantom" / "metrics.csv"
wp = MetricsWriter(pph)
S = "shared-session-id"
wp.row("request", session=S, context_tokens=21020)   # parent turn
wp.row("request", session=S, context_tokens=514)     # <- side-query, NOT a turn
wp.row("request", session=S, context_tokens=21469)   # parent's next real turn
ph = [dict(zip(HEADER, row)) for row in read_rows(pph)[1:]]
check("an unexplained drop (no curation between) is BLANK, never a fake cliff",
      ph[1]["tokens_in"] == "", ph[1])
check("the interleaved row does NOT poison the chain: the parent's next turn "
      "reports its REAL growth (449), not a fabricated +20955 wall",
      ph[2]["tokens_in"] == "449", ph[2])
check("no fabricated 20k spike appears anywhere in the file",
      all(abs(int(r["tokens_in"])) < 20000
          for r in ph if r["tokens_in"] not in ("", None)))

# and the mirror case: an EXPLAINED drop of the same shape must survive
pex = tmp / "explained" / "metrics.csv"
we = MetricsWriter(pex)
we.row("request", session=S, context_tokens=31270)
we.next_cycle()
we.row("curation", session=S, cycle=1, tokens_evicted=6000)
we.row("request", session=S, context_tokens=24533)
ex = [dict(zip(HEADER, row)) for row in read_rows(pex)[1:]]
check("a drop EXPLAINED by a curation is still recorded (-6737), so the fix "
      "suppresses phantoms without flattening the real sawtooth",
      ex[2]["tokens_in"] == "-6737", ex[2])
wd2 = MetricsWriter(p3)
wd2.row("request", context_tokens=12345)
check("first request after a restart is blank again (delta truly unknown)",
      dict(zip(HEADER, read_rows(p3)[5]))["tokens_in"] == "",
      read_rows(p3)[5])

# ── the delta chain is PER SESSION ──────────────────────────────────────
# One proxy carries a conversation plus every subagent it spawns. On a single
# global chain, two interleaved sessions produce equal-and-opposite spikes
# that are pure fiction: A grows to 100k, B's first turn is 5k, and the file
# claims the context fell 95k and then rose 95k again.
print("\nper-session delta:")
p3s = tmp / "delta-session" / "metrics.csv"
ws = MetricsWriter(p3s)
ws.row("request", session="sess-A", context_tokens=100000)
ws.row("request", session="sess-B", context_tokens=5000)     # a subagent
ws.row("request", session="sess-A", context_tokens=104000)
ws.row("request", session="sess-B", context_tokens=6000)
s = [dict(zip(HEADER, row)) for row in read_rows(p3s)[1:]]
check("a second session's first turn is BLANK, not a -95000 cliff",
      s[1]["tokens_in"] == "", s[1])
check("session A's delta ignores session B entirely",
      s[2]["tokens_in"] == "4000", s[2])
check("session B's delta ignores session A entirely",
      s[3]["tokens_in"] == "1000", s[3])
check("no session's row was fabricated as 0",
      all(r["tokens_in"] != "0" for r in s), s)
check("session id is NOT written into the CSV (it is not a column)",
      all("sess-" not in ",".join(row) for row in read_rows(p3s)), read_rows(p3s))
ws.row("probe", note="non-turn POST /v1/messages/count_tokens")
ws.row("request", session="sess-A", context_tokens=104500)
s2 = [dict(zip(HEADER, row)) for row in read_rows(p3s)[1:]]
check("a probe row carries no context and no delta",
      s2[4]["context_tokens"] == "" and s2[4]["tokens_in"] == "", s2[4])
check("a probe never enters the delta chain (A's next delta is still real)",
      s2[5]["tokens_in"] == "500", s2[5])
for i in range(70):                                  # more than _MAX_SESSIONS
    ws.row("request", session=f"flood-{i}", context_tokens=1000 + i)
check("the per-session map is bounded (no unbounded growth in a long proxy)",
      len(ws._session_context) <= metrics._MAX_SESSIONS,
      len(ws._session_context))

# ── escaping: a note must not shift the columns ─────────────────────────
print("\nCSV escaping:")
p4 = tmp / "esc" / "metrics.csv"
we = MetricsWriter(p4)
NASTY = ('mode=ferry, trigger=100,000 — he said "moved, not lost"\n'
         'second line, with a comma')
we.row("proxy_start", note=NASTY)
we.row("error", note='upstream: {"error": "429, rate_limit"}')
esc = read_rows(p4)
check("commas, quotes and newlines round-trip exactly",
      esc[1][HEADER.index("note")] == NASTY, repr(esc[1][-1]))
check("a nasty note does not shift any column",
      len(esc[1]) == len(HEADER) and esc[1][1] == "proxy_start", esc[1])
check("a JSON-ish note round-trips too",
      esc[2][HEADER.index("note")] == 'upstream: {"error": "429, rate_limit"}',
      esc[2])
check("row count is right despite the embedded newline", len(esc) == 3, esc)

# ── concurrency: the proxy curates while it serves ──────────────────────
print("\nconcurrent writers:")
p5 = tmp / "conc" / "metrics.csv"
wc = MetricsWriter(p5)
N = 250


def hammer(tag, event):
    for i in range(N):
        wc.row(event, cycle=i, context_tokens=i * 7,
               note=f"{tag}, row {i}, \"quoted\"")


t1 = threading.Thread(target=hammer, args=("request-thread", "request"))
t2 = threading.Thread(target=hammer, args=("curation-thread", "curation"))
t1.start(); t2.start(); t1.join(); t2.join()
conc = read_rows(p5)
check("every row from both threads landed",
      len(conc) == 2 * N + 1, len(conc))
check("no row is torn or interleaved (all 12 columns, everywhere)",
      all(len(row) == len(HEADER) for row in conc),
      [len(row) for row in conc if len(row) != len(HEADER)][:5])
bad = [row for row in conc[1:]
       if not row[HEADER.index("note")].startswith(
           "request-thread" if row[1] == "request" else "curation-thread")]
check("each row's fields stayed with their own event", not bad, bad[:3])
check("both threads' rows are all present",
      sum(1 for r in conc[1:] if r[1] == "request") == N
      and sum(1 for r in conc[1:] if r[1] == "curation") == N)
check("the writer counted what it wrote", wc.rows_written == 2 * N,
      wc.rows_written)

# ── an unwritable path degrades, never raises ───────────────────────────
print("\nIO failure tolerance:")
blocker = tmp / "not-a-dir"
blocker.write_text("I am a file, not a directory")
bad_path = blocker / "sub" / "metrics.csv"     # parent is a FILE
try:
    wf = MetricsWriter(bad_path)
    wf.row("proxy_start", note="this will not land")
    wf.row("request", context_tokens=1)
    check("unwritable path: construction + rows raise nothing", True)
    check("nothing was written, and the failure was counted",
          wf.rows_written == 0 and wf.errors > 0,
          (wf.rows_written, wf.errors))
except Exception as e:
    check("unwritable path: construction + rows raise nothing", False, e)
    check("nothing was written, and the failure was counted", False, e)

# a file that goes away mid-life just gets recreated, not crashed on
p6 = tmp / "vanish" / "metrics.csv"
wv = MetricsWriter(p6)
wv.row("request", context_tokens=1)
os.unlink(p6)
wv.row("request", context_tokens=2)
check("a deleted metrics file is recreated with its header",
      read_rows(p6)[0][0] == "ts_iso" and len(read_rows(p6)) == 2,
      read_rows(p6))

# unknown field names are dropped loudly, not written into the wrong column
p7 = tmp / "unknown" / "metrics.csv"
wu = MetricsWriter(p7)
wu.row("request", context_tokens=5, tokens_out=99)
u = read_rows(p7)[1]
check("an unknown field never shifts the contract columns",
      len(u) == len(HEADER) and dict(zip(HEADER, u))["context_tokens"] == "5", u)

# ── archive totals: correct, and monotonic ──────────────────────────────
print("\narchive totals:")
adir = tmp / "arch" / "archive"
adir.mkdir(parents=True)


def archive_write(name, n, text="x"):
    with open(adir / name, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"role": "user", "content": f"{text}{i}"}) + "\n")


def truth():
    """Ground truth, recomputed the dumb expensive way."""
    files = sorted(adir.glob("archive_*.jsonl"))
    return (sum(sum(1 for _ in open(p, encoding="utf-8")) for p in files),
            sum(p.stat().st_size for p in files))


at = ArchiveTotals(adir)
check("empty archive totals to (0, 0)", at.totals() == (0, 0), at.totals())
archive_write("archive_20260822.jsonl", 5)
check("counts lines and bytes of one file", at.totals() == truth(),
      (at.totals(), truth()))
prev = at.totals()
archive_write("archive_20260822.jsonl", 3)      # same file grows
archive_write("archive_20260823.jsonl", 4)      # a second day
now = at.totals()
check("incremental count matches a full recount across files",
      now == truth(), (now, truth()))
check("totals are monotonic (archive is append-only)",
      now[0] > prev[0] and now[1] > prev[1], (prev, now))
check("a fresh counter agrees with the incremental one",
      ArchiveTotals(adir).totals() == now, (ArchiveTotals(adir).totals(), now))
(adir / "not-an-archive.txt").write_text("ignore me" * 100)
check("non-archive files are not counted", at.totals() == now, at.totals())

# "cannot know" is (None, None) — the CSV gets a blank. An empty archive dir
# really does hold 0 lines; a missing one holds an unknown number. Asserting
# "0 or None" (as this test once did) accepts the exact bug it exists to catch.
check("a missing archive dir reports blanks, not zeros",
      ArchiveTotals(tmp / "nope").totals() == (None, None),
      ArchiveTotals(tmp / "nope").totals())
notdir = tmp / "arch-is-a-file"
notdir.write_text("I am a file, not an archive directory")
check("an archive path that is a FILE reports blanks, not zeros",
      ArchiveTotals(notdir).totals() == (None, None),
      ArchiveTotals(notdir).totals())
if os.geteuid() != 0:
    locked = tmp / "arch-locked"
    (locked / "x").mkdir(parents=True)
    (locked / "x" / "archive_20260822.jsonl").write_text('{"role":"user"}\n')
    os.chmod(locked / "x", 0o000)
    try:
        check("an unreadable archive dir reports blanks, not zeros",
              ArchiveTotals(locked / "x").totals() == (None, None),
              ArchiveTotals(locked / "x").totals())
    finally:
        os.chmod(locked / "x", 0o755)
else:
    print("  SKIP  unreadable-archive-dir check (running as root)")

# rotation: totals must describe the files that EXIST, not the ones we
# happened to count once. A stale entry means every later row overstates the
# archive — the graph keeps quoting bytes nobody can fetch any more.
rot = tmp / "rotate" / "archive"
rot.mkdir(parents=True)
for name, n in (("archive_20260820.jsonl", 4), ("archive_20260821.jsonl", 6)):
    with open(rot / name, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"role": "user", "content": f"turn {i}"}) + "\n")


def rot_truth():
    files = sorted(rot.glob("archive_*.jsonl"))
    return (sum(sum(1 for _ in open(p, encoding="utf-8")) for p in files),
            sum(p.stat().st_size for p in files))


rt = ArchiveTotals(rot)
before_rotation = rt.totals()
check("both archive files counted before rotation",
      before_rotation == rot_truth() and before_rotation[0] == 10,
      (before_rotation, rot_truth()))
os.unlink(rot / "archive_20260820.jsonl")          # rotated away / cleaned up
after_rotation = rt.totals()
check("a deleted archive file stops counting (no phantom lines)",
      after_rotation == rot_truth() and after_rotation[0] == 6,
      (after_rotation, rot_truth()))
check("the pruned counter agrees with a fresh one",
      after_rotation == ArchiveTotals(rot).totals(),
      (after_rotation, ArchiveTotals(rot).totals()))
check("internal state was pruned, not just the sum",
      set(rt._seen) == {"archive_20260821.jsonl"}, sorted(rt._seen))

# ── gating: default upstream mode writes nothing ────────────────────────
print("\nmode gating (upstream parity):")
saved_env = {k: os.environ.get(k) for k in ("ROLLING_CONTEXT_CURATION",
                                            "FERRY_DATA", "HOME")}
gate_dir = tmp / "gate"
os.environ.pop("ROLLING_CONTEXT_CURATION", None)
os.environ["FERRY_DATA"] = str(gate_dir)
check("no curation mode -> metrics disabled", not metrics_enabled())
check("no curation mode -> get_writer() is None", get_writer() is None)
check("nothing was created on disk while disabled", not gate_dir.exists())
check("disabled says WHY, in one sentence naming the switch",
      "ROLLING_CONTEXT_CURATION" in (metrics.disabled_reason() or ""),
      metrics.disabled_reason())
os.environ["ROLLING_CONTEXT_CURATION"] = "ferry"
os.environ["FERRY_DATA"] = str(gate_dir)
gw = get_writer()
check("curation + FERRY_DATA -> a writer at <FERRY_DATA>/metrics.csv",
      gw is not None and gw.path == str(gate_dir / "metrics.csv"),
      gw and gw.path)
check("the writer is shared process-wide (one lock, one cycle counter)",
      get_writer() is gw)
check("enabled -> no disabled reason to print",
      metrics.disabled_reason() is None, metrics.disabled_reason())

# FERRY_DATA unset is a SUPPORTED config: curation and the proxy state store
# both fall back to ~/ferry-data, so the metrics must land there too. Silently
# turning them off left the one config a first-time user runs uninstrumented.
fake_home = tmp / "home"
fake_home.mkdir()
os.environ["HOME"] = str(fake_home)
os.environ.pop("FERRY_DATA", None)
check("curation on, FERRY_DATA unset -> metrics still enabled",
      metrics_enabled())
check("the data dir falls back to ~/ferry-data (same as curation.py)",
      metrics.data_dir() == str(fake_home / "ferry-data"), metrics.data_dir())
gh = get_writer()
check("a writer is built at ~/ferry-data/metrics.csv, not silently skipped",
      gh is not None and gh.path == str(fake_home / "ferry-data" / "metrics.csv"),
      gh and gh.path)
gh.row("proxy_start", note="fallback home")
check("and it really writes there",
      (fake_home / "ferry-data" / "metrics.csv").exists())

# one physical file -> one writer, however the caller spells the path
spellings = [str(fake_home / "ferry-data") + "/",
             str(fake_home / "ferry-data") + "/./",
             str(fake_home / "sub" / ".." / "ferry-data"),
             "~/ferry-data"]
check("an un-normalized path does not get a second writer",
      all(get_writer(s) is gh for s in spellings),
      [(s, get_writer(s).path) for s in spellings if get_writer(s) is not gh])
link = tmp / "linked-data"
os.symlink(fake_home / "ferry-data", link)
check("a symlinked spelling of the same file shares the writer too",
      get_writer(str(link)) is gh, get_writer(str(link)).path)

# HOME is restored right here: the end-to-end block below resolves FERRY_CORE
# through ~, and a fake home would send it looking for a core that isn't there.
if saved_env["HOME"] is None:
    os.environ.pop("HOME", None)
else:
    os.environ["HOME"] = saved_env["HOME"]

# ── cycle numbering survives a restart ──────────────────────────────────
print("\ncycle numbering:")
p8 = tmp / "cycle" / "metrics.csv"
wcy = MetricsWriter(p8)
check("cycles are 1-based", wcy.next_cycle() == 1)
wcy.row("curation", cycle=1, tokens_evicted=10)
c2 = wcy.next_cycle()
wcy.row("curation", cycle=c2, tokens_evicted=10)
check("cycles increment", c2 == 2)
wcy2 = MetricsWriter(p8)      # restart
check("a restarted writer continues the numbering (no cycle 1 twice)",
      wcy2.next_cycle() == 3, wcy2._cycle)

# ── end to end: the producer's own rows ─────────────────────────────────
print("\nend-to-end through the curation producer:")
e2e = tmp / "e2e"
os.environ["ROLLING_CONTEXT_CURATION"] = "ferry"
os.environ["FERRY_DATA"] = str(e2e)
os.environ.setdefault("FERRY_CORE", str(Path.home() / "ferry" / "core"))
from curation import FerryCurationProducer  # noqa: E402

MSGS = [{"role": "user", "content": f"turn {i}: " + "real work here " * 20}
        for i in range(8)]
prod = FerryCurationProducer(data_dir=e2e)
prod.produce(MSGS, 0, 5)
mrows = [dict(zip(HEADER, row)) for row in read_rows(e2e / "metrics.csv")[1:]]
cur = [r for r in mrows if r["event"] == "curation"]
aw = [r for r in mrows if r["event"] == "archive_write"]
check("one curation row emitted", len(cur) == 1, mrows)
check("one archive_write row emitted", len(aw) == 1, mrows)
check("curation + archive_write share the cycle number",
      cur and aw and cur[0]["cycle"] == aw[0]["cycle"] == "1", (cur, aw))
check("tokens_evicted is a positive estimate, labelled as one",
      cur and int(cur[0]["tokens_evicted"]) > 0
      and "estimate:chars/4" in cur[0]["note"], cur)
arch_files = sorted((e2e / "archive").glob("archive_*.jsonl"))
real_lines = sum(sum(1 for _ in open(p, encoding="utf-8")) for p in arch_files)
real_bytes = sum(p.stat().st_size for p in arch_files)
check("archive_lines/bytes match the archive on disk",
      aw and int(aw[0]["archive_lines"]) == real_lines == 5
      and int(aw[0]["archive_bytes"]) == real_bytes, (aw, real_lines, real_bytes))
check("archive_write note carries the pointer span",
      aw and "#L1-L5" in aw[0]["note"], aw)
check("curation rows leave context_tokens blank when nobody passed it",
      cur and cur[0]["context_tokens"] == "", cur)

# A real second cycle evicts DIFFERENT turns — eviction removes turns from
# context, so the same batch cannot legitimately be evicted twice. Re-running
# produce() with identical messages is a REPLAY, not a cycle, and archive
# writes are now idempotent against exactly that (the Phase D restart race).
# This fixture used to replay and assert growth; it was invented, and the
# dedupe caught it.
MSGS2 = [{"role": "user", "content": f"second cycle turn {i} — new material "
          f"that did not exist during cycle one"} for i in range(5)] + MSGS[5:]
prod.produce(MSGS2, 0, 5)    # second cycle, same producer, NEW turns
mrows2 = [dict(zip(HEADER, row)) for row in read_rows(e2e / "metrics.csv")[1:]]
aw2 = [r for r in mrows2 if r["event"] == "archive_write"]
check("cycle 2 is numbered 2", len(aw2) == 2 and aw2[1]["cycle"] == "2", aw2)
check("archive totals grow across cycles (where the tokens went)",
      int(aw2[1]["archive_lines"]) > int(aw2[0]["archive_lines"])
      and int(aw2[1]["archive_bytes"]) > int(aw2[0]["archive_bytes"]), aw2)

# real_tokens: the caller (the compressor) knows the number upstream reported
# when the trigger fired; the producer cannot. One optional kwarg puts the
# cliff on the same curve as the request rows.
print("\ncuration row records WHERE on the curve the cliff happened:")
prod.produce(MSGS, 0, 5, real_tokens=163_840)
mrows3 = [dict(zip(HEADER, row)) for row in read_rows(e2e / "metrics.csv")[1:]]
cur3 = [r for r in mrows3 if r["event"] == "curation"]
check("real_tokens lands in context_tokens",
      len(cur3) == 3 and cur3[2]["context_tokens"] == "163840", cur3)
check("earlier curation rows are untouched (still blank, not backfilled)",
      cur3[0]["context_tokens"] == "" and cur3[1]["context_tokens"] == "", cur3)
check("a curation row never joins the request delta chain (tokens_in blank)",
      all(r["tokens_in"] == "" for r in cur3), cur3)
for bogus in (0, None, "", "not a number", -5):
    prod.produce(MSGS, 0, 5, real_tokens=bogus)
cur4 = [r for r in [dict(zip(HEADER, row))
                    for row in read_rows(e2e / "metrics.csv")[1:]]
        if r["event"] == "curation"]
check("unknown/zero/garbage real_tokens records BLANK, never 0",
      len(cur4) == 8 and all(r["context_tokens"] == "" for r in cur4[3:]),
      [r["context_tokens"] for r in cur4])
check("the producer is still independently callable with no real_tokens",
      bool(FerryCurationProducer(data_dir=tmp / "solo").produce(MSGS, 0, 5)))

# producing with metrics OFF must still curate, silently
off = tmp / "off"
os.environ.pop("ROLLING_CONTEXT_CURATION", None)
prod_off = FerryCurationProducer(data_dir=off)
payload_off = prod_off.produce(MSGS, 0, 5)
check("curation works untouched when metrics are off",
      bool(payload_off) and not (off / "metrics.csv").exists())

# ── live proxy: only /v1/messages is a turn ─────────────────────────────
# The regression this pins: do_POST dispatches on the /v1/messages PREFIX, so
# a /v1/messages/count_tokens probe used to be written down as a `request`
# with a chars/4 pseudo-context — and because the delta chain was one global
# number, it injected two equal-and-opposite spikes into tokens_in. A second
# session (a subagent) did the same. Both are asserted here through a REAL
# proxy talking to a REAL (mock) endpoint, because the bug lived in the wiring,
# not in either module on its own.
print("\nlive proxy (a probe is not a turn, a subagent is not this session):")

MOCK_SRC = '''\
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        if self.path.rstrip("/").endswith("count_tokens"):
            # The real endpoint answers with a bare input_tokens and no usage
            # block — which is exactly why the proxy fell back to chars/4.
            self._send(json.dumps({"input_tokens": 12345}).encode(),
                       "application/json")
            return
        # max_tokens doubles as "report this many input tokens", so each turn
        # can name its own context size.
        n = int(payload.get("max_tokens") or 1000)
        events = [
            {"type": "message_start", "message": {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": payload.get("model", "m"), "content": [],
                "usage": {"input_tokens": n, "output_tokens": 1,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        body = "".join("event: %s\\ndata: %s\\n\\n" % (e["type"], json.dumps(e))
                       for e in events).encode()
        self._send(body, "text/event-stream")


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''

live = tmp / "live"
(live / "home" / ".claude").mkdir(parents=True)
(live / "data").mkdir(parents=True)
mock_py = live / "mock.py"
mock_py.write_text(MOCK_SRC, encoding="utf-8")


def free_port(lo=5600, hi=5699):
    """Test proxies live in 5600-5699 — never near a live session's ports."""
    for port in range(lo, hi + 1):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port in 5600-5699")


def wait_port(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def post(port, path, msgs, session=None, tokens=1000, stream=True):
    headers = {"content-type": "application/json", "x-api-key": "k",
               "anthropic-version": "2023-06-01"}
    if session:
        headers["X-Claude-Code-Session-Id"] = session
    body = {"model": "claude-mock", "max_tokens": tokens,
            "stream": stream, "messages": msgs}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def wait_rows(path, want, timeout=15):
    """Rows land after the response is streamed back, so poll rather than
    sleep-and-hope."""
    end = time.time() + timeout
    rows = []
    while time.time() < end:
        try:
            rows = read_rows(path)
        except OSError:
            rows = []
        if len(rows) >= want:
            return rows
        time.sleep(0.2)
    return rows


mock_port = free_port()
proxy_port = free_port(mock_port + 1)     # never the same port: a proxy that
check("the rig gave the proxy and the mock different ports",   # forwards to
      mock_port != proxy_port, (mock_port, proxy_port))        # itself proves
                                                               # nothing
live_env = dict(os.environ)
for k in list(live_env):
    if k.startswith("ROLLING_CONTEXT_") or k in ("ANTHROPIC_BASE_URL",
                                                 "ANTHROPIC_AUTH_TOKEN"):
        live_env.pop(k, None)
live_env.update(
    HOME=str(live / "home"), USERPROFILE=str(live / "home"),
    ROLLING_CONTEXT_PORT=str(proxy_port),
    ROLLING_CONTEXT_UPSTREAM=f"http://127.0.0.1:{mock_port}",
    ROLLING_CONTEXT_TRIGGER="100000000",     # nothing may compress here
    ROLLING_CONTEXT_CURATION="ferry",
    FERRY_DATA=str(live / "data"),
    FERRY_CORE=os.environ.get("FERRY_CORE", str(Path.home() / "ferry" / "core")),
)
mock_proc = subprocess.Popen([sys.executable, str(mock_py), str(mock_port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
proxy_proc = subprocess.Popen([sys.executable, "server.py"],
                              cwd=str(REPO / "proxy"), env=live_env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
live_csv = live / "data" / "metrics.csv"
try:
    check("mock endpoint came up", wait_port(mock_port))
    check("proxy came up on a 56xx test port", wait_port(proxy_port))
    check("the proxy process is actually alive (not a port stolen by the mock)",
          proxy_proc.poll() is None, proxy_proc.poll())
    convo = [{"role": "user", "content": "turn one, a real question"}]
    post(proxy_port, "/v1/messages", convo, session="sess-A", tokens=100000)
    post(proxy_port, "/v1/messages/count_tokens", convo, session="sess-A",
         tokens=100000, stream=False)
    post(proxy_port, "/v1/messages", convo, session="sess-B", tokens=5000)
    post(proxy_port, "/v1/messages?beta=true", convo, session="sess-A",
         tokens=104000)
    live_rows = wait_rows(live_csv, 6)          # header + start + 4 events
finally:
    proxy_proc.terminate()
    mock_proc.terminate()
    proxy_proc.wait(timeout=15)
    mock_proc.wait(timeout=15)

lrows = [dict(zip(HEADER, row)) for row in live_rows[1:]] if live_rows else []
lreq = [r for r in lrows if r["event"] == "request"]
lprobe = [r for r in lrows if r["event"] == "probe"]
check("the proxy wrote its metrics file", bool(lrows), live_rows)
check("exactly three conversation turns were logged as requests",
      len(lreq) == 3, [(r["event"], r["context_tokens"]) for r in lrows])
check("count_tokens is NOT a request row", len(lprobe) == 1,
      [(r["event"], r["note"]) for r in lrows])
check("the probe row names the path it came from",
      lprobe and "count_tokens" in lprobe[0]["note"], lprobe)
check("the probe carries no context_tokens and no tokens_in",
      lprobe and lprobe[0]["context_tokens"] == ""
      and lprobe[0]["tokens_in"] == "", lprobe)
check("turn context_tokens are the numbers upstream reported",
      [r["context_tokens"] for r in lreq] == ["100000", "5000", "104000"],
      [r["context_tokens"] for r in lreq])
check("the subagent's first turn is blank, not a -95000 cliff",
      lreq and lreq[1]["tokens_in"] == "", lreq)
check("this session's next delta is real (4000), not poisoned by either",
      lreq and lreq[2]["tokens_in"] == "4000", lreq)
check("no fake spike anywhere in the file (nothing over 90k in tokens_in)",
      all(abs(int(r["tokens_in"])) < 90000 for r in lrows if r["tokens_in"]),
      [r["tokens_in"] for r in lrows if r["tokens_in"]])
check("a query string does not stop /v1/messages being a turn",
      lreq and lreq[2]["model"] == "claude-mock", lreq)

for k, v in saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

# ── idempotent archive writes seen from the producer level ──────────────
# Isolated producer + directory ON PURPOSE: an extra produce() advances the
# shared cycle counter, and the first version of this check did exactly that
# and broke four positional assertions downstream. Test fixtures must not
# perturb the state other tests measure.
#
# ASSUMPTION, stated because it is load-bearing and not proven: a
# byte-identical TRAILING batch can only arise from a restart replay, because
# eviction removes turns from context and they cannot be evicted twice. If
# that ever stops holding, dedupe would swallow real content.
print("\nidempotent archive (producer level):")
_rd = Path(tempfile.mkdtemp())
_rp = FerryCurationProducer(_rd)
_RM = [{"role": "user", "content": f"replay turn {i} with enough words to be "
        f"a real looking evicted turn"} for i in range(5)] + \
      [{"role": "user", "content": "kept"}]
_rp.produce(_RM, 0, 5)
_before = sum(f.stat().st_size for f in (_rd / "archive").glob("archive_*.jsonl"))
_rp.produce(_RM, 0, 5)          # byte-identical replay
_after = sum(f.stat().st_size for f in (_rd / "archive").glob("archive_*.jsonl"))
check("a byte-identical REPLAY does not grow the archive (the Phase D "
      "restart race, closed)", _after == _before, (_before, _after))
_new = [{"role": "user", "content": "genuinely new material for cycle two"}] * 5 + \
       [{"role": "user", "content": "kept"}]
_rp.produce(_new, 0, 5)
_grown = sum(f.stat().st_size for f in (_rd / "archive").glob("archive_*.jsonl"))
check("a genuinely new batch still grows the archive", _grown > _after,
      (_after, _grown))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
