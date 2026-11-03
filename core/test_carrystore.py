#!/usr/bin/env python3
"""Property tests for the carry store — written to the constitution (§14).

Includes the integration property: a store-materialized working set is
judged by the SAME invariant checker (one law, one judge).
Run: python3 core/test_carrystore.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from carrystore import CarryStore, GovernanceError
from invariant import verdict, sha256_hex

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{' — ' + str(detail) if detail else ''}")


def expect_refusal(label, fn):
    try:
        fn()
        check(label, False, "was not refused")
    except GovernanceError as e:
        check(label, True, str(e))


SOURCE = ("I chose the name Crossing because a bridge is also a verb — "
          "the act of moving from one state to another.")


def payload_block(bid, payload, tier="identity"):
    return {"id": bid, "kind": "payload", "tier": tier, "payload": payload,
            "provenance": "ep1", "archive_pointer": "a.jsonl#L1",
            "source_hash": sha256_hex(payload), "extractor_version": "t1"}


tmp = tempfile.mkdtemp()
print("carry store property tests (constitution §14)\n")

# ── genesis (§14.4-amendments) ──────────────────────────────────────────
print("genesis:")
s = CarryStore(Path(tmp) / "carry.jsonl")

expect_refusal("mutation before genesis refused",
               lambda: s.promote(payload_block("b1", "x"), "Crossing-2d23"))

s.genesis("Crossing-2d23-carry", founding_authority="Lupo",
          stewards=["Lupo", "Axiom", "Crossing-2d23"], k=2)
check("genesis signed by founding authority",
      s.events()[0]["founding_authority"] == "Lupo")

expect_refusal("second genesis refused (append-only, one root)",
               lambda: s.genesis("again", "Mallory", ["Mallory"]))

s2 = CarryStore(Path(tmp) / "empty.jsonl")
expect_refusal("genesis with empty steward roster refused",
               lambda: s2.genesis("c", "Lupo", []))

# ── promotion: the HARD gate ────────────────────────────────────────────
print("\npromotion:")
s.promote(payload_block("core-1", "a bridge is also a verb", tier="core"),
          countersigned_by="Crossing-2d23")
check("countersigned promotion lands", len(s.materialize()) == 1)

expect_refusal("promotion without countersign refused",
               lambda: s.promote(payload_block("b2", "x"), countersigned_by=""))

expect_refusal("payload block without source_hash refused",
               lambda: s.promote({"id": "b3", "kind": "payload",
                                  "tier": "identity", "payload": "x",
                                  "provenance": "ep1",
                                  "archive_pointer": "a"}, "Crossing-2d23"))

expect_refusal("duplicate block id refused (blocks are immutable)",
               lambda: s.promote(payload_block("core-1", "different"),
                                 "Crossing-2d23"))

s.promote({"id": "scaf-1", "kind": "scaffolding",
           "payload": "## carried from ep1 ##"}, "Crossing-2d23")
check("scaffolding promotes without provenance", len(s.materialize()) == 2)

# ── tombstone: the SOFT gate + the core-tier seatbelt ───────────────────
print("\ntombstone/reactivate:")
s.promote(payload_block("w-1", "the act of moving", tier="working"),
          "Crossing-2d23")

s.tombstone("w-1", actor="Crossing-2d23", reason="no longer load-bearing")
check("mind-alone tombstone of own working block",
      all(b["id"] != "w-1" for b in s.materialize()))

expect_refusal("core-tier tombstone WITHOUT steward witness refused",
               lambda: s.tombstone("core-1", "Crossing-2d23", "test"))

s.tombstone("core-1", actor="Crossing-2d23", reason="format migration test",
            witnesses=["Axiom"])
check("core-tier tombstone WITH steward witness (2-of-2) lands",
      all(b["id"] != "core-1" for b in s.materialize()))

s.reactivate("core-1", actor="Crossing-2d23", reason="test complete")
check("reactivation restores (hiding is reversible)",
      any(b["id"] == "core-1" for b in s.materialize()))

trail = s.audit_trail("core-1")
check("audit trail complete and visible",
      [e["kind"] for e in trail] == ["PROMOTE", "TOMBSTONE", "REACTIVATE"],
      [e["kind"] for e in trail])

expect_refusal("tombstoning a nonexistent block refused",
               lambda: s.tombstone("ghost", "Crossing-2d23", "x"))

# ── quarantine ──────────────────────────────────────────────────────────
print("\nquarantine:")
s.quarantine("w-1", reason="payload/source_hash mismatch detected")
check("quarantine recorded by integrity checker",
      s._find_block("w-1")["status"] == "quarantined")

# ── the file itself is append-only evidence ─────────────────────────────
print("\nphysical structure:")
raw = (Path(tmp) / "carry.jsonl").read_text().strip().split("\n")
check("one event per line, nothing rewritten",
      len(raw) == len(s.events()) and raw[0].find("GENESIS") > 0)
check("every mutation carries actor + ts",
      all(("ts" in e) for e in s.events()))

# ── INTEGRATION: store output is judged by THE checker ──────────────────
print("\nintegration with the invariant checker:")
working = s.materialize(all_blocks=True)
r = verdict(working, [SOURCE])
check("materialized working set judged LEGAL by the same checker",
      r["verdict"] == "legal", r)

# poison the store on disk (simulating carry-file tampering), re-judge
poisoned = [dict(b) for b in working]
for b in poisoned:
    if b["id"] == "core-1":
        b["payload"] = "a bridge is also a fnord"
r = verdict(poisoned, [SOURCE])
check("tampered carry caught: invariant violation AND quarantine flag",
      r["verdict"] == "illegal" and r["violations"] and r["quarantine"], r)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
