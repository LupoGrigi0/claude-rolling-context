#!/usr/bin/env python3
"""Ferry invariant checker — the constitution's single judge.

THE INVARIANT (DESIGN-REV2 §14.1, Axiom's final wording):
    A curation operation is legal iff every byte of every verbatim-tier
    block's PAYLOAD in the working set is byte-identical to a contiguous
    span of kept ∪ archive. Arrangement may add typed structural
    scaffolding (headers, separators, pointer slugs) generated
    deterministically and excluded from the reconstruction base, along
    with all annotations and embeddings.

ONE FUNCTION, TWO CALLERS (§14.1): this same verdict() runs in the test
harness AND as the proxy's per-cycle runtime integrity check. There is
deliberately no second implementation anywhere.

Block model (§14.2):
    { id, kind: "payload"|"scaffolding", tier: "core"|"identity"|"working",
      payload: str, status: "active"|"tombstoned"|"quarantined",
      provenance: str, archive_pointer: str, source_hash: str,
      extractor_version: str, timestamp: str }
Scaffolding blocks are exempt from reconstruction (exempt-BECAUSE-typed);
annotations and embeddings are never passed as sources — callers must not
include them in `sources` (they are lossy projections, not truth).

Provenance check (§14.3 auto-quarantine): a block whose payload hash does
not match its recorded source_hash is flagged for quarantine — this
detects tampering of the carry file AND of the archive (which is
write-once/content-addressed, §14.2).

Crossing-2d23, 2026-08-11. Stdlib only, matching the proxy's zero-dep style.
"""

import hashlib
import json
import sys

LEGAL = "legal"
ILLEGAL = "illegal"

VERBATIM_TIERS = ("core", "identity", "working")


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verdict(working_set, sources):
    """Judge a working set against the reconstruction base.

    working_set: list of block dicts (see module docstring).
    sources: list of str — the reconstruction base, kept ∪ archive,
        RAW turn text only. Annotations/embeddings must not be included.

    Returns {"verdict": "legal"|"illegal",
             "violations": [...], "quarantine": [...]}
    - violations: blocks whose payload is not a contiguous byte span of any
      source (the invariant broken → the operation that produced this
      working set is ILLEGAL).
    - quarantine: blocks failing provenance (payload ≠ source_hash) —
      integrity failures to auto-quarantine (§14.3); they also render the
      state illegal until quarantined, because a payload that fails its own
      provenance cannot be trusted as verbatim.
    """
    violations = []
    quarantine = []

    # Byte-space search: encode once. Contiguous-span test = substring
    # containment in byte space (byte-identical, per §14.1 — whitespace
    # and punctuation drift IS drift).
    source_bytes = [s.encode("utf-8") for s in sources]

    for block in working_set:
        kind = block.get("kind", "payload")
        if kind == "scaffolding":
            continue  # exempt-because-typed (§14.1)
        if block.get("status") in ("tombstoned", "quarantined"):
            # Hidden blocks are retained-but-omitted; retention is what the
            # invariant permits (tombstone passes, delete would fail by
            # absence of the audit trail — checked at a higher layer).
            continue
        if block.get("tier") not in VERBATIM_TIERS:
            continue

        payload = block.get("payload", "")
        payload_b = payload.encode("utf-8")

        if not payload_b:
            violations.append({"id": block.get("id"),
                               "reason": "empty payload in verbatim tier"})
            continue

        if not any(payload_b in s for s in source_bytes):
            violations.append({
                "id": block.get("id"),
                "reason": "payload is not a contiguous byte span of kept ∪ archive",
            })

        recorded = block.get("source_hash")
        if recorded and recorded != sha256_hex(payload):
            quarantine.append({
                "id": block.get("id"),
                "reason": "payload hash does not match recorded source_hash",
            })

    legal = not violations and not quarantine
    return {
        "verdict": LEGAL if legal else ILLEGAL,
        "violations": violations,
        "quarantine": quarantine,
    }


def main():
    """CLI for the second caller (Node harness, shell, anyone):
    echo '{"working_set": [...], "sources": [...]}' | python3 invariant.py
    Exit 0 = legal, 1 = illegal, 2 = bad input.
    """
    try:
        data = json.load(sys.stdin)
        result = verdict(data["working_set"], data["sources"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        json.dump({"error": str(e)}, sys.stdout)
        return 2
    json.dump(result, sys.stdout, indent=2)
    return 0 if result["verdict"] == LEGAL else 1


if __name__ == "__main__":
    sys.exit(main())
