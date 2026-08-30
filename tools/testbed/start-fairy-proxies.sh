#!/usr/bin/env bash
# start-fairy-proxies.sh — one Ferry proxy per fairy. Run as Crossing-2d23,
# under `sudo openrouter-run --instance Crossing-2d23 -- ...` so the upstream
# key reaches this process and NO FURTHER.
#
# The fairies never receive a credential. Each talks plain HTTP to its own
# proxy on localhost; the proxy injects the real key on the way out
# (FERRY_UPSTREAM_KEY_FILE, 0600, readable only by Crossing-2d23).
set -euo pipefail

TB=/mnt/lupoportfolio/ferry-testbed
FORK="$HOME/repos/claude-rolling-context"
KEYFILE="$HOME/.ferry-upstream-key"
PIDDIR="$HOME/.fairy-proxies"
UPSTREAM="${FAIRY_UPSTREAM:-https://openrouter.ai/api}"
TRIGGER="${FAIRY_TRIGGER:-30000}"
TARGET="${FAIRY_TARGET:-12000}"

# FAIRY_NO_INJECT=1 -> the proxy holds NO key and passes the client's own
# authorization header through untouched (proxy/server.py `_forward_headers`
# guards the whole credential-termination block behind `if key:`).
#
# This is the mode for running the fairies on a Max plan: each already has an
# OAuth blob in ~/.claude/.credentials.json, and OAuth REFRESHES. A copy of a
# refreshing token sitting in a keyfile is the January ProtectHome scar wearing
# a new hat -- it works until it silently stops. Let the client own its own
# credential; keep the proxy out of it.
#
# The keyfile is not merely unused in this mode, it is EMPTIED in the child env:
# a leftover FERRY_UPSTREAM_KEY_FILE pointing at the old OpenRouter key would
# overwrite a live OAuth bearer with a dead one, and the failure would look for
# all the world like a proxy bug.
NO_INJECT="${FAIRY_NO_INJECT:-0}"
SELECT="$*"   # captured before the loop's `set --` clobbers "$@"
KEYFILE_ENV="$KEYFILE"

if [ "$NO_INJECT" = "1" ]; then
  KEYFILE_ENV=""
  echo "credential injection: OFF -- clients authenticate as themselves"
else
  [ -n "${OPENROUTER_API_KEY:-}" ] || {
    echo "OPENROUTER_API_KEY not in env — run me under openrouter-run" >&2
    echo "(or set FAIRY_NO_INJECT=1 to pass client credentials through)" >&2
    exit 1; }
  # Key to a 0600 FILE, not an env var passed onward: /proc/<pid>/environ is
  # readable by the process owner, and env leaks into crash dumps and ps output
  # on some systems. The file never leaves my home, which the group cannot write.
  umask 077
  printf '%s' "$OPENROUTER_API_KEY" > "$KEYFILE"
  chmod 600 "$KEYFILE"
fi
mkdir -p "$PIDDIR"

start() {
  local name="$1" port="$2"
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$port "; then
    echo "  $name: port $port already held — REFUSING to touch it"; return 0
  fi
  # APPEND, never truncate. `> "$log"` wiped proxy.log on every restart, and I
  # restarted proxies five times on 2026-08-26 -- each one destroying the
  # compaction history, the 429 record, and the startup banners that said which
  # code version was loaded. Noticed only because ferry-arms.sh reads that log
  # to decide which experimental arm an instance is in, and passenger silently
  # went from "3 compactions" to "0" across a restart. The evidence a run
  # produces is the run's only lasting output; a start script must not eat it.
  local data="$TB/ferry-data/$name"
  # APPEND (>>), never truncate. `>` wiped proxy.log on EVERY restart, and I
  # restarted proxies five times on 2026-08-26 -- each one destroying the
  # compaction history, the 429 record, and the startup banner saying which code
  # was loaded. Noticed only because ferry-arms.sh reads that log to decide
  # which experimental arm an instance is in, and passenger silently went from
  # "3 compactions" to "0" across a restart. metrics.csv survived because it is
  # append-only. The evidence a run produces is its only lasting output; the
  # start script must not eat it.
  local log="$TB/ferry-data/$name/proxy.log"
  ( cd "$FORK" && \
    FERRY_DATA="$data" \
    FERRY_UPSTREAM_KEY_FILE="$KEYFILE_ENV" \
    FERRY_UPSTREAM_KEY="" \
    ROLLING_CONTEXT_HOME="$data/.rolling-context" \
    ROLLING_CONTEXT_PORT="$port" \
    ROLLING_CONTEXT_CURATION=ferry \
    ROLLING_CONTEXT_TRIGGER="$TRIGGER" \
    ROLLING_CONTEXT_TARGET="$TARGET" \
    ROLLING_CONTEXT_UPSTREAM="$UPSTREAM" \
    setsid nohup python3 proxy/server.py >> "$log" 2>&1 < /dev/null &
    echo $! > "$PIDDIR/$name.pid" )
  # setsid, not just nohup: openrouter-run WAITS for its children, and a
  # proxy that is still a child keeps the wrapper alive forever. Worse, when
  # the wrapper is killed the whole process group dies with it — which is
  # exactly what happened on the first attempt: three healthy proxies logged a
  # clean startup and were then reaped by a SIGTERM meant for their parent.
  # A new session detaches them from that fate.
  sleep 2
  # The pidfile must name the LISTENER, not the subshell. `echo $!` inside
  # ( ... & ) records the subshell, which exits immediately -- so the file
  # names a dead pid the OS is then free to reuse. THIRD sighting of this exact
  # bug (twice 2026-08-25, again 2026-08-26, off by exactly one: 2209174 vs
  # 2209175). A stop that kills nothing looks identical to a stop that worked,
  # and once the number is recycled it kills something else entirely. So
  # resolve the truth from the port, and REFUSE to record any pid whose
  # cmdline is not our server.
  local real; real=$(ss -ltnp 2>/dev/null | grep "127.0.0.1:$port " \
                     | grep -oP 'pid=\K[0-9]+' | head -1)
  if [ -n "$real" ] && tr '\0' ' ' < "/proc/$real/cmdline" 2>/dev/null \
       | grep -q 'proxy/server.py'; then
    echo "$real" > "$PIDDIR/$name.pid"
  fi
  local pid; pid=$(cat "$PIDDIR/$name.pid")
  if kill -0 "$pid" 2>/dev/null && ss -ltn 2>/dev/null | grep -q "127.0.0.1:$port "; then
    echo "  $name: proxy pid $pid on 127.0.0.1:$port  data=$data"
  else
    echo "  $name: FAILED to start — see $log"; tail -3 "$log" 2>/dev/null || true
  fi
}

# Which fairies: all three by default, or exactly the ones named as arguments.
# The named subset exists so a model or upstream switch can be proven on ONE
# mind before three are committed to it -- discovering an auth failure once is
# cheaper than discovering it three-deep into a reset.
echo "trigger=$TRIGGER target=$TARGET upstream=$UPSTREAM inject=$([ "$NO_INJECT" = "1" ] && echo off || echo on)"
# A typo must not look like success. `start-fairy-proxies.sh fariee` would
# otherwise print the header, match nothing, start nothing, and exit 0 --
# a silent no-op wearing a green light. Validate before doing anything.
for want in ${SELECT:-}; do
  case "$want" in
    passenger|ferry|fairie) ;;
    *) echo "REFUSING: unknown fairy '$want' (expected: passenger ferry fairie)" >&2
       exit 2 ;;
  esac
done

for entry in "passenger 5610" "ferry 5611" "fairie 5612"; do
  set -- $entry
  if [ "$#" -eq 0 ]; then continue; fi
  if [ -z "${SELECT:-}" ]; then
    start "$1" "$2"
  else
    case " $SELECT " in *" $1 "*) start "$1" "$2" ;; esac
  fi
done
echo
echo "stop with: kill \$(cat $PIDDIR/*.pid)"
