#!/usr/bin/env python3
"""
THE LIVENESS PULSE KILLED THE STALL ALARM. This test exists so it cannot again.

2026-09-18. passenger took an upstream 401 at 14:30 and stopped issuing requests
entirely. Feeds kept landing rc=0, its channel stayed up, its mind stayed alive and
sleeping. Two and a half hours later ferry-watch still reported it `up` and had
raised no stall alarm -- and could not have, ever.

    ferry-watch.sh:59   lastts=$(tail -1 "$M" | cut -d, -f1)    # LAST ROW OF ANY KIND
    ferry-watch.sh:61   quiet=$(( (NOW - lastepoch) / 60 ))
    ferry-watch.sh:21   STALL_MIN=50

The liveness pulse (9730ca2, wired 2026-09-09) writes an `alive` row every 300s.
So `quiet` can never exceed 5 on a proxy that is running, and STALL_MIN=50 became
unreachable on every instance in the fleet the day the pulse shipped.

MEASURED at the moment of discovery:

    last row of ANY kind  2026-09-18T16:35:28Z  ->  quiet =   0 min   (what it used)
    last REQUEST          2026-09-18T14:00:12Z  ->  quiet = 156 min   (what it meant)

I learned passenger had stopped from an ERROR row, not from the stall alarm. A mind
that goes quiet WITHOUT erroring would have been invisible indefinitely.

The shape of the mistake is the one I keep making: I added an instrument to detect
silence, tested that the instrument writes rows, and never tested that something
which READS rows still worked afterwards. A fix verified only against its own case
is half-tested -- and so is a feature.

RULE: `quiet` must measure silence of the thing being watched (requests), not
silence of the file. A heartbeat row is evidence the WRITER lived. It is not
evidence the MIND spoke.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

WATCH = Path("/mnt/coordinaton_mcp_data/instances/Crossing-2d23/ferry-watch.sh")

failures = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got={got!r} want={want!r}")
        failures.append(name)


def extract_lastts_expr(src):
    """Pull the REAL lastts line out of the script.

    Extracted, never copied: a test carrying its own duplicate of the expression
    passes happily while the script drifts away from it. That is the same
    two-implementations-one-fact disease documented four times this month.
    """
    m = re.search(r"^\s*lastts=\$\((.+?)\)\s*$", src, re.M)
    return m.group(1) if m else None


def quiet_minutes(csv_text, expr):
    """Run the script's own extraction against a synthetic metrics.csv."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    cmd = expr.replace('"$M"', f'"{path}"').replace("$M", path)
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    return out.stdout.strip()


HEADER = "ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,archive_lines,archive_bytes,carry_chars,model,window,note,seq\n"


def main():
    print("ferry-watch: the stall alarm must survive the liveness pulse\n")

    if not WATCH.exists():
        print(f"FAIL: {WATCH} not found")
        return 1
    src = WATCH.read_text()

    expr = extract_lastts_expr(src)
    check("lastts expression found in the real script", expr is not None, True)
    if expr is None:
        return 1

    # ---- THE REGRESSION CASE ------------------------------------------------
    # A mind that stopped talking at 14:00 on a proxy that is still pulsing.
    stalled = HEADER + "".join(
        [
            "2026-09-18T14:00:12Z,request,,113342,191,,,,,claude-haiku-4-5-20251001,,injected,900\n",
            "2026-09-18T14:05:28Z,alive,,,,,,,,,,liveness pulse; interval=300s; silence is not rest,901\n",
            "2026-09-18T15:05:28Z,alive,,,,,,,,,,liveness pulse; interval=300s; silence is not rest,902\n",
            "2026-09-18T16:35:28Z,alive,,,,,,,,,,liveness pulse; interval=300s; silence is not rest,903\n",
        ]
    )
    got = quiet_minutes(stalled, expr)
    check(
        "STALLED MIND: lastts must be the last REQUEST, not the last alive pulse",
        got,
        "2026-09-18T14:00:12Z",
    )

    # ---- CASES THAT ALREADY WORKED, which is the half I keep skipping -------
    healthy = HEADER + "".join(
        [
            "2026-09-18T16:00:03Z,request,,101528,200,,,,,claude-haiku-4-5-20251001,,injected,910\n",
            "2026-09-18T16:05:28Z,alive,,,,,,,,,,liveness pulse,911\n",
        ]
    )
    check(
        "HEALTHY MIND: still reports its recent request",
        quiet_minutes(healthy, expr),
        "2026-09-18T16:00:03Z",
    )

    long_dark = HEADER + "".join(
        [
            "2026-09-05T10:41:41Z,request,,139421,150,,,,,claude-haiku-4-5-20251001,,injected,500\n",
            "2026-09-18T16:35:20Z,alive,,,,,,,,,,liveness pulse,501\n",
        ]
    )
    check(
        "LONG-DARK MIND (ferry, 13 days): reports the ancient request, not the pulse",
        quiet_minutes(long_dark, expr),
        "2026-09-05T10:41:41Z",
    )

    curation_counts = HEADER + "".join(
        [
            "2026-09-18T12:00:00Z,request,,140000,100,,,,,m,,injected,920\n",
            "2026-09-18T12:00:01Z,curation,179,150426,,,,,,m,,\"estimate:chars/4, turns=98\",921\n",
            "2026-09-18T12:00:02Z,archive_write,179,,,,,,,m,,,922\n",
            "2026-09-18T16:35:28Z,alive,,,,,,,,,,liveness pulse,923\n",
        ]
    )
    check(
        "A CURATION IS ACTIVITY: a real event after the request still counts",
        quiet_minutes(curation_counts, expr),
        "2026-09-18T12:00:02Z",
    )

    only_pulses = HEADER + "2026-09-18T16:35:28Z,alive,,,,,,,,,,liveness pulse,930\n"
    got = quiet_minutes(only_pulses, expr)
    check(
        "NOTHING BUT PULSES: must not report the pulse as activity (empty is correct)",
        got,
        "",
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        print("\nThe stall alarm cannot fire. A watcher that cannot alarm is worse")
        print("than no watcher, because it is trusted.")
        return 1
    print("all ok — quiet measures the mind's silence, not the file's")
    return 0


if __name__ == "__main__":
    sys.exit(main())
