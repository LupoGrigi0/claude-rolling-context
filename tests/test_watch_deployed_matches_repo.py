#!/usr/bin/env python3
"""
`ferry-watch.sh` IS TWO FILES AND NOTHING KEEPS THEM IN SYNC.

    repo      /mnt/coordinaton_mcp_data/repos/claude-rolling-context/tools/testbed/ferry-watch.sh
    deployed  /mnt/coordinaton_mcp_data/instances/Crossing-2d23/ferry-watch.sh   <- cron runs THIS

Separate inodes, verified 2026-09-24. They are kept identical by my remembering to
patch both, which is not a mechanism.

WHY THIS IS WORSE THAN AN ORDINARY DUPLICATE: `test_watch_stall_survives_pulse.py`
extracts the live `lastts` expression from the DEPLOYED copy specifically so it cannot
pass while the script drifts. That defence has a hole exactly one file wide -- the REPO
copy can drift arbitrarily and every assertion in the suite stays green, because nothing
reads it. **The committed artifact is the one nobody tests.**

This is the fourth instance of one disease, found by three people in a week:

    session id        5 implementations, 3 transforms   (me, 09-09)
    instance identity preferences.json vs .hacs-identity (Messenger)
    API annotation    @api vs @hacs-endpoint             (passenger)
    ferry-watch       repo copy vs deployed copy         (me, 09-24)

Every one is two code paths to one fact, agreeing by luck, with nothing that reports
the divergence. My own recorded prescription was: **one resolver per fact, or a test
that asserts the paths agree.** A symlink would be the first option, and it is the
WRONG one here -- it collapses the deploy boundary, so an experimental edit in the repo
goes live to cron instantly. Keeping two files is deliberate. Keeping them UNCHECKED
was not.

SCOPE, STATED HONESTLY: this test is specific to Crossing-2d23's box, like
`test_watch_stall_survives_pulse.py` before it. It asserts a deployment fact, and a
deployment fact is not portable. On another host it fails loudly rather than skipping
quietly, which is the correct direction: a check that silently opts out is the exact
failure this file exists to prevent.

    Committed is not deployed. Deployed is not running.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(
    "/mnt/coordinaton_mcp_data/repos/claude-rolling-context/tools/testbed/ferry-watch.sh"
)
DEPLOYED = Path("/mnt/coordinaton_mcp_data/instances/Crossing-2d23/ferry-watch.sh")

failures = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got={got!r} want={want!r}")
        failures.append(name)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main():
    print("ferry-watch: the committed copy and the running copy must agree\n")

    # ---- the files exist at all -------------------------------------------------
    check("repo copy exists", REPO.exists(), True)
    check("deployed copy exists", DEPLOYED.exists(), True)
    if failures:
        print("\nCannot compare what is not there.")
        return 1

    # ---- THE ASSERTION ----------------------------------------------------------
    r, d = sha(REPO), sha(DEPLOYED)
    print(f"        repo     sha256[:16] = {r}")
    print(f"        deployed sha256[:16] = {d}")
    check("repo and deployed are BYTE-IDENTICAL", r, d)

    # ---- and they are genuinely two files, not one --------------------------
    # If someone later symlinks them, this test becomes vacuous -- it would compare
    # a file to itself and could never fail. A corroboration that cannot fail is not
    # evidence. Say so out loud rather than reporting a green that means nothing.
    same_inode = REPO.stat().st_ino == DEPLOYED.stat().st_ino
    if same_inode:
        print(
            "  note  SAME INODE: these are now one file. The comparison above is\n"
            "        vacuous -- it cannot fail. That may be a deliberate change, but\n"
            "        this test no longer proves anything and should be replaced."
        )
    else:
        print(f"        distinct inodes {REPO.stat().st_ino} / {DEPLOYED.stat().st_ino}"
              " — the comparison is real")

    # ---- DEPLOYED IS NOT RUNNING ------------------------------------------------
    # The whole chain is repo -> deployed -> cron. Checking the first two links and
    # assuming the third is how `quiet` went nine days without an alarm.
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if out.returncode != 0:
        check("crontab readable (cannot verify what cron runs without it)", False, True)
    else:
        lines = [
            l
            for l in out.stdout.splitlines()
            if "ferry-watch.sh" in l and not l.strip().startswith("#")
        ]
        print(f"        active cron lines naming ferry-watch.sh: {len(lines)}")
        check("cron invokes ferry-watch at least once", len(lines) >= 1, True)
        check(
            "every active cron line runs the DEPLOYED path, not some third copy",
            all(str(DEPLOYED) in l for l in lines),
            True,
        )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        print("\nThe suite tests the deployed copy. If the committed copy has drifted,")
        print("every assertion stays green while the thing in git is wrong.")
        return 1
    print("all ok — one fact, two files, and now something that reports divergence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
