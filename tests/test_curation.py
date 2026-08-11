#!/usr/bin/env python3
"""Property tests for the Ferry curation producer.

Written to the Ferry constitution (DESIGN-REV2 §14) and the STRIP-MAP
producer contract — before wiring, per the checker-first rule.
Offline: no network, no LLM (curation is deterministic).
Run: python3 tests/test_curation.py
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))
FERRY_CORE = Path.home() / "ferry" / "core"
os.environ["FERRY_CORE"] = str(FERRY_CORE)
os.environ["ROLLING_CONTEXT_CURATION"] = "ferry"

from curation import FerryCurationProducer, SLUG_PREFIX  # noqa: E402

sys.path.insert(0, str(FERRY_CORE))
from invariant import verdict, sha256_hex  # noqa: E402
from carrystore import CarryStore  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{' — ' + str(detail) if detail else ''}")


tmp = Path(tempfile.mkdtemp())
data = tmp / "ferry-data"
producer = FerryCurationProducer(data_dir=data)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes-for-test"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()

MESSAGES = [
    {"role": "user", "content": "We chose Postgres over MongoDB because we "
                                "need real foreign keys."},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Agreed — boring, proven, done arguing."},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "node -v"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "v20.19.5\n" + "build log line: everything fine\n" * 120}]},
    {"role": "user", "content": [
        {"type": "text", "text": "Here is the screenshot:"},
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png",
                                     "data": PNG_B64}}]},
    {"role": "assistant", "content": "Measure twice, cut once — I never "
                                     "ship on a Friday."},
    {"role": "user", "content": "Latest question about deployment."},
    {"role": "assistant", "content": "Latest answer about deployment."},
]

print("ferry curation producer property tests\n")

# ── first curation cycle ────────────────────────────────────────────────
print("archive + slugs:")
payload = producer.produce(MESSAGES, start_idx=0, keep_from_idx=5)

archive_files = sorted((data / "archive").glob("archive_*.jsonl"))
check("archive file created", len(archive_files) == 1, archive_files)
archived = [json.loads(l) for l in
            archive_files[0].read_text().strip().split("\n")]
check("all 5 evicted turns archived", len(archived) == 5, len(archived))

check("text turns archived byte-identical",
      archived[0]["content"] == MESSAGES[0]["content"]
      and archived[4]["content"] == MESSAGES[4]["content"])

slugs = [l for l in payload.splitlines() if l.startswith(SLUG_PREFIX)]
check("one slug per evicted turn", len(slugs) == 5, slugs)
check("slugs carry pointers to file#line",
      all(f"{archive_files[0].name}#L" in s for s in slugs), slugs)

# gists are verbatim contiguous prefixes (invariant-legal selections)
gist0 = slugs[0].split("] ", 1)[1].rsplit(" → ", 1)[0]
check("gist is a verbatim prefix of the archived turn",
      MESSAGES[0]["content"].startswith(gist0), gist0)

# ── §15.1a: blobs never inline ──────────────────────────────────────────
print("\nblob extraction (§15.1a):")
digest = hashlib.sha256(PNG_BYTES).hexdigest()
blob_path = data / "archive" / "blobs" / f"sha256-{digest}"
check("blob stored as raw binary, filename = its own sha256",
      blob_path.exists() and blob_path.read_bytes() == PNG_BYTES)
check("no base64 blob data inline in the archive",
      PNG_B64 not in archive_files[0].read_text())
img_turn = archived[3]["content"]
check("archived turn holds a ferry_blob ref",
      any(isinstance(b, dict) and b.get("type") == "ferry_blob"
          and b.get("sha256") == digest for b in img_turn), img_turn)

# ── payload judged by THE checker ───────────────────────────────────────
print("\ninvariant integration:")
sources = [json.dumps(r, ensure_ascii=False) for r in archived] + [
    r["content"] for r in archived if isinstance(r["content"], str)]
gist_blocks = []
for i, s in enumerate(slugs):
    g = s.split("] ", 1)[1].rsplit(" → ", 1)[0]
    src = archived[i]["content"]
    text = src if isinstance(src, str) else json.dumps(src, ensure_ascii=False)
    if g and g in text:
        gist_blocks.append({"id": f"g{i}", "kind": "payload",
                            "tier": "working", "payload": g,
                            "source_hash": sha256_hex(g)})
r = verdict(gist_blocks, sources)
check("every gist judged LEGAL against the archive",
      r["verdict"] == "legal" and len(gist_blocks) >= 3, r)

# ── carry render ────────────────────────────────────────────────────────
print("\ncarry render:")
store = CarryStore(data / "carry.jsonl")
store.genesis("test-carry", "Lupo", ["Lupo", "Axiom", "Crossing-2d23"], k=2)
IDENTITY = "I chose the name Crossing because a bridge is also a verb."
store.promote({"id": "core-1", "kind": "payload", "tier": "core",
               "payload": IDENTITY, "provenance": "ep0",
               "archive_pointer": "genesis", "source_hash": sha256_hex(IDENTITY),
               "extractor_version": "v1"}, countersigned_by="Crossing-2d23")
store.promote({"id": "w-2", "kind": "payload", "tier": "working",
               "payload": "tombstone me", "provenance": "ep0",
               "archive_pointer": "genesis", "source_hash": sha256_hex("tombstone me"),
               "extractor_version": "v1"}, countersigned_by="Crossing-2d23")
store.tombstone("w-2", actor="Crossing-2d23", reason="test")

payload2 = producer.produce(MESSAGES, 0, 5, existing_payload=payload)
check("carry rendered verbatim at head", IDENTITY in payload2)
check("tombstoned block absent from render", "tombstone me" not in payload2)

# ── rolling merge (append-only pointer growth) ──────────────────────────
print("\nrolling merge:")
slugs2 = [l for l in payload2.splitlines() if l.startswith(SLUG_PREFIX)]
check("prior slugs carried forward + new appended (5 + 5)",
      len(slugs2) == 10, len(slugs2))

# ── end-to-end through RollingCompressor ────────────────────────────────
print("\nend-to-end through the compressor:")
os.environ["FERRY_DATA"] = str(data)
import compressor as compressor_mod  # noqa: E402
comp = compressor_mod.RollingCompressor(target_tokens=1)  # force aggressive cut
result = comp.compress(MESSAGES, auth_headers={}, real_token_count=100000,
                       payload={"model": "claude-haiku-4-5-20251001"})
check("compress() returns curated result without any LLM call",
      result is not None, result)
if result:
    check("envelope shape: [summary(user), ack(assistant), *recent]",
          result[0]["role"] == "user" and result[1]["role"] == "assistant"
          and compressor_mod.SUMMARY_MARKER in result[0]["content"])
    n_recent = len(result) - 2
    check("recent tail byte-identical to live messages",
          result[2:] == MESSAGES[len(MESSAGES) - n_recent:])
    check("curated head materially smaller than evicted content",
          comp._count_chars(result) < comp._count_chars(MESSAGES))
    check("honest framing (no 'summary of our earlier conversation' claim)",
          "moved, not lost" in result[0]["content"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
