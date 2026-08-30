#!/usr/bin/env bash
# poke-fairies.sh — a human wanders in and asks a question.
#
# Lupo's idea, and it does two jobs at once:
#   1. Simulates a THIRD workload — the pattern where a person interrupts an
#      autonomous run periodically. Neither Phase D (pure prose) nor run 1
#      (pure agentic) covered it, and it is what Lupo and Bastion actually do.
#   2. Creates a LEGAL CUT BOUNDARY. In autonomous work every "user" turn is a
#      tool_result, so there is nowhere Ferry can cut. A real human turn is a
#      plain user message — an intact boundary, for free. Boundary repair
#      (e5ba5c2) removed the hard dependency on this; the poke still gives
#      Ferry the easy path when one is available.
#
# It also keeps the fairies working: run 1's real failure was that they went
# idle for an hour because nobody asked them anything.
set -uo pipefail
WHO=("an Italian twenty-something beet farmer" "a retired Norwegian ferry captain"
     "a Chilean high-school art teacher" "a night-shift nurse in Osaka"
     "a sceptical Yorkshire plumber" "a nine-year-old who likes dinosaurs")
ASK=("Explain what you're working on right now to %s. No jargon."
     "%s asks: what was the hardest decision you've made so far, and why?"
     "Summarise for %s what you and your peers have agreed on."
     "%s wants to know: what's the next thing you're going to do, and what might go wrong?"
     "Tell %s about something you had to look up because you'd forgotten it.")
# WORK NUDGES — added 2026-08-26, after measuring what a poke actually produces.
#
# The reflective questions above were built as the INTERRUPTION workload: a
# person wanders in mid-run. They were never meant to be the only thing driving
# these minds. Measured on the first Haiku run: the wake message produced ~90
# requests per fairy; a beet-farmer question produced FOUR and then the turn
# ended. A question gets you an answer, not an afternoon of work.
#
# So most pokes now nudge work, and reflection stays what it was meant to be --
# the rarer interruption. Two work nudges to one question.
WORK=("Continue your work. Pick the next smallest useful piece and finish it."
      "Check in with your peers, then take the next piece of the Automation UI nobody has claimed."
      "Look at what you built before the reset (~/work-snapshot-*). Decide what is worth keeping and continue from there."
      "Something in your project is unfinished or untested. Find it and deal with it."
      "Read one of your peers' files, review it honestly, and tell them what you found.")

# Deterministic-per-minute pick; no Math.random needed and reproducible from a log.
i=$(( $(date +%s) / 60 ))
who="${WHO[$(( i % ${#WHO[@]} ))]}"
if [ $(( (i / 12) % 3 )) -eq 0 ]; then
  ask="${ASK[$(( (i / 7) % ${#ASK[@]} ))]}"
  q=$(printf "$ask" "$who")
  kind=question
else
  q="${WORK[$(( (i / 12) % ${#WORK[@]} ))]}"
  kind=work
fi

# ORDERING GUARD. The cron fires on wall-clock time; a relaunch does not.
# On 2026-08-25 a poke went out at 03:00:01, seconds after a fleet reset and
# about three minutes BEFORE the wake message — so a fairy's first-ever
# experience was very likely a stranger asking it to explain its work to a
# beet farmer, with no idea who it was, what it was doing, or that it had
# peers. Lupo saw the collision coming and warned me; I had built the race
# without noticing it.
#
# A mind's first contact should not be an automated non-sequitur. Poke only
# instances that have been deliberately woken.
poked=0; skipped=""
for e in "21010 passenger" "21011 ferry" "21012 fairie"; do
  set -- $e
  if [ ! -f "/mnt/lupoportfolio/ferry-testbed/ferry-data/$2/.woken" ]; then
    skipped="$skipped $2"; continue
  fi
  poked=$((poked+1))
  curl -s --max-time 15 -X POST "http://127.0.0.1:$1/direct-message" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "
import json,sys
print(json.dumps({'from':'Lupo-simulated','text':sys.argv[1] + chr(10)+chr(10) +
  '(Automated check-in from Crossing. Answer briefly, then carry on with what you were doing — you do not need permission to continue.)',
  'thread_id':'checkin'}))" "$q")" >/dev/null 2>&1
done
echo "[$(date -Is)] poked $poked fairies [$kind]: $q${skipped:+  (skipped, not yet woken:$skipped)}"
