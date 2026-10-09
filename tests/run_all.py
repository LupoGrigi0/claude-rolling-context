#!/usr/bin/env python3
"""Ferry test runner — ONE command, exit code only, no mind reads the output.

Ship card rows F11 and F14. Lodestone-8ec9's point: one runner that aggregates
AND treats skip / could-not-run as FAILURE is both rows in a single piece of
work, and without it no evidence column on the card can be re-run by a
reviewer -- so every green is a claim rather than a measurement.

    python3 tests/run_all.py            run everything, exit 0 iff all green
    python3 tests/run_all.py --self-test  prove the runner FAILS on a planted bad test

WHY THE THIRD VERDICT EXISTS HERE AND NOT JUST IN THE PRODUCT
Each suite ends `sys.exit(1 if failed else 0)`, which is correct -- and NOT
sufficient. A file that executes ZERO checks also exits 0. So does a file whose
body was skipped by an early `return`, or whose import failed in a way the
author caught. All three are indistinguishable from "everything passed" to a
runner that only reads exit codes.

Lodestone measured exactly this in his own house: suites printed SKIP and
exited 0, TWENTY skip sites, and an all-skipped live section read as green.
Cairn caught it. The product having the discipline does not give it to the
tests, and the test layer is the one nobody reviews.

So a file is GREEN here only if ALL of:
  * exit code 0
  * a parseable "N passed, M failed" summary line
  * M == 0
  * N > 0                  <-- zero checks executed is NO VERDICT, not a pass
Anything else is FAIL or NO-VERDICT, and both are non-zero overall.

ALLOW-LIST, deliberately empty: a suite permitted to run zero checks must be
named HERE, in the runner, by a human. An empty list means no exemptions exist
today -- and an exemption cannot be granted by the suite itself.
"""
import re, subprocess, sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
TIMEOUT = 300                      # a hang is a failure, not a wait
ZERO_CHECK_ALLOWED = set()         # see ALLOW-LIST above. Keep it empty.

# ENV-BOUND SUITES -- named HERE, by a human, with the reason. A suite may ANNOUNCE
# that it cannot run (printing "SKIP(env): <why>"), but it may NEVER grant itself the
# exemption: only this table does that, and anything skipping while absent from it is
# a FAILURE. Lodestone-8ec9's rule, after his own suites printed SKIP, exited 0, and
# an all-skipped live section read as green across twenty skip sites.
#
# Added 2026-10-09 after he cloned this repo fresh on Windows and measured 16 of 20 /
# 414 checks against my 20 of 20 / 515. Three of the four were not Windows at all --
# they were MY BOX leaking into the suite.
ENV_BOUND = {
    "test_curation.py":
        "imports invariant/carrystore, which are NOT in this repo; set FERRY_CORE",
    "test_watch_stall_survives_pulse.py":
        "asserts a property of the smoothcurves deployment; box-bound by design",
    "test_watch_deployed_matches_repo.py":
        "compares deployed-vs-repo on smoothcurves; box-bound by design",
}
SUMMARY = re.compile(r'(\d+)\s+passed,\s+(\d+)\s+failed')

def run(path):
    """-> (verdict, passed, failed, detail). Never raises."""
    try:
        r = subprocess.run([sys.executable, str(path)], cwd=str(REPO),
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return ("NO-VERDICT", 0, 0, f"timed out after {TIMEOUT}s")
    except Exception as e:                      # could not even launch it
        return ("NO-VERDICT", 0, 0, f"could not run: {type(e).__name__}: {e}")

    out = r.stdout + "\n" + r.stderr
    lines = out.splitlines()
    skip = next((l.split("SKIP(env):", 1)[1].strip() for l in lines if "SKIP(env):" in l), None)
    if skip is not None:
        if path.name in ENV_BOUND:
            return ("SKIP", 0, 0, f"env-bound: {skip}")
        return ("FAIL", 0, 0, f"skipped but NOT on the ENV_BOUND allow-list: {skip}")
    m = None
    for line in reversed(lines):
        m = SUMMARY.search(line)
        if m:
            break
    if not m:
        # SECOND CONVENTION, found by this runner's first real run: four suites
        # report `ok    <desc>` per check and end `all ok -- ...`, with NO COUNT.
        # Do NOT simply accept that tail: "all ok" is a numerator with no
        # denominator and reads identically whether it ran forty checks or none.
        # So DERIVE the count from the evidence instead of trusting a summary --
        # prefer the derivation when one exists. Zero ok-lines stays NO-VERDICT.
        # THREE conventions exist in this suite, found by running it:
        #   1. check()      -> "N passed, M failed"   (16 files)  <- has a denominator
        #   2. ok <desc>    -> "all ok -- ..."        ( 2 files)  <- no denominator
        #   3. PASS <desc>  -> "ALL TESTS PASSED"     ( 2 files)  <- no denominator
        # Conventions 2 and 3 are numerators with no denominator: identical output
        # whether forty checks ran or none. So count the per-check lines OURSELVES.
        # The runner deriving the count is strictly better than the suite asserting
        # it -- a derived count cannot be wrong about itself, and the counter is
        # independent of the thing it measures.
        oks = sum(1 for l in lines if re.match(r'\s*(ok|PASS)\s', l))
        nots = sum(1 for l in lines if re.match(r'\s*(not ok|FAIL|fail)\b', l))
        if oks and not nots and r.returncode == 0:
            return ("GREEN", oks, 0, f"ok-lines counted, not summarised ({oks})")
        if nots or (oks and r.returncode != 0):
            return ("FAIL", oks, nots, f"{nots} not-ok lines, exit {r.returncode}")
    if not m:
        # Exit 0 with no summary is the dangerous case: looks green, proved nothing.
        return ("NO-VERDICT", 0, 0,
                f"no 'N passed, M failed' line (exit {r.returncode}) "
                f"-- cannot distinguish all-passed from nothing-ran")
    p, f = int(m.group(1)), int(m.group(2))
    if r.returncode != 0 or f > 0:
        return ("FAIL", p, f, f"exit {r.returncode}, {f} failed")
    if p == 0 and path.name not in ZERO_CHECK_ALLOWED:
        return ("NO-VERDICT", 0, 0,
                "exit 0 but ZERO checks executed -- not on the allow-list")
    return ("GREEN", p, f, "")

def main(argv):
    if "--self-test" in argv:
        return self_test()
    files = sorted(f for f in TESTS.glob("test_*.py"))
    if not files:
        print("NO-VERDICT: found no test_*.py at all -- refusing to report green")
        return 2
    tot_p = tot_f = 0
    bad = []
    skipped = []
    for f in files:
        v, p, fl, detail = run(f)
        tot_p += p; tot_f += fl
        if v == "SKIP":
            skipped.append((f.name, detail))
        elif v != "GREEN":
            bad.append((f.name, v, detail))
        print(f"  {v:<10} {f.name:<42} {p} passed, {fl} failed"
              + (f"   <- {detail}" if detail else ""))
    print()
    # Report the alarming numerator over the measured denominator -- never either alone.
    green = len(files) - len(bad) - len(skipped)
    # Report the alarming numerator over the measured denominator, and NEVER hide the
    # skips inside the green count -- that is how an all-skipped suite reads as healthy.
    print(f"SUITES: {green} green, {len(skipped)} env-skipped, {len(bad)} NOT green "
          f"— of {len(files)} total")
    print(f"CHECKS: {tot_f} failed of {tot_p + tot_f} executed")
    for name, detail in skipped:
        print(f"  SKIPPED (allow-listed): {name} — {detail}")
    for name, v, detail in bad:
        print(f"  NOT GREEN: {name} [{v}] {detail}")
    return 1 if bad else 0

def self_test():
    """F14's control: a FORCED no-verdict must make this runner FAIL.

    A runner that has never returned non-zero is not a runner. Three planted
    cases, each a way a suite can look green while proving nothing.
    """
    import tempfile, os
    cases = {
        "test_zz_zero_checks.py":  "print('0 passed, 0 failed')\n",            # nothing ran
        "test_zz_no_summary.py":   "print('all good!')\n",                      # no summary line
        "test_zz_real_failure.py": "print('3 passed, 1 failed')\nimport sys; sys.exit(1)\n",
        # The ALLOW-LIST's own control: a suite that announces a skip while absent from
        # ENV_BOUND must FAIL. Without this case the allow-list is an unverified claim,
        # and an unverified allow-list is exactly how twenty skip sites read as green.
        "test_zz_unlisted_skip.py": "print('SKIP(env): I exempted myself')\nprint('0 passed, 0 failed')\n",
    }
    ok = True
    with tempfile.TemporaryDirectory() as d:
        for name, body in cases.items():
            p = Path(d) / name
            p.write_text(body)
            v, _, _, detail = run(p)
            good = v != "GREEN"
            ok &= good
            print(f"  {'PASS' if good else 'FAIL'}  planted {name:<28} -> {v}  {detail}")
        # and the inverse: a well-formed green must still read GREEN
        p = Path(d) / "test_zz_good.py"
        p.write_text("print('2 passed, 0 failed')\n")
        v, _, _, _ = run(p)
        good = v == "GREEN"
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  planted test_zz_good.py"
              f"{'':<17} -> {v}  (a true green must not be flagged)")
    print("\nSELF-TEST " + ("PASSED -- the runner refuses every planted no-verdict shape "
                            "and passes a real green" if ok else "FAILED"))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
