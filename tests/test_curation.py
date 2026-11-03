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
# FERRY_CORE is the proxy's OWN configured seam (proxy/curation.py:_ferry_core_on_path).
# This test used to hardcode Path.home()/"ferry"/"core" -- which passes on every box we
# own and NOWHERE else (Lodestone-8ec9 measured it NO-VERDICT on a fresh clone). Since
# ship item S2 the core lives in this repo's core/, which is also the proxy's default,
# so a missing module is now a real FAILURE, not an env skip.
FERRY_CORE = Path(os.environ.get("FERRY_CORE") or (REPO / "core"))
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
# real_token_count must be REALISTIC relative to MESSAGES. The old fixture used
# target=1 with real=100,000, which since 2026-08-26 correctly trips the
# "target is below the unevictable floor" refusal (§23): scaffolding alone would
# be ~100,000 tokens and no eviction can reach a target of 1. Use a real count
# close to the messages' own size so this exercises the curation path it was
# written to exercise. The refusal itself is tested separately below.
_msg_tokens = compressor_mod.RollingCompressor()._count_chars(MESSAGES) // 4
comp = compressor_mod.RollingCompressor(target_tokens=max(1, _msg_tokens // 4))
result = comp.compress(MESSAGES, auth_headers={}, real_token_count=_msg_tokens,
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

# ── regression: first-live-crossing finding (2026-08-11) ────────────────
# Claude Code prepends <system-reminder> blocks to user turns; a naive
# first-80-chars gist captured boilerplate and buried the planted beacon
# code. Gists must skip harness boilerplate.
print("\nlive-crossing regression:")
boiler = ("<system-reminder>\nAvailable agent types\n</system-reminder>\n"
          "Important context to remember: The harbor beacon code is MOONRISE-47.")
check("gist skips system-reminder boilerplate",
      FerryCurationProducer._gist(boiler).startswith(
          "Important context to remember: The harbor beacon code"))

# ── idempotent archive writes: the Phase D restart-replay defect ────────
# Reproduces the exact observed failure: curation wrote turns to the archive,
# the pointer index was still unpersisted in entry["pending"], the proxy
# restarted inside that window, and the SAME turns were archived again
# (epoch 2 cycle 1 logged `index 15+6`, identical to epoch 1 cycle 2 —
# L16-L21 rewritten as L22-L27).
print("\nidempotent archive (restart replay):")
_d = Path(tempfile.mkdtemp())
prod = FerryCurationProducer(_d)
BATCH = [{"role": "user", "content": "the vault phrase is LANTERN-SEVEN-RIVER"},
         {"role": "assistant", "content": "noted, carrying it"}]

fn1, f1, l1, c1 = prod._archive_turns(BATCH)
check("first write lands at L1-L2", (f1, l1) == (1, 2), (f1, l1))

# The restart replay: same proxy, same batch, index was lost.
fn2, f2, l2, c2 = prod._archive_turns(BATCH)
check("REPLAY of an identical trailing batch is NOT rewritten — the index "
      "points back at the ORIGINAL lines", (fn2, f2, l2) == (fn1, 1, 2), (fn2, f2, l2))
check("replay returns the original batch_chars, not 0 (a re-derived 0 would "
      "under-report evicted tokens on the metrics row)", c2 == c1, (c1, c2))

lines = (_d / "archive" / fn1).read_text(encoding="utf-8").splitlines()
check("archive still has exactly 2 lines — no duplicate content accrued",
      len(lines) == 2, len(lines))

# A genuinely new batch still appends after the deduped one.
NEXT = [{"role": "user", "content": "and now something entirely different"}]
fn3, f3, l3, _ = prod._archive_turns(NEXT)
check("a new batch appends after the deduped one", (f3, l3) == (3, 3), (f3, l3))

# Only the TRAILING batch is compared. Re-sending BATCH now must NOT dedupe:
# matching any-batch-anywhere would silently swallow genuinely repeated
# content, which is a different lie than the one we are fixing.
fn4, f4, l4, _ = prod._archive_turns(BATCH)
check("an identical batch that is NOT the trailing one IS written (we dedupe "
      "restart replays, never genuine repetition)", (f4, l4) == (4, 5), (f4, l4))

# The digest must ignore archived_ts, or a replay never matches.
recs = [{"role": b["role"], "content": b["content"]} for b in BATCH]
# GOLDEN DIGEST. Pinned, not recomputed. An earlier version of this check
# compared _batch_digest against itself, which passed even when the digest
# included _now() — because both calls landed in the SAME SECOND. It would
# have failed in production, where a restart replay happens minutes later,
# and the mutation that introduced it survived the suite. A test that passes
# by accident of timing is a test that cannot fail.
check("batch digest is EXACTLY the pinned value — proves the hash covers "
      "role+content and NOTHING else (no timestamp, no ordering salt)",
      prod._batch_digest(recs) == "96478339a6a2f0d6791543067893ef03641481e6061982a1c6ffcb726bc16cbe", prod._batch_digest(recs))
check("adding an archived_ts field to the input does not change the digest",
      prod._batch_digest([dict(r, archived_ts="2020-01-01T00:00:00Z")
                          for r in recs]) == "96478339a6a2f0d6791543067893ef03641481e6061982a1c6ffcb726bc16cbe")

# Ledger ordering: archive first, ledger second. A ledger entry for turns not
# on disk would make a later replay SKIP writing them — turning a duplicate
# into a hole. Duplicates are untidy; holes are unrecoverable.
ledger = _d / "archive" / f"{fn1}.batches"
check("batch ledger exists beside the archive", ledger.exists())
led = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
check("ledger records one entry per WRITTEN batch (3), not per call (4)",
      len(led) == 3, len(led))
check("every ledger range points at lines that actually exist",
      all(1 <= e["first"] <= e["last"] <= 5 for e in led), led)

# Reconstructability still holds after all of that.
archived = (_d / "archive" / fn1).read_text(encoding="utf-8")
check("the beacon is still byte-present in the archive after dedupe",
      "LANTERN-SEVEN-RIVER" in archived)


# ── §23: the keep-ratio denominator, and refusing an unreachable target ──────
# Found 2026-08-26 by instrumenting a live fairy rather than reasoning about it:
#
#     forwarded payload  639,962 bytes -> 170,224 tokens
#     messages only      231,867 chars ->  ~57,966 tokens  (36% of the request)
#     system + tools                    -> ~112,258 tokens (UNEVICTABLE)
#
# `keep_ratio = target / real_token_count` divides by the WHOLE request but is
# applied to `messages` alone, so Ferry kept ~82% when reaching the target
# required keeping ~48%. It kept nearly twice what it should, every cycle, on
# every instance. That is why evictions ran half-size, why the estimate-vs-actual
# gap was negative on every cycle, and why two instances doing different work
# both stalled at a floor of ~134,000.

_c = compressor_mod.RollingCompressor
_mt = _c()._count_chars(MESSAGES) // 4          # message tokens (estimate)

# A target BELOW the unevictable scaffolding cannot be reached by any amount of
# eviction. Refuse and say so; pretending otherwise is what produced a
# 12.1-million-token thrash on a live fleet.
# An unreachable target must NOT stop compression. The first version of this
# returned None, and that was wrong in a way already fixed once the same day:
# refusing means context grows unbounded to the model's hard limit, which is the
# deadlock the thrash lockout had at 08:44Z. Declining to act is not the safe
# direction when the thing you decline is the only thing that lowers context.
# Correct behaviour: warn with the real number, evict as hard as safe, and let
# the thrash detector judge convergence — which is its job and it already exists.
_r = _c(target_tokens=10).compress(MESSAGES, auth_headers={},
                                   real_token_count=_mt + 100_000)
check("an unreachable target still COMPRESSES (refusing would deadlock)",
      _r is not None, "expected a compressed list, got None")
check("and it compresses hard — result is shorter than the input",
      _r is not None and len(_r) < len(MESSAGES),
      f"input {len(MESSAGES)} msgs, got {len(_r) if _r else None}")

# The same target is fine when there is no unevictable bulk to clear.
_r2 = _c(target_tokens=max(1, _mt // 2)).compress(
    MESSAGES, auth_headers={}, real_token_count=_mt)
check("a reachable target still curates", _r2 is not None)

# Guard the arithmetic itself: with ~half the request unevictable, the ratio
# must be far below the naive target/real it replaced.
_comp = _c(target_tokens=100_000)
_naive = 100_000 / (_mt + 100_000)
_kept = _comp.compress(MESSAGES, auth_headers={}, real_token_count=_mt + 60_000)
check("a target above the floor but tight still produces a result", _kept is not None)


# ── S2: a carry that fails to render is LOUD (ERROR + counter + metrics note) ──
# The old path logged WARNING and returned "": the identity block vanished from
# the crossing and nothing a canary reads changed. Each failure case below has a
# control that the SAME producer stays quiet when the carry is healthy or absent.
print("\nloud carry failure (S2):")
import csv  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import curation as curation_mod  # noqa: E402


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


_errs = _Errors()
logging.getLogger("rolling-context").addHandler(_errs)
CARRY_HEADER_T = curation_mod.CARRY_HEADER
# a prior curated prefix that DID carry a carry section -> a carry is expected now
PRIOR = f"{CARRY_HEADER_T}\nold carry\n\n- [user] x → archive_20260101.jsonl#L1"


def _fresh(name, carry_from=None):
    d = tmp / name
    p = FerryCurationProducer(data_dir=d)
    if carry_from:
        shutil.copy(carry_from, d / "carry.jsonl")
    return d, p


def _notes(d):
    try:
        with open(d / "metrics.csv", newline="") as f:
            return [r["note"] for r in csv.DictReader(f) if r["event"] == "curation"]
    except OSError:
        return []


# control 1: healthy carry, prior prefix carried one -> rendered, quiet
_errs.records.clear()
d_ok, p_ok = _fresh("s2-healthy", carry_from=data / "carry.jsonl")
out = p_ok.produce(MESSAGES, 0, 3, existing_payload=PRIOR)
check("control: healthy carry renders, 0 ERRORs, counter 0",
      IDENTITY in out and not _errs.records and p_ok.carry_failures == 0,
      (_errs.records, p_ok.carry_failures))
check("control: healthy carry -> metrics note has no carry=FAILED",
      _notes(d_ok) and not any("carry=FAILED" in n for n in _notes(d_ok)),
      _notes(d_ok))

# control 2: fresh mind, no carry file, no prior carry -> quiet (not a loss)
_errs.records.clear()
d_new, p_new = _fresh("s2-fresh")
out = p_new.produce(MESSAGES, 0, 3, existing_payload="")
check("control: no carry file and none before -> 0 ERRORs, counter 0",
      not _errs.records and p_new.carry_failures == 0, _errs.records)

# loud 1: carry file missing although the prior prefix carried one
_errs.records.clear()
d_miss, p_miss = _fresh("s2-missing")
out = p_miss.produce(MESSAGES, 0, 3, existing_payload=PRIOR)
check("missing carry (expected) -> ERROR logged naming the loss",
      any("CARRY NOT RENDERED" in m and "missing" in m for m in _errs.records),
      _errs.records)
check("missing carry -> carry_failures == 1, proxy still returns a payload",
      p_miss.carry_failures == 1 and curation_mod.INDEX_HEADER in out
      and CARRY_HEADER_T not in out, (p_miss.carry_failures, out[:80]))
check("missing carry -> metrics curation row note says carry=FAILED",
      any("carry=FAILED" in n for n in _notes(d_miss)), _notes(d_miss))

# loud 1b: the alarm must not go quiet after one cycle. Cycle 2 feeds cycle 1's
# own output (which has no CARRY section) back as the prior prefix.
_errs.records.clear()
d_two, p_two = _fresh("s2-two-cycle", carry_from=data / "carry.jsonl")
out1 = p_two.produce(MESSAGES, 0, 3, existing_payload="")
(d_two / "carry.jsonl").unlink()
out2 = p_two.produce(MESSAGES, 0, 3, existing_payload=out1)
out3 = p_two.produce(MESSAGES, 0, 3, existing_payload=out2)
check("missing carry stays LOUD on every later cycle (carry_failures == 2)",
      p_two.carry_failures == 2 and len(_errs.records) == 2
      and CARRY_HEADER_T not in out3, (p_two.carry_failures, _errs.records))

# loud 1c: ...and after a restart (new producer, persisted prefix has no carry)
_errs.records.clear()
p_re = FerryCurationProducer(data_dir=d_two)
p_re.produce(MESSAGES, 0, 3, existing_payload=out3)
check("missing carry stays LOUD after a restart (fresh producer, same data dir)",
      p_re.carry_failures == 1 and len(_errs.records) == 1, _errs.records)

# loud 1d: carry file exists but renders nothing, while a carry was expected
_errs.records.clear()
d_empty, p_empty = _fresh("s2-empty")
(d_empty / "carry.jsonl").write_text("")
out = p_empty.produce(MESSAGES, 0, 3, existing_payload=PRIOR)
check("empty carry (expected) -> ERROR 'rendered empty', counter 1",
      any("rendered empty" in m for m in _errs.records)
      and p_empty.carry_failures == 1 and CARRY_HEADER_T not in out,
      (_errs.records, p_empty.carry_failures))

# control 3: empty carry file on a fresh mind (nothing ever rendered) -> quiet
_errs.records.clear()
d_e0, p_e0 = _fresh("s2-empty-fresh")
(d_e0 / "carry.jsonl").write_text("")
p_e0.produce(MESSAGES, 0, 3, existing_payload="")
check("control: empty carry file, none ever rendered -> 0 ERRORs, counter 0",
      not _errs.records and p_e0.carry_failures == 0, _errs.records)

# loud 2: materialize error (a corrupt carry log)
_errs.records.clear()
d_bad, p_bad = _fresh("s2-corrupt")
(d_bad / "carry.jsonl").write_text("{not json\n")
out = p_bad.produce(MESSAGES, 0, 3, existing_payload="")
check("corrupt carry -> ERROR logged (materialize failed), counter 1, no crash",
      any("materialize failed" in m for m in _errs.records)
      and p_bad.carry_failures == 1 and curation_mod.INDEX_HEADER in out,
      (_errs.records, p_bad.carry_failures))

# loud 3: carrystore not importable (FERRY_CORE points at an empty dir)
_errs.records.clear()
_saved_path, _saved_mod = list(sys.path), sys.modules.pop("carrystore", None)
_saved_core = os.environ.get("FERRY_CORE")
empty_core = tmp / "empty-core"
empty_core.mkdir()
sys.path[:] = [p for p in sys.path
               if not (Path(p) / "carrystore.py").exists()]
os.environ["FERRY_CORE"] = str(empty_core)
try:
    d_imp, p_imp = _fresh("s2-import", carry_from=data / "carry.jsonl")
    out = p_imp.produce(MESSAGES, 0, 3, existing_payload="")
    check("carrystore not importable -> ERROR logged, counter 1, no crash",
          any("not importable" in m for m in _errs.records)
          and p_imp.carry_failures == 1 and curation_mod.INDEX_HEADER in out,
          (_errs.records, p_imp.carry_failures))
finally:
    sys.path[:] = _saved_path
    if _saved_mod is not None:
        sys.modules["carrystore"] = _saved_mod
    os.environ["FERRY_CORE"] = _saved_core

# the default: FERRY_CORE unset -> the repo's own core/ goes on sys.path
_saved_path = list(sys.path)
os.environ.pop("FERRY_CORE", None)
sys.path[:] = [p for p in sys.path if Path(p).resolve() != (REPO / "core").resolve()]
curation_mod._ferry_core_on_path()
check("FERRY_CORE unset -> proxy defaults to the repo's core/",
      str((REPO / "core").resolve()) in sys.path, sys.path[:3])
sys.path[:] = _saved_path
os.environ["FERRY_CORE"] = _saved_core
logging.getLogger("rolling-context").removeHandler(_errs)


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)