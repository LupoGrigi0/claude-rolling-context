#!/usr/bin/env python3
"""Ferry restart-safety: the proxy's compression store must survive a restart.

Without persistence, a restart forgets the match table -> next request ships
full history, re-curates, re-archives, loses the pointer index. These tests
prove the store round-trips through disk and that a "restarted" store still
matches the same request (so curation does NOT re-fire on already-curated
content). Run: python3 tests/test_persistence.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))

# Force curation mode + a throwaway data dir BEFORE importing server (it reads
# these at module load to decide whether to persist).
tmp = Path(tempfile.mkdtemp())
os.environ["ROLLING_CONTEXT_CURATION"] = "ferry"
os.environ["FERRY_DATA"] = str(tmp / "ferry-data")
os.environ["FERRY_CORE"] = str(Path.home() / "ferry" / "core")

import server  # noqa: E402
from server import CompressionStore  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{' — ' + str(detail) if detail else ''}")


PERSIST = str(tmp / "state.json")
PREFIX = [{"role": "user", "content": "[carry] identity + pointer index here"},
          {"role": "assistant", "content": "ack"}]
HASHES = ["deadbeef00000001", "deadbeef00000002", "deadbeef00000003"]

print("ferry persistence property tests\n")

# ── a resolved entry survives a restart ─────────────────────────────────
print("round trip:")
s1 = CompressionStore(persist_path=PERSIST)
e = s1.add()
e["prefix"] = PREFIX
e["original_hashes"] = HASHES
s1.persist()
check("persist wrote the state file", os.path.exists(PERSIST))

# simulate a proxy restart: brand-new store, same path
s2 = CompressionStore(persist_path=PERSIST)
check("restored exactly one resolved compression", len(s2.compressions) == 1,
      len(s2.compressions))
r = s2.compressions[0]
check("prefix survived verbatim", r["prefix"] == PREFIX)
check("original_hashes survived verbatim", r["original_hashes"] == HASHES)
check("transient fields reset clean",
      r["pending"] is None and r["thread"] is None)

# ── the restored store still MATCHES — curation won't re-fire ────────────
print("\nmatch after restart (no re-curation):")
# a request whose history contains the archived hash-chain, plus new turns
req_hashes = HASHES + ["newhash00000001", "newhash00000002"]
match, end = s2.compressions and s2.find_match(req_hashes) or (None, -1)
check("restored entry matches the same request",
      match is not None and match["prefix"] == PREFIX, (match, end))
check("match ends where the chain ends (rest is recent tail)",
      end == len(HASHES), end)

# ── unresolved (pending-only) entries are NOT persisted ─────────────────
print("\nonly resolved entries persist:")
s3 = CompressionStore(persist_path=str(tmp / "state2.json"))
p = s3.add()
p["pending"] = PREFIX          # in-flight, not yet promoted
p["pending_hashes"] = HASHES
s3.persist()
s4 = CompressionStore(persist_path=str(tmp / "state2.json"))
check("pending-only entry is not restored (nothing half-baked)",
      len(s4.compressions) == 0, len(s4.compressions))

# ── remove() keeps the snapshot in sync ─────────────────────────────────
print("\nremove syncs disk:")
s2.remove(s2.compressions[0])
s5 = CompressionStore(persist_path=PERSIST)
check("removed entry is gone after restart", len(s5.compressions) == 0)

# ── corrupt/torn state file must not crash boot ─────────────────────────
print("\ncorruption tolerance:")
bad = str(tmp / "bad.json")
with open(bad, "w") as f:
    f.write('[{"prefix": [{"role":"user","content":"x"}], "original_hash')  # torn
s6 = CompressionStore(persist_path=bad)
check("torn state file -> empty store, no crash", len(s6.compressions) == 0)

# ── off-mode writes nothing (upstream parity) ───────────────────────────
print("\ndefault mode parity:")
s7 = CompressionStore()  # no persist path
e7 = s7.add()
e7["prefix"] = PREFIX
s7.persist()  # must be a silent no-op
check("no persist path -> persist() is a harmless no-op", s7._persist_path is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
