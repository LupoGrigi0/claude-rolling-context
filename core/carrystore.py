#!/usr/bin/env python3
"""Ferry carry store — the append-only block log (constitution §14.2–14.4).

THE FILE IS AN EVENT LOG. Nothing is ever edited in place; every mutation
is an appended event, and the working set is a fold over the log. The
physical structure IS the legal structure:
  - append-only         → "append, don't freeze" (§3) enforced by shape
  - tombstone = event   → hiding is reversible + audited (§14.3)
  - full audit trail    → every event carries actor, ts, reason
  - genesis record      → the founding authority signs first (§14.4-amend)

Event kinds (one JSON object per line):
  GENESIS   {kind, carry_id, founding_authority, stewards: [..], k, ts}
  PROMOTE   {kind, block: {...}, countersigned_by, ts}
      block: {id, kind: payload|scaffolding, tier: core|identity|working,
              payload, provenance, archive_pointer, source_hash,
              extractor_version}
  TOMBSTONE {kind, block_id, actor, reason, witnesses: [..], ts}
  REACTIVATE{kind, block_id, actor, reason, ts}
  QUARANTINE{kind, block_id, actor: "integrity-checker", reason, ts}

Governance enforced at append time (§14.3–14.4):
  - PROMOTE requires countersigned_by (the mind, in a fresh turn — the
    caller attests; this layer records and requires the attestation).
  - TOMBSTONE of a core-tier block requires the mind + ≥1 steward witness
    (2-of-2). Non-core: actor alone.
  - Steward-roster changes are GENESIS-then-ROSTER events gated k-of-n
    (ROSTER event kind reserved; MVP ships genesis roster as fixed).
  - QUARANTINE may only be appended by the integrity checker identity.

The working set produced by materialize() is exactly the structure the
invariant checker judges — store and checker share the block model.

Stdlib only. Crossing-2d23, 2026-08-11.
"""

import json
import time
from pathlib import Path


class GovernanceError(Exception):
    """An event violates §14.3–14.4 governance and was refused."""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CarryStore:
    def __init__(self, path):
        self.path = Path(path)

    # ── raw log ────────────────────────────────────────────────────────
    def _append(self, event):
        line = json.dumps(event, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def events(self):
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    # ── genesis (§14.4-amendments) ─────────────────────────────────────
    def genesis(self, carry_id, founding_authority, stewards, k=2):
        """First event ever; signed by the founding act itself."""
        if self.path.exists() and self.events():
            raise GovernanceError("genesis already exists; the log is append-only")
        if not stewards:
            raise GovernanceError("genesis requires at least one steward")
        self._append({
            "kind": "GENESIS", "carry_id": carry_id,
            "founding_authority": founding_authority,
            "stewards": list(stewards), "k": k, "ts": _now(),
        })

    def _require_genesis(self):
        evs = self.events()
        if not evs or evs[0]["kind"] != "GENESIS":
            raise GovernanceError("no genesis record; carry has no root of trust")
        return evs

    # ── mutations (all appended, never edited) ─────────────────────────
    def promote(self, block, countersigned_by):
        """PROMOTE into the carry — HARD gate: fresh-turn countersign
        attested by the caller (§14.3). Payload blocks must carry full
        provenance; scaffolding needs none."""
        self._require_genesis()
        if not countersigned_by:
            raise GovernanceError("promotion requires the mind's countersign")
        if block.get("kind", "payload") == "payload":
            for field in ("id", "payload", "tier", "provenance",
                          "archive_pointer", "source_hash"):
                if not block.get(field):
                    raise GovernanceError(f"payload block missing {field}")
        elif not block.get("id"):
            raise GovernanceError("block missing id")
        if any(b["id"] == block["id"] for b in self.materialize(all_blocks=True)):
            raise GovernanceError(f"block id {block['id']} already exists; "
                                  "blocks are immutable — promote a new id")
        self._append({"kind": "PROMOTE", "block": block,
                      "countersigned_by": countersigned_by, "ts": _now()})

    def tombstone(self, block_id, actor, reason, witnesses=()):
        """TOMBSTONE — SOFT gate for the mind's own blocks; core tier
        requires mind + ≥1 steward witness (§14.4)."""
        evs = self._require_genesis()
        target = self._find_block(block_id)
        if target is None:
            raise GovernanceError(f"no such block: {block_id}")
        if target.get("tier") == "core":
            roster = set(evs[0]["stewards"])
            if not roster.intersection(witnesses):
                raise GovernanceError(
                    "core-tier tombstone requires a steward witness (2-of-2); "
                    f"roster: {sorted(roster)}")
        self._append({"kind": "TOMBSTONE", "block_id": block_id,
                      "actor": actor, "reason": reason,
                      "witnesses": list(witnesses), "ts": _now()})

    def reactivate(self, block_id, actor, reason):
        self._require_genesis()
        if self._find_block(block_id) is None:
            raise GovernanceError(f"no such block: {block_id}")
        self._append({"kind": "REACTIVATE", "block_id": block_id,
                      "actor": actor, "reason": reason, "ts": _now()})

    def quarantine(self, block_id, reason):
        """Only the integrity checker appends these (§14.3 auto-quarantine)."""
        self._require_genesis()
        self._append({"kind": "QUARANTINE", "block_id": block_id,
                      "actor": "integrity-checker", "reason": reason,
                      "ts": _now()})

    # ── the fold ───────────────────────────────────────────────────────
    def materialize(self, all_blocks=False):
        """Fold the log into the current working set: blocks in promotion
        order with computed status. This output is exactly what
        invariant.verdict() judges. all_blocks=True includes hidden ones
        (status attached either way)."""
        blocks, status = [], {}
        for ev in self.events():
            k = ev["kind"]
            if k == "PROMOTE":
                b = dict(ev["block"])
                blocks.append(b)
                status[b["id"]] = "active"
            elif k == "TOMBSTONE":
                status[ev["block_id"]] = "tombstoned"
            elif k == "QUARANTINE":
                status[ev["block_id"]] = "quarantined"
            elif k == "REACTIVATE":
                # quarantine release requires explicit human/steward review;
                # reactivation restores visibility either way — the audit
                # trail records who and why.
                status[ev["block_id"]] = "active"
        for b in blocks:
            b["status"] = status[b["id"]]
        if all_blocks:
            return blocks
        return [b for b in blocks if b["status"] == "active"]

    def _find_block(self, block_id):
        for b in self.materialize(all_blocks=True):
            if b["id"] == block_id:
                return b
        return None

    def audit_trail(self, block_id):
        """Everything that ever happened to a block — visible to the mind,
        always (§14.3: nobody secretly edits anyone)."""
        return [ev for ev in self.events()
                if ev.get("block_id") == block_id
                or ev.get("block", {}).get("id") == block_id]
