#!/usr/bin/env python3
"""Property tests for ferry-fetch — the round trip is the point:
producer archives turns + emits slugs → fetch(slug) returns the SAME
turns, verbatim. Run: python3 core/test_fetch.py
"""

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))
# the producer lives in this repo's proxy/; tests exercise the REAL shared format
sys.path.insert(0, str(CORE.parent / "proxy"))

from fetch import parse_pointer, fetch, render  # noqa: E402
from curation import FerryCurationProducer, SLUG_PREFIX  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{' — ' + str(detail) if detail else ''}")


print("ferry-fetch property tests\n")

# ── pointer parsing ─────────────────────────────────────────────────────
print("pointer parsing:")
check("single line", parse_pointer("archive_20260811.jsonl#L3")
      == ("archive_20260811.jsonl", 3, 3))
check("range", parse_pointer("archive_20260811.jsonl#L3-L7")
      == ("archive_20260811.jsonl", 3, 7))
check("range without second L", parse_pointer("archive_20260811.jsonl#L3-7")
      == ("archive_20260811.jsonl", 3, 7))
for bad in ("archive_2026.jsonl#L3", "archive_20260811.jsonl#L0",
            "archive_20260811.jsonl#L7-L3", "../../etc/passwd#L1",
            "archive_20260811.jsonl", ""):
    check(f"malformed rejected: {bad!r}", parse_pointer(bad) is None)

# ── the round trip (producer → slug → fetch → verbatim) ─────────────────
print("\nround trip with the real producer:")
tmp = Path(tempfile.mkdtemp())
data = tmp / "ferry-data"
producer = FerryCurationProducer(data_dir=data)

PNG = b"\x89PNG\r\n\x1a\nround-trip-test-bytes"
MESSAGES = [
    {"role": "user", "content": "The harbor beacon code is MOONRISE-47 and "
                                "the relief keeper arrives on Thursday."},
    {"role": "assistant", "content": "Noted: beacon MOONRISE-47, relief "
                                     "keeper Thursday."},
    {"role": "user", "content": [
        {"type": "text", "text": "Photo of the beacon:"},
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png",
                                     "data": base64.b64encode(PNG).decode()}}]},
    {"role": "user", "content": "Recent turn stays."},
    {"role": "assistant", "content": "Indeed it does."},
]

payload = producer.produce(MESSAGES, 0, 3)
slugs = [l for l in payload.splitlines() if l.startswith(SLUG_PREFIX)]
pointers = [s.rsplit(" → ", 1)[1].strip() for s in slugs]
check("producer emitted 3 pointers", len(pointers) == 3, pointers)

turns = fetch(pointers[0], data_dir=data)
check("fetch returns the evicted turn verbatim",
      len(turns) == 1 and turns[0]["content"] == MESSAGES[0]["content"],
      turns)
check("the beacon code comes back through the pointer",
      "MOONRISE-47" in turns[0]["content"])

fname = pointers[0].split("#")[0]
all_three = fetch(f"{fname}#L1-L3", data_dir=data)
check("range fetch returns all evicted turns in order",
      len(all_three) == 3
      and all_three[1]["content"] == MESSAGES[1]["content"])

rendered = render(all_three)
check("render carries verbatim text + blob ref",
      "MOONRISE-47" in rendered and "reconstitute with ferry-extract" in rendered
      and "sha256-" in rendered, rendered[:200])

# ── failure honesty ─────────────────────────────────────────────────────
print("\nfailure honesty:")
try:
    fetch(f"{fname}#L99", data_dir=data)
    check("out-of-range pointer raises", False)
except ValueError as e:
    check("out-of-range pointer raises", True, e)
try:
    fetch("archive_19990101.jsonl#L1", data_dir=data)
    check("missing archive raises", False)
except FileNotFoundError:
    check("missing archive raises", True)

# ── CLI (the shape a mind's Bash tool actually calls) ───────────────────
print("\nCLI:")
env = {"FERRY_DATA": str(data), "PATH": "/usr/bin:/bin"}
r = subprocess.run([sys.executable, str(CORE / "fetch.py"), pointers[0]],
                   capture_output=True, text=True, env=env)
check("CLI fetch: exit 0 + verbatim content",
      r.returncode == 0 and "MOONRISE-47" in r.stdout, r.stderr)
r = subprocess.run([sys.executable, str(CORE / "fetch.py"), "--json",
                    pointers[0]], capture_output=True, text=True, env=env)
check("CLI --json: parseable, verbatim",
      r.returncode == 0
      and json.loads(r.stdout)[0]["content"] == MESSAGES[0]["content"])
r = subprocess.run([sys.executable, str(CORE / "fetch.py"), "nope#L1"],
                   capture_output=True, text=True, env=env)
check("CLI malformed pointer: exit 1, error on stderr",
      r.returncode == 1 and "malformed" in r.stderr)


# ── the fetch row (recall is half the crossing; it must be measurable) ──
# Same 12-column contract as proxy/metrics.py, written by this file with the
# stdlib csv module — the fork is not importable from here, so the header is
# mirrored, and this is the test that keeps the two copies honest.
print("\nmetrics fetch row:")
from fetch import METRICS_HEADER, _log_fetch  # noqa: E402
import csv  # noqa: E402
import os  # noqa: E402

try:
    from metrics import HEADER as PROXY_HEADER  # noqa: E402
    check("header matches proxy/metrics.py column-for-column",
          METRICS_HEADER == list(PROXY_HEADER), (METRICS_HEADER, PROXY_HEADER))
except ImportError:
    check("header matches proxy/metrics.py column-for-column", False,
          "proxy/metrics.py not importable — cannot verify the contract")


def rows_of(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


# The CLI runs above already exported FERRY_DATA; the two successful fetches
# there must have left rows behind.
mcsv = data / "metrics.csv"
check("CLI fetches wrote metrics.csv", mcsv.exists(), str(mcsv))
if mcsv.exists():
    rows = rows_of(mcsv)
    check("header written exactly once, first", rows[0] == METRICS_HEADER,
          rows[0])
    frows = [r for r in rows[1:] if len(r) > 1 and r[1] == "fetch"]
    check("one fetch row per successful CLI fetch (2), none for the bad one",
          len(frows) == 2, rows[1:])
    if frows:
        r = dict(zip(METRICS_HEADER, frows[0]))
        check("note carries the pointer and the turn count",
              pointers[0] in r["note"] and "turns=1" in r["note"], r["note"])
        check("BLANK IS NOT ZERO: every unknown column is empty",
              all(r[c] == "" for c in METRICS_HEADER
                  if c not in ("ts_iso", "event", "note")), r)
        check("ts_iso is the same shape the collector stamps",
              len(r["ts_iso"]) == 20 and r["ts_iso"].endswith("Z")
              and r["ts_iso"][10] == "T", r["ts_iso"])

# opt-in: no FERRY_DATA, no CSV appears anywhere
quiet = Path(tempfile.mkdtemp()) / "quiet"
quiet.mkdir(parents=True)
os.environ.pop("FERRY_DATA", None)
_log_fetch(quiet, "archive_20260811.jsonl#L1", 1)
check("no FERRY_DATA -> no metrics file is created",
      not (quiet / "metrics.csv").exists())

# a fetch that cannot log itself is still a fetch
os.environ["FERRY_DATA"] = str(data)
before = len(rows_of(mcsv)) if mcsv.exists() else 0
_log_fetch(Path("/proc/nonexistent-ferry-dir"), "archive_20260811.jsonl#L1", 1)
check("unwritable metrics dir never raises", True)
turns_again = fetch(pointers[0], data_dir=data)
check("in-process fetch still returns the turns while logging",
      len(turns_again) == 1 and "MOONRISE-47" in turns_again[0]["content"])
check("...and appended exactly one row", len(rows_of(mcsv)) == before + 1,
      (before, len(rows_of(mcsv))))
os.environ.pop("FERRY_DATA", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
