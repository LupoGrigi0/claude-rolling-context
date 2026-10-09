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

# BOX-BOUND: this asserts a property of the smoothcurves deployment and cannot be
# true anywhere else. Lodestone-8ec9 measured it FAIL on a fresh clone -- which is
# a false alarm, not a finding. Report a NAMED could-not-run instead; the decision
# to exempt it lives in tests/run_all.py ENV_BOUND, named by a human, because a
# suite must never grant itself an exemption.
import os as _os
if not _os.path.isdir("/mnt/coordinaton_mcp_data/instances/Crossing-2d23"):
    print("SKIP(env): box-bound to smoothcurves; this deployment path does not exist here")
    print("0 passed, 0 failed")
    raise SystemExit(0)


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

    # ---- 2026-09-24: I FIXED THE CASE AND LEFT THE CLASS ------------------------
    # ac33f99 excluded `alive` and stopped. But `alive` was never the only row the
    # PROXY writes about ITSELF. The real vocabulary, counted from all three fleet
    # CSVs on 2026-09-24 (24,095 request / 12,050 alive / 488 curation / 488
    # archive_write / 35 probe / 27 restart / 27 proxy_start / 18 error / 10 gate /
    # 2 fetch):
    #
    #   THE MIND SPOKE   request  curation  archive_write  fetch  gate  error
    #   THE PROXY LIVED  alive  proxy_start  restart  probe
    #
    # MEASURED on the live data before the fix: ferry's last non-`alive` row was
    # 2026-09-09T19:45:19Z (a `proxy_start`), while its last REQUEST was
    # 2026-09-05T10:41:41Z. **A 4.4-day error in the reported silence of a mind I
    # am protecting.**
    #
    # Today that only misreports. The case where it BLINDS the alarm is a proxy
    # restarting on a period shorter than STALL_MIN: every restart resets `quiet`
    # to 0, so the alarm never fires -- which is EXACTLY the pulse bug again, with
    # a different row type. A recurring row the proxy writes about itself is a
    # clock reset, and any of them will do it.
    #
    # THE FIX IS A WHITELIST, DELIBERATELY. A blacklist fails OPEN: the next event
    # type someone adds counts as the mind speaking and silently suppresses the
    # alarm. A whitelist fails CLOSED: an unknown type is ignored, `quiet` grows,
    # the alarm fires early. One costs a false alarm; the other costs a missed
    # one. Choose the cheap failure.

    restarted = HEADER + "".join(
        [
            "2026-09-05T10:41:41Z,request,,135602,,,,,,claude-haiku-4-5-20251001,,injected,800\n",
            "2026-09-09T19:45:19Z,restart,,,,,,,,,,restored 222 compression(s),801\n",
            "2026-09-09T19:45:19Z,proxy_start,,,,,,,,,,\"mode=ferry, trigger=150000\",802\n",
            "2026-09-09T19:50:19Z,alive,,,,,,,,,,liveness pulse,803\n",
        ]
    )
    check(
        "PROXY RESTART IS NOT THE MIND: ferry's real 09-05 request, not its 09-09 restart",
        quiet_minutes(restarted, expr),
        "2026-09-05T10:41:41Z",
    )

    # The one that actually disables the alarm rather than merely misreporting it.
    loop_rows = ["2026-09-05T10:41:41Z,request,,135602,,,,,,m,,injected,810\n"]
    for i, ts in enumerate(
        ["2026-09-24T03:00:00Z", "2026-09-24T03:45:00Z", "2026-09-24T04:30:00Z"]
    ):
        loop_rows.append(f"{ts},restart,,,,,,,,,,supervisor restart,{820+i}\n")
        loop_rows.append(f"{ts},proxy_start,,,,,,,,,,mode=ferry,{830+i}\n")
    check(
        "RESTART LOOP (period < STALL_MIN): must NOT reset the clock, or the alarm never fires",
        quiet_minutes(HEADER + "".join(loop_rows), expr),
        "2026-09-05T10:41:41Z",
    )

    probed = HEADER + "".join(
        [
            "2026-09-05T10:41:41Z,request,,135602,,,,,,m,,injected,840\n",
            "2026-09-24T05:00:00Z,probe,,,,,,,,m,,non-turn POST /v1/messages/count_tokens,841\n",
        ]
    )
    check(
        "A PROBE IS NOT A TURN: count_tokens is bookkeeping, not the mind speaking",
        quiet_minutes(probed, expr),
        "2026-09-05T10:41:41Z",
    )

    # ---- POSITIVE CONTROLS: the half I skip, every single time ------------------
    # Each of these MUST count. A whitelist that drops a real mind event makes the
    # watcher cry wolf, and a watcher that cries wolf gets ignored -- which is the
    # same outcome as no watcher, reached by a different road.

    fetched = HEADER + "".join(
        [
            "2026-08-31T02:00:00Z,request,,100000,,,,,,m,,injected,850\n",
            "2026-08-31T02:07:31Z,fetch,,,,,,,,,,\"archive_20260831.jsonl#L157-L262\",851\n",
            "2026-08-31T02:10:00Z,alive,,,,,,,,,,liveness pulse,852\n",
        ]
    )
    check(
        "A FETCH IS THE MIND: reaching back for an evicted memory is the realest signal we have",
        quiet_minutes(fetched, expr),
        "2026-08-31T02:07:31Z",
    )

    gated = HEADER + "".join(
        [
            "2026-09-11T17:00:00Z,request,,150000,,,,,,m,,injected,860\n",
            "2026-09-11T18:00:08Z,gate,,150442,,,,,,m,,held: only 2.6s since the last cycle,861\n",
        ]
    )
    check(
        "A GATE IS THE MIND: a suppressed curation means context grew, which means it talked",
        quiet_minutes(gated, expr),
        "2026-09-11T18:00:08Z",
    )

    # passenger's real rows. An `error` means the mind TRIED and upstream refused --
    # evidence it was still working at 14:30, half an hour after its last good turn.
    # TENSION, STATED RATHER THAN HIDDEN: a mind erroring in a tight loop is not
    # silent, so this alarm will not catch it. That is an error-RATE alarm's job and
    # nobody has built one. Counting `error` here is right for "has it gone quiet"
    # and useless for "is it healthy" -- two different questions, and I am only
    # answering the first.
    errored = HEADER + "".join(
        [
            "2026-09-18T14:00:12Z,request,,113342,191,,,,,claude-haiku-4-5-20251001,,injected,870\n",
            "2026-09-18T14:30:03Z,error,,,,,,,,claude-haiku-4-5-20251001,,\"upstream 401; not a turn\",871\n",
            "2026-09-24T06:10:30Z,alive,,,,,,,,,,liveness pulse,872\n",
        ]
    )
    check(
        "AN ERROR IS AN ATTEMPT: passenger was still trying at 14:30, 30m after its last turn",
        quiet_minutes(errored, expr),
        "2026-09-18T14:30:03Z",
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
