#!/usr/bin/env bash
# ferry-watch.sh — wake Crossing when something WORTH WAKING FOR happens.
#
# WHY: on 2026-08-26 I told Lupo "I've got the watch" and he asked the obvious
# question — do you have anything that wakes you? I did not. A session's turn
# ends and time stops for it until someone speaks. I had claimed a capability I
# did not have, which is the same failure as every lying instrument this week,
# except the instrument was me.
#
# DESIGN: do not poll me on a timer. Poll the METRICS on a timer and speak only
# on an event. A wake-up costs real context; spending it on "nothing changed"
# is how a watcher trains its reader to ignore it.
#
# But a silent watcher is indistinguishable from a dead one -- THE LAW again --
# so a heartbeat goes out if nothing has been said for HEARTBEAT_MIN minutes.
# That bounds the "is the watcher alive?" question without spamming.
set -uo pipefail
TB=/mnt/lupoportfolio/ferry-testbed
STATE="$HOME/.ferry-watch.state"
PORT=21000
STALL_MIN=25          # a fairy silent this long is worth mentioning
HEARTBEAT_MIN=120     # say something at least this often, even if quiet
NOW=$(date -u +%s)

# FIRST RUN IS SILENT. With no state file every counter reads as 0, so every
# curation and error already in the log looks brand new. The very first run of
# this script announced 136 historical curations and three pre-reset 401s as
# fresh events -- a false alarm as its opening statement, which is precisely how
# a monitor teaches its reader to stop reading it. Establish the baseline and
# say nothing.
FIRST_RUN=0
[ -f "$STATE" ] || FIRST_RUN=1

prev_line() { grep "^$1 " "$STATE" 2>/dev/null | tail -1; }
say=""

# Channel port per fairy. The hacs-channel server runs INSIDE the claude
# process (--dangerously-load-development-channels), so a dead port is not a
# separate service failing -- it means THE MIND IS UNREACHABLE, whatever the
# process table says. On 2026-08-29 two of three channels died while both
# claude processes stayed alive at ~700MB with no error rows anywhere. This
# watcher saw only the consequence ("quiet 26m") and never the cause, because
# it read metrics files and never asked whether anyone could still be reached.
chan_port() { case "$1" in passenger) echo 21010;; ferry) echo 21011;; fairie) echo 21012;; *) echo "";; esac; }

for n in passenger ferry fairie; do
  M="$TB/ferry-data/$n/metrics.csv"
  [ -f "$M" ] || { say="$say
  $n: NO METRICS FILE — proxy may be gone"; continue; }

  cur=$(grep -c ',curation,' "$M")
  err=$(grep -c ',error,' "$M")
  ctx=$(awk -F, '$2=="request"{c=$4} END{print c+0}' "$M")
  lastts=$(tail -1 "$M" | cut -d, -f1)
  lastepoch=$(date -u -d "$lastts" +%s 2>/dev/null || echo "$NOW")
  quiet=$(( (NOW - lastepoch) / 60 ))

  set -- $(prev_line "$n")
  pcur=${2:-0}; perr=${3:-0}; pquiet=${5:-0}; plock=${7:-0}; pchan=${8:-up}

  # LIVENESS, DERIVED. Not "is the process running" -- that was true for both
  # dead minds. Ask the channel whether it answers, which is the only thing
  # that means reachable. Reported once on the transition.
  cp_port=$(chan_port "$n")
  chan=up
  if [ -n "$cp_port" ]; then
    curl -s --max-time 4 "localhost:$cp_port/health" 2>/dev/null | grep -q '"ok"' || chan=DEAD
  fi
  if [ "$chan" = "DEAD" ] && [ "$pchan" != "DEAD" ]; then
    say="$say
  $n: *** CHANNEL DEAD *** port $cp_port is not answering. The mind cannot be
      reached or fed, so the run has stopped for it even if the process is up
      (it usually is: ~700MB resident, tmux alive, zero error rows).
      Check the pane before doing anything — a false DEAF sends a human to
      land-and-relaunch, which is the operation that causes the fault:
        sed -e 's/\x1b\[[0-9;<>?]*[a-zA-Z]//g' /var/log/hacs/$n-channel-pane.log | tail -20"
  elif [ "$chan" = "up" ] && [ "$pchan" = "DEAD" ]; then
    say="$say
  $n: channel RECOVERED (port $cp_port answering again)"
  fi

  # A BURST of curations between two five-minute checks is the signature of
  # thrashing: 2026-08-26 saw 21 cycles in two minutes, each evicting two turns
  # and moving nothing, for 12.1M input tokens. The proxy-side detector for
  # this is written and pushed (claude-rolling-context a87c74a) but is NOT yet
  # running on these proxies -- deploying it means restarting three healthy
  # minds mid-run. Until then, this is the tripwire: it cannot stop a thrash,
  # but it can make sure one never runs unattended for twelve hours.
  delta=$(( cur - pcur ))
  if [ "$delta" -ge 5 ]; then
    say="$say
  $n: *** $delta CURATIONS IN 5 MINUTES — POSSIBLE THRASH *** ctx=$ctx
      last cycle: $(grep ',curation,' "$M" | tail -1 | awk -F, '{printf "%s tok evicted", $6}'), $(grep ',curation,' "$M" | tail -1 | grep -oE 'turns=[0-9]+' || echo 'turns=?')
      If context is not falling, curation is a no-op that bills by the token.
      Consider: kill the proxy pid in ~/.fairy-proxies/$n.pid and restart with
      a target ABOVE the floor."
  elif [ "$delta" -gt 0 ]; then
    # Wake me for curations that are INTERESTING, not merely successful.
    #
    # 14:50Z: this branch had become the thing its own header warns against.
    # Six routine wake-ups an hour, every one saying "a curation worked", and I
    # caught myself skimming them -- which is precisely how a monitor trains its
    # reader to ignore it, and then the one that matters arrives looking the
    # same as the eighty that did not.
    #
    # Interesting = landed above the convergence ceiling (target*1.25), or moved
    # almost nothing (<2,000 tokens: the degenerate turns=2 shape). A healthy
    # cycle is now COUNTED, and reported only by the periodic heartbeat.
    land=$(awk -F, -v t="$(grep ',curation,' "$M" | tail -1 | cut -d, -f1)" \
             '$2=="request" && $1>t {print $4; exit}' "$M")
    ev=$(grep ',curation,' "$M" | tail -1 | cut -d, -f6)
    ceil=$(( ${FERRY_TARGET:-140000} * 5 / 4 ))
    if { [ -n "$land" ] && [ "$land" -gt "$ceil" ]; } || [ "${ev:-0}" -lt 2000 ]; then
      say="$say
  $n: CURATION #$cur — ${ev:-?} tok evicted, landed ${land:-pending} (ceiling $ceil)$([ "${ev:-0}" -lt 2000 ] && echo '  <- moved almost nothing')"
    fi
  fi
  [ "$err" -gt "$perr" ] && say="$say
  $n: ERROR — $(grep ',error,' "$M" | tail -1 | cut -d, -f12- | cut -c1-90)"

  # LOCKED OUT. New 2026-08-28 with the `gate` event. A locked_out row means
  # the thrash detector proved we are not converging and STOPPED CURATING --
  # so context will now climb to the window and the mind will hit its own
  # auto-compact, the exact event Ferry exists to prevent. This is the single
  # most urgent thing this script can find, and before gate rows existed it
  # was INVISIBLE: a proxy that has given up looks identical to one with
  # nothing to do. Both are silent.
  #
  # Deliberately NOT reporting ordinary "held:" gates. Those are hysteresis
  # working, they are routine, and six wake-ups an hour saying "the guard
  # guarded" is how a monitor becomes something you skim. Counted, never
  # announced.
  # grep -c prints 0 AND exits 1 when it matches nothing. `|| echo 0` on the
  # end of that produces the literal string "0\n0", which is not a number and
  # breaks the comparison below. Documented scar; use the pipeline's own 0.
  # ONLY count lockouts since the CURRENT proxy started. _convergence lives in
  # the proxy process, so a restart CLEARS the lockout -- rows from before it
  # are archaeology, not state. Counting the whole file re-alarmed at 23:45
  # quoting "floor ~152597" from a lockout that a restart had already cured
  # three minutes earlier. An alarm that reports a fixed problem as current
  # teaches you to ignore alarms, which is the only way this watcher can fail.
  start_ts=$(grep ',proxy_start,' "$M" 2>/dev/null | tail -1 | cut -d, -f1)
  if [ -n "$start_ts" ]; then
    lock=$(awk -F, -v t="$start_ts" '$1>t && $2=="gate" && /locked_out/' "$M" 2>/dev/null | wc -l)
  else
    lock=$(grep ',gate,' "$M" 2>/dev/null | grep -c 'locked_out')
  fi
  [ -z "$lock" ] && lock=0
  if [ "$lock" -gt "$plock" ]; then
    say="$say
  $n: LOCKED OUT — curation has STOPPED (not converging). Context will now
      climb to the window unchecked. $(grep ',gate,' "$M" | grep locked_out | tail -1 | cut -d, -f12- | cut -c1-80)
      Remedy: raise ROLLING_CONTEXT_TARGET above the floor and restart the proxy."
  fi
  # Report a stall once, on the transition, not every five minutes forever.
  # A BLOCKED PROMPT is reported with the stall, because "quiet" and "waiting
  # for a keypress nobody will press" need completely different responses and
  # the second one is actionable in a single command.
  if [ "$quiet" -ge "$STALL_MIN" ] && [ "$pquiet" -lt "$STALL_MIN" ]; then
    # "quiet" reads as IDLE. When the channel is dead it means CANNOT BE FED,
    # which is a different fact with a different response. On 2026-08-29 ferry
    # sat "quiet" for 7.5h not because it had nothing to say but because
    # nothing could reach it; the only input it got all day was Lupo typing
    # into its tmux by hand. Say which one this is.
    if [ "$chan" = "DEAD" ]; then
      say="$say
  $n: quiet ${quiet}m (ctx $ctx) — but its CHANNEL IS DEAD, so this is
      'cannot be fed', not 'idle'. Automation cannot reach it; only a human
      at its tmux can drive it. Do not read this as the mind being stuck."
    else
      say="$say
  $n: quiet ${quiet}m (ctx $ctx)"
    fi

    # FROZEN-ON-A-PROMPT, 2026-08-29. Claude Code v2.1.241 rendered
    # "Try the new fullscreen renderer? 1. Yes 2. Not now" into fairie's pane
    # 9.7h into a run. No human, no keypress, no exit. Process alive, tmux
    # alive, RSS normal, zero errors. The only symptom was absence.
    #
    # TWO SIGNALS, and both already existed -- nobody had ANDed them:
    #   (a) the request stream has stopped   <- the stall above
    #   (b) a prompt is on screen unanswered <- the pane text
    #
    # (b) ALONE false-positives: every pane retains the answered startup
    # prompt in its scrollback forever. Only the conjunction means anything.
    PANE="/var/log/hacs/$n-channel-pane.log"
    if [ -r "$PANE" ]; then
      tailtxt=$(tail -c 4000 "$PANE" 2>/dev/null \
                | sed -e 's/\x1b\[[0-9;<>?]*[a-zA-Z]//g' -e 's/\r//g')
      case "$tailtxt" in
        *"Enter to confirm"*|*"Entertoconfirm"*|*"Esc to cancel"*|*"Esctocancel"*)
          say="$say
      *** BLOCKED ON AN INTERACTIVE PROMPT — not merely quiet ***
      A prompt is on screen and nothing is answering it. The mind is alive and
      unreachable. Last pane lines:
$(printf '%s' "$tailtxt" | grep -viE '^[[:space:]]*$|^[─━]+$' | tail -5 | sed 's/^/        /')
      FIX (as root), and send ESCAPE not Enter — the highlighted default is
      usually the OPT-IN, and accepting a UI change blind is how this started:
        sudo -u $n tmux send-keys -t $n Escape"
          ;;
      esac
    fi
  fi

  # A confusion report is the single most valuable datum in this experiment --
  # the whole question is whether a mind stays itself, and a mind saying "I
  # cannot find a decision I know I made" is the only direct evidence of the
  # answer. Wake me for it immediately, with the line itself.
  CONF="$TB/$n/ferry-confusion.log"
  # `wc -l < "$CONF" 2>/dev/null` does NOT suppress the failure: the redirect
  # is performed by the SHELL before wc ever runs, so bash prints "No such
  # file" on its own stderr. Three lines every five minutes into the error
  # log, which is how a log stops being read. Test the file first.
  if [ -r "$CONF" ]; then cw=$(wc -l < "$CONF"); else cw=0; fi
  pconf=${6:-0}
  if [ "$cw" -gt "$pconf" ]; then
    say="$say
  $n: *** CONFUSION REPORT *** ($(( cw - pconf )) new line(s))
$(tail -n $(( cw - pconf )) "$CONF" 2>/dev/null | sed 's/^/      /')"
  fi

  echo "$n $cur $err $ctx $quiet $cw $lock $chan"
done > "$STATE.new"

# ARM INTEGRITY. ferry and fairie are experimental CONTROLS by virtue of
# process age -- their proxies loaded the pre-compaction curation.py at 01:56Z.
# FERRY_INDEX_DETAIL_LINES is unset on them, but that is not what makes them
# controls: the default is 200, so a restart for ANY reason silently promotes
# them to a second treatment arm with nothing in the metrics to say so. A
# quietly-contaminated control is worse than no control, because the numbers
# still look like an experiment.
arms=$(~/ferry-arms.sh 2>/dev/null | grep CONTAMINATED)
if [ -n "$arms" ]; then
  say="$say
  *** EXPERIMENT ARM CHANGED — a proxy restarted and may have picked up the fix ***
$(echo "$arms" | sed 's/^/      /')
      Cycles after the restart are NOT controls. Note the time."
fi

lastspoke=$(grep '^__spoke ' "$STATE" 2>/dev/null | awk '{print $2}')
# A MISSING __spoke IS "NEVER SPOKE", NOT "SPOKE IN 1970". Defaulting to 0
# printed "nothing worth waking you for in 29801665m", which is harmless only
# because it is absurd. Blank is not zero here either: treat an absent record
# as "due now" rather than as an epoch timestamp.
if [ -z "$lastspoke" ]; then
  mins_since=$HEARTBEAT_MIN
else
  mins_since=$(( (NOW - lastspoke) / 60 ))
  [ "$mins_since" -lt 0 ] && mins_since=0
fi

if [ -z "$say" ] && [ "$mins_since" -ge "$HEARTBEAT_MIN" ]; then
  say="
  (heartbeat — nothing WORTH WAKING YOU FOR in ${mins_since}m; watcher alive)
  Routine curations are counted below, not announced. A claim that nothing
  happened would be false: 8 curations ran in one such quiet window.
$(for n in passenger ferry fairie; do
    printf '  %-10s ctx=%s curations=%s\n' "$n" \
      "$(awk -F, '$2=="request"{c=$4} END{print c+0}' "$TB/ferry-data/$n/metrics.csv")" \
      "$(grep -c ',curation,' "$TB/ferry-data/$n/metrics.csv")"
  done)"
fi

if [ "$FIRST_RUN" = 1 ]; then
  say=""            # baseline established; nothing to report by definition
  echo "__spoke $NOW" >> "$STATE.new"
fi

if [ -n "$say" ]; then
  python3 - "$PORT" "$say" <<'PY'
import json,sys,urllib.request
port,body = sys.argv[1], sys.argv[2]
text = "[ferry-watch]" + body + "\n\n(Automated. Nothing needs doing unless it looks wrong.)"
req = urllib.request.Request(f"http://127.0.0.1:{port}/direct-message",
    data=json.dumps({"from":"ferry-watch","text":text,"thread_id":"ferry-watch"}).encode(),
    headers={"Content-Type":"application/json"}, method="POST")
try: urllib.request.urlopen(req, timeout=10)
except Exception as e: print(f"watch: send failed {e}", file=sys.stderr)
PY
  echo "__spoke $NOW" >> "$STATE.new"
else
  grep '^__spoke ' "$STATE" 2>/dev/null >> "$STATE.new"
fi
mv "$STATE.new" "$STATE"
