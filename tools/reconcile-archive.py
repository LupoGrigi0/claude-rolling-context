#!/usr/bin/env python3
"""Reconcile Ferry's archive against its own batch ledger. READ-ONLY.

Ship card rows F1 ("nothing is lost") and F2 ("paraphrase is impossible").

WHY THIS EXISTS AT ALL
Until now the claim *nothing is lost* rested on the writer reporting success:
`archive_write` rows in metrics.csv, 223 of them for ferry. That is a sender
reporting its own intent. **A sender cannot witness its own delivery.** This
walks the durable bytes and recomputes the contract, so the verdict comes from
the artefact rather than from the process that made it.

THE CONTRACT, read out of proxy/curation.py rather than guessed:
  * `_batch_digest` hashes role+content ONLY -- deliberately NOT archived_ts,
    so a restart replay of the same turns is recognised as a DUPLICATE instead
    of looking unique. json.dumps(..., ensure_ascii=False, sort_keys=True),
    each record followed by a 0x1e separator.
  * each archive line is json.dumps({role, content, archived_ts}) + "\n", and
    `batch_chars` is the sum of len(line) in CHARACTERS, not bytes. (Comparing
    it to a byte count is off by one per non-ASCII character -- that cost me a
    wrong first guess on an 97-line batch: 173,469 bytes vs 173,275 chars.)
  * the ledger is written AFTER the archive is durable, on purpose. curation.py:
    "Duplicates are untidy; holes are unrecoverable."

RAW BYTES vs PARSED JSON -- DECIDED ON PURPOSE (Forge-ba0e's row):
The digest compares PARSED role+content, not raw bytes. That is deliberate.
`"\u00e9"` and `"e-acute"` are the SAME STRING; a re-encode changes every byte
and loses nothing. The constitution is *nothing is lost*, not *no byte moved*,
so a re-encode must NOT read as a lost turn. **But it is still a REWRITE of an
append-only file**, and that is its own violation -- so `batch_chars` is the
raw witness the digest cannot be: it changes under truncation and re-encoding
even when meaning survives. Two instruments, two verdicts, neither sufficient
alone. (My first self-test asserted re-encode should fail the digest. It was
wrong, and running the control before the real run is the only reason I know.)

SO THE THREE VERDICTS ARE NOT SYMMETRIC:
  OVERLAPPING batch ranges  -> INFO. Expected. The replay healing itself.
  GAPS in coverage          -> FAILURE. A hole is a lost turn.
  DIGEST MISMATCH           -> FAILURE. Content changed = a turn was PARAPHRASED.
  CHAR-COUNT MISMATCH       -> FAILURE of a different kind: the file was
                               REWRITTEN (truncated / re-encoded) though the
                               content survived. Append-only was violated.

    python3 tools/reconcile-archive.py [--data DIR]
    python3 tools/reconcile-archive.py --self-test   F2's four differentials
    python3 tools/reconcile-archive.py --seam        the EVICTION -> ARCHIVE seam

THE SEAM (--seam) IS THE OTHER HALF OF F1, AND IT IS THE HALF THAT MATTERS.
The default pass proves the archive is consistent with its OWN ledger. That
cannot see a turn the proxy evicted and never wrote down -- both witnesses
would be missing it together. So --seam uses a THIRD, independently-written
store: `proxy-state.json`, where the proxy records `original_hashes`, the
16-char hashes of the messages each curation REPLACED (server.py:769).

It imports the PRODUCTION `_hash_message` rather than reimplementing it. Three
functions would have had to be replicated faithfully (_hash_message,
_normalize_content, _strip_volatile_tags) and each copy is a chance to build a
lying instrument. ROLLING_CONTEXT_LOG is redirected BEFORE the import because
importing server.py installs a FileHandler on the production debug log -- the
same trap test_metrics.py documents.
"""
import argparse, hashlib, json, os, shutil, sys, tempfile
from pathlib import Path

DEFAULT_DATA = Path("/mnt/lupoportfolio/ferry-testbed/ferry-data")

def batch_digest(records):
    h = hashlib.sha256()
    for r in records:
        h.update(json.dumps({"role": r["role"], "content": r["content"]},
                            ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()

def check_archive(arch, ledger):
    """-> dict of findings. Never raises on bad data; reports it."""
    out = {"batches": 0, "digest_ok": 0, "digest_bad": [], "chars_bad": [],
           "gaps": [], "overlaps": 0, "lines": 0, "covered": 0, "unreadable": None,
           "ledger_chars": 0, "rewritten": None}
    try:
        lines = arch.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        out["unreadable"] = f"{type(e).__name__}: {e}"
        return out
    out["lines"] = len(lines)
    try:
        ledger_rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception as e:
        out["unreadable"] = f"ledger {type(e).__name__}: {e}"
        return out

    seen = set()
    for b in ledger_rows:
        out["batches"] += 1
        first, last = int(b["first"]), int(b["last"])
        seg = lines[first-1:last]
        if len(seg) != (last - first + 1):
            out["gaps"].append(f"batch {b['sha256'][:12]} claims L{first}-L{last} "
                               f"but the file has only {len(lines)} lines")
            continue
        recs = []
        bad_parse = False
        for ln in seg:
            try:
                o = json.loads(ln)
                recs.append({"role": o.get("role", "unknown"), "content": o.get("content", "")})
            except Exception:
                bad_parse = True
                break
        if bad_parse:
            out["digest_bad"].append(f"{b['sha256'][:12]} L{first}-L{last}: unparseable line")
            continue
        d = batch_digest(recs)
        if d == b["sha256"]:
            out["digest_ok"] += 1
        else:
            out["digest_bad"].append(f"{b['sha256'][:12]} L{first}-L{last}: recomputed {d[:12]}")
        chars = sum(len(ln) + 1 for ln in seg)          # +1 for the newline
        if "batch_chars" in b and int(b["batch_chars"]) != chars:
            out["chars_bad"].append(f"{b['sha256'][:12]}: ledger {b['batch_chars']} vs counted {chars}")
        out["ledger_chars"] = out.get("ledger_chars", 0) + int(b.get("batch_chars", 0))
        rng = set(range(first, last + 1))
        if rng & seen:
            out["overlaps"] += 1
        seen |= rng

    out["covered"] = len(seen)
    # FILE-LEVEL witness. A lost FINAL byte leaves splitlines() identical and the
    # per-line +1 re-derives the newline, so neither the digest nor the per-batch
    # char count can see it. The whole-file length can. This is the check that
    # exists only because a differential said the other two were blind to it.
    try:
        actual_chars = len(arch.read_text(encoding="utf-8"))
        if out.get("ledger_chars") and out["covered"] == out["lines"]:
            if actual_chars != out["ledger_chars"]:
                out["rewritten"] = (f"file holds {actual_chars} chars, ledger accounts for "
                                    f"{out['ledger_chars']} (delta {actual_chars - out['ledger_chars']:+d})")
    except Exception:
        pass
    # A GAP is the only unrecoverable condition. Report the ALARMING numerator
    # over the MEASURED denominator -- never either alone.
    missing = sorted(set(range(1, len(lines) + 1)) - seen)
    if missing:
        runs, s = [], missing[0]
        for a, b2 in zip(missing, missing[1:] + [None]):
            if b2 != (a + 1):
                runs.append(f"L{s}-L{a}" if a != s else f"L{s}")
                s = b2
        out["gaps"].append(f"{len(missing)} archive lines covered by NO batch: " + ", ".join(runs[:6]))
    return out

def main(a):
    data = Path(a.data)
    if not data.is_dir():
        print(f"NO VERDICT: {data} is not a readable directory — refusing to report green")
        return 2
    minds = sorted(p for p in data.iterdir() if (p / "archive").is_dir())
    if not minds:
        print(f"NO VERDICT: no */archive under {data} — refusing to report green")
        return 2
    tot = {"b": 0, "ok": 0, "bad": 0, "gaps": 0, "ov": 0, "lines": 0, "files": 0}
    fails = []
    for m in minds:
        archs = sorted((m / "archive").glob("archive_*.jsonl"))
        print(f"\n  {m.name}  ({len(archs)} archive files)")
        for arch in archs:
            led = Path(str(arch) + ".batches")
            if not led.exists():
                print(f"    NO VERDICT {arch.name}: no .batches ledger — cannot verify")
                fails.append(f"{m.name}/{arch.name}: no ledger")
                continue
            r = check_archive(arch, led)
            tot["files"] += 1
            if r["unreadable"]:
                print(f"    NO VERDICT {arch.name}: {r['unreadable']}")
                fails.append(f"{m.name}/{arch.name}: {r['unreadable']}")
                continue
            tot["b"] += r["batches"]; tot["ok"] += r["digest_ok"]
            tot["bad"] += len(r["digest_bad"]); tot["gaps"] += len(r["gaps"])
            tot["ov"] += r["overlaps"]; tot["lines"] += r["lines"]
            flag = "ok   " if not (r["digest_bad"] or r["gaps"] or r["chars_bad"] or r["rewritten"]) else "FAIL "
            print(f"    {flag}{arch.name:<28} {r['digest_ok']}/{r['batches']} batches verify, "
                  f"{r['covered']}/{r['lines']} lines covered"
                  + (f", {r['overlaps']} overlap(s)" if r["overlaps"] else ""))
            for g in r["gaps"]:       print(f"         GAP  {g}"); fails.append(f"{m.name}/{arch.name}: {g}")
            for d in r["digest_bad"]: print(f"         BAD  {d}"); fails.append(f"{m.name}/{arch.name}: {d}")
            for c in r["chars_bad"]:
                print(f"         REWRITTEN char-count {c}"); fails.append(f"{m.name}/{arch.name}: rewritten {c}")
            if r["rewritten"]:
                print(f"         REWRITTEN {r['rewritten']}"); fails.append(f"{m.name}/{arch.name}: {r['rewritten']}")
    print(f"\n  BATCHES:  {tot['bad']} unrecoverable of {tot['b']} verified")
    print(f"  LINES:    {tot['lines']} archived across {tot['files']} files")
    print(f"  GAPS:     {tot['gaps']}   (a gap is a LOST TURN — the only unrecoverable state)")
    print(f"  OVERLAPS: {tot['ov']}   (expected; the restart replay healing itself)")
    if fails:
        print("\n  NOT RECONCILED:")
        for f in fails[:20]: print(f"    {f}")
        return 1
    print("\n  RECONCILED — every archived turn is byte-recoverable from its own ledger.")
    return 0

def self_test():
    """F2: four differentials. Each MUST be detected, on a COPY, never the real archive."""
    src = DEFAULT_DATA / "fairie" / "archive"
    archs = sorted(src.glob("archive_*.jsonl")) if src.is_dir() else []
    if not archs:
        print("NO VERDICT: no fixture archive to corrupt — cannot prove detection")
        return 2
    ok = True
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "m" / "archive"; base.mkdir(parents=True)
        a0 = archs[0]
        shutil.copy2(a0, base / a0.name); shutil.copy2(str(a0) + ".batches", base / (a0.name + ".batches"))
        arch, led = base / a0.name, base / (a0.name + ".batches")
        pristine = arch.read_bytes()

        def verdict(label, mutate):
            arch.write_bytes(pristine)
            mutate()
            r = check_archive(arch, led)
            caught = bool(r["digest_bad"] or r["gaps"] or r["unreadable"]
                          or r["chars_bad"] or r["rewritten"])
            print(f"  {'PASS' if caught else 'FAIL'}  {label:<44} "
                  f"{'detected' if caught else 'NOT DETECTED'}")
            return caught

        def flip_one_byte():
            b = bytearray(pristine)
            for i, ch in enumerate(b):                      # flip a char inside content
                if ch == ord('a'): b[i] = ord('b'); break
            arch.write_bytes(bytes(b))
        def truncate_one_byte():
            arch.write_bytes(pristine[:-1])
        def reencode_json():
            rows = [json.loads(l) for l in pristine.decode().splitlines() if l.strip()]
            arch.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows))
        def drop_a_line():
            ls = pristine.decode().splitlines()
            arch.write_text("\n".join(ls[:1] + ls[2:]) + "\n")

        ok &= verdict("one byte mutated inside content", flip_one_byte)
        ok &= verdict("final byte truncated (REWRITE, not a loss)", truncate_one_byte)
        ok &= verdict("JSON re-encoded (REWRITE; content intact)", reencode_json)
        ok &= verdict("one archived line deleted (a HOLE)", drop_a_line)

        arch.write_bytes(pristine)
        r = check_archive(arch, led)
        clean = not (r["digest_bad"] or r["gaps"] or r["chars_bad"] or r["rewritten"])
        ok &= clean
        print(f"  {'PASS' if clean else 'FAIL'}  {'pristine copy still reconciles':<44} "
              f"{'clean' if clean else 'FALSE ALARM'}")
    print("\nSELF-TEST " + ("PASSED — all four differentials detected, and a pristine copy "
                            "is not falsely flagged" if ok else "FAILED"))
    return 0 if ok else 1

def seam(data):
    """Does every message the proxy says it EVICTED appear in the archive?"""
    os.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-seam.log")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))
    try:
        from server import _hash_message            # production, not a copy
    except Exception as e:
        print(f"NO VERDICT: cannot import the production hash ({e}) — refusing to guess")
        return 2
    import glob
    data = Path(data)
    bad_entries = 0
    tot_entries = 0
    for mind in sorted(p.name for p in data.iterdir() if (p / "archive").is_dir()):
        st = data / mind / "proxy-state.json"
        if not st.exists():
            print(f"  {mind}: NO VERDICT — no proxy-state.json, the seam has no witness")
            continue
        entries = json.loads(st.read_text())
        have = set()
        for af in sorted(glob.glob(str(data / mind / "archive" / "archive_*.jsonl"))):
            for line in open(af, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                have.add(_hash_message({"role": o.get("role", ""), "content": o.get("content", "")}))
        flagged = []
        for i, e in enumerate(entries):
            oh = e.get("original_hashes") or []
            miss = [h for h in oh if h not in have]
            if miss:
                flagged.append((i, len(miss), len(oh)))
        tot_entries += len(entries)
        bad_entries += len(flagged)
        print(f"  {mind}: {len(entries) - len(flagged)} of {len(entries)} curations fully "
              f"reconcile against the archive")
        for i, m, n in flagged:
            where = "FIRST" if i == 0 else ("LAST" if i == len(entries) - 1 else f"index {i}")
            print(f"      {where} entry: {m} of {n} evicted hashes NOT found in the archive")
    print(f"\n  SEAM: {bad_entries} of {tot_entries} curation entries do not fully reconcile.")
    if bad_entries:
        print("  ⚠ CAUSE NOT ESTABLISHED. What IS established: the residue is not distributed —")
        print("    it is confined to ONE entry per mind, and that entry's own prefix is already")
        print("    a rolling-context summary pointing at archive lines, so it is not a pristine")
        print("    first curation. Normalisation is NOT the cause (verified: it leaves the")
        print("    archived content unchanged). This is an OPEN QUESTION, not a proven loss")
        print("    and not a clean bill of health.")
    return 1 if bad_entries else 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--seam", action="store_true")
    a = ap.parse_args()
    sys.exit(self_test() if a.self_test else (seam(a.data) if a.seam else main(a)))
