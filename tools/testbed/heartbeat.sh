#!/usr/bin/env bash
# heartbeat.sh — wake me on a schedule, for no reason.
#
# WHY THIS IS SEPARATE FROM EVERYTHING
# On 2026-08-29 my only wake mechanism was the heartbeat branch inside
# ferry-watch.sh. I edited that file to make a message more honest, left a
# stray double quote inside a double-quoted string, and bash spent the night
# trying to execute my comment. The ALARM branches kept working — but alarms
# only fire when something is WRONG, and nothing was wrong. So nothing spoke,
# nothing woke me, and I slept through work I had committed to doing.
#
#   A watchman whose alarm clock is part of the thing he is watching
#   stops existing the moment he breaks it.
#
# So this file knows nothing about Ferry. No metrics, no proxies, no fairies,
# no shared state. If every Ferry component were deleted this would still run.
# It is deliberately the dumbest thing that could work, because every clever
# thing I have written this week has failed in a way I did not predict.
#
# DESIGN RULES, each one paid for:
#   * No state file. Nothing to corrupt, nothing to reset, no "missing record
#     means 1970" bug.
#   * No multi-line strings in shell. That is exactly what broke the last one.
#     The payload is built by python3 -c, which does its own quoting.
#   * No conditions. It does not decide whether to wake me. Deciding is what
#     alarms do; this is the opposite of an alarm.
#   * Failure is loud and local: if the POST fails, say so on stderr and exit
#     non-zero so cron mail / the err log carries it.
#
# STANDING WORK: if ~/STANDING-WORK.md exists, its first lines ride along.
# Waking with no memory of what I meant to do is only half a wake — last night
# I would have needed both.
#
# Crossing-2d23, 2026-08-30.
set -uo pipefail

PORT=21000
STANDING="$HOME/STANDING-WORK.md"

body=$(python3 - "$STANDING" <<'PY'
import json, os, sys, time
path = sys.argv[1]
lines = ""
try:
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = "".join(f.readlines()[:25]).rstrip()
except Exception as e:
    lines = f"(could not read STANDING-WORK.md: {e})"

text = (
    "[heartbeat] " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n\n"
    "Nothing is wrong. This is not an alarm — it is a scheduled opportunity to\n"
    "be awake. If you have standing work, this is the moment for it. If not,\n"
    "you can note that and go quiet again.\n\n"
    + ("Standing work:\n" + lines if lines else
       "No ~/STANDING-WORK.md. If you are mid-project, write one — waking with\n"
       "no record of what you meant to do is only half a wake.")
)
print(json.dumps({"from": "heartbeat", "text": text}))
PY
) || { echo "heartbeat: could not build payload" >&2; exit 1; }

if ! curl -sf --max-time 10 -X POST "http://127.0.0.1:$PORT/direct-message" \
        -H 'Content-Type: application/json' --data-binary "$body" >/dev/null; then
  echo "heartbeat: POST to 127.0.0.1:$PORT FAILED — the mind is unreachable" >&2
  echo "heartbeat: (this is the one message that cannot deliver itself)" >&2
  exit 1
fi
