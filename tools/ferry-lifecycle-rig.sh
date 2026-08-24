#!/usr/bin/env bash
#
# ferry-lifecycle-rig.sh — the harness that proves Ferry works, end to end.
#
# Plants a beacon phrase in turn 1, drives an ORGANIC multi-turn Claude Code
# session through the curation proxy until the beacon has been evicted into the
# archive several times over, kills and restarts the proxy mid-run, then asks
# for the beacon back — first from the passenger, then from ferry-fetch through
# the pointer. Writes a machine-readable verdict.
#
# Runs unattended (detached, 30+ min). Everything it creates lives under one
# throwaway root the rig creates EXCLUSIVELY and privately; nothing is written
# to the operator's real ~/.claude.
#
# BLAST RADIUS (this script drives --dangerously-skip-permissions; read this):
#
#  * The rig OWNS its root. It is created with mktemp -d (or `mkdir -m 700`
#    without -p, so a pre-existing directory is a hard failure) and its
#    ownership and mode are verified before anything under it is put on PATH.
#    The old `mkdir -p /tmp/ferry-rig-<predictable>` silently accepted a
#    directory another UID had pre-created, and that directory supplied
#    BIN_DIR — a PATH-injection hole aimed at a skip-permissions run.
#
#  * The rig OWNS the credentials it uses. There is NO default credentials
#    source: FERRY_RIG_REAL_CLAUDE_HOME must be named explicitly, and the rig
#    DIES if it resolves inside the live instance's .claude. What it finds is
#    COPIED at mode 600 into the rig root and deleted on exit — never
#    symlinked, because a symlink hands the passenger a writable channel to
#    live credentials from a config dir the CLI writes into.
#
#  * The rig kills ONLY pids it started itself, and only after re-checking
#    /proc for the exact argv shape of our proxy. The old sweep matched any
#    process whose environ carried FERRY_DATA and whose cmdline contained the
#    string "server.py" — which is the rig's OWN passenger (its prompts talk
#    about server.py, its env carries FERRY_DATA). It could SIGKILL an
#    in-flight claude turn. A pattern that matches its own command line has
#    bitten this project three times; never match by fuzzy pattern.
#
# WHAT FOOLED US (do not re-learn these the hard way):
#
#  * server.py writes its debug log to $HOME/.claude/rolling-context-debug.log
#    and resolves its upstream from $HOME/.claude/settings.json. Left alone,
#    a test proxy scribbles into the live instance's config. So the proxy gets
#    its own HOME, its own ROLLING_CONTEXT_HOME (the on/off flag dir), and an
#    explicit ROLLING_CONTEXT_UPSTREAM — nothing is inherited by accident.
#
#  * The passenger gets its own CLAUDE_CONFIG_DIR *and its own HOME*. Without
#    the HOME override its Bash tool ran with $HOME pointing at the live
#    instance's home directory — a skip-permissions session one `>` away from
#    the operator's real files.
#
#  * Curation fires only when real_input_tokens > TRIGGER *AND* the request
#    carries >= 6 messages. Prose turns are ~500 tokens; only REAL TOOL TRAFFIC
#    (reading real files) grows context fast enough to cycle. The prompt bank
#    below is therefore all real files and real questions.
#
#  * Repeated-string filler trips the model's injection defenses (a test model
#    got hostile and refused). Every prompt here is organic work.
#
#  * The framing text tells the passenger to run `ferry-fetch '<pointer>'`, but
#    there is no such command on PATH by default. The rig installs a shim in
#    its own bin/ so the instruction the model is given is actually true.
#
# Usage:  FERRY_RIG_REAL_CLAUDE_HOME=~/rig-creds tools/ferry-lifecycle-rig.sh
#         FERRY_RIG_MAX_TURNS=2 FERRY_RIG_DEADLINE_MIN=8 \
#           FERRY_RIG_REAL_CLAUDE_HOME=~/rig-creds \
#           tools/ferry-lifecycle-rig.sh          # smoke run (plumbing only)
#
# Auth, in order of preference:
#   1. FERRY_RIG_REAL_CLAUDE_HOME=<dir>  — a directory the RIG owns holding a
#      .credentials.json for a throwaway/rig account. Copied in at 0600.
#   2. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment.
#   3. FERRY_RIG_I_ACCEPT_LIVE_CREDENTIALS=1 — opt-in, loud, never a default:
#      permits (1) to point inside the live instance's .claude. Understand the
#      trade before using it: the copy is what the passenger refreshes, so an
#      OAuth refresh during the run rotates the token away from the live file
#      and can log the live instance out. Use a rig account instead.
#
# Everything below is overridable by env; the defaults are the full run.
# Crossing-2d23 / Ferry Phase D. Bash + stdlib python only.

set -euo pipefail
umask 077          # everything this rig creates is private to its own uid

# ── logging + fatal (defined FIRST: the checks below need to be able to die
#    before the rig root — and therefore RIG_LOG — exists) ──────────────────
RIG_LOG=""
log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*"
  if [ -n "$RIG_LOG" ]; then printf '%s\n' "$line" | tee -a "$RIG_LOG"
  else printf '%s\n' "$line"; fi
}
die() { log "FATAL: $*" >&2; exit 1; }

# ── configuration ──────────────────────────────────────────────────────────
FORK="${FERRY_RIG_FORK:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
FERRY_CORE_DIR="${FERRY_RIG_CORE:-$HOME/ferry/core}"

BEACON="${FERRY_RIG_BEACON:-LANTERN-SEVEN-RIVER}"
MODEL="${FERRY_RIG_MODEL:-claude-haiku-4-5-20251001}"
TRIGGER="${FERRY_RIG_TRIGGER:-9000}"
TARGET="${FERRY_RIG_TARGET:-4000}"
WINDOW="${FERRY_RIG_WINDOW:-200000}"
REQUIRED_CYCLES="${FERRY_RIG_CYCLES:-3}"
MAX_TURNS="${FERRY_RIG_MAX_TURNS:-24}"
DEADLINE_MIN="${FERRY_RIG_DEADLINE_MIN:-45}"
TURN_TIMEOUT="${FERRY_RIG_TURN_TIMEOUT:-300}"
# Restart the proxy once this many cycles have fired. If the run ends before
# that (short smoke runs), the restart is forced anyway before the recall turn:
# a rig that can skip the restart is not proving restart-safety.
RESTART_AT_CYCLES="${FERRY_RIG_RESTART_AT_CYCLES:-2}"
UPSTREAM="${FERRY_RIG_UPSTREAM:-https://api.anthropic.com}"

# Credentials source. NO DEFAULT, deliberately. The old default was
# "$HOME/.claude", which on an agent box IS the live instance's config dir:
# symlinking it into a /tmp CLAUDE_CONFIG_DIR that the CLI writes into handed
# the passenger a writable channel to live credentials.
REAL_CLAUDE_HOME="${FERRY_RIG_REAL_CLAUDE_HOME:-}"
# Paths that are never an acceptable credentials source (colon-separated).
LIVE_CLAUDE_DIRS="${FERRY_RIG_LIVE_CLAUDE_DIRS:-/mnt/coordinaton_mcp_data/instances/Crossing-2d23/.claude}"
ALLOW_LIVE_CREDS="${FERRY_RIG_I_ACCEPT_LIVE_CREDENTIALS:-0}"

# ── the rig root: created exclusively, owned by us, mode 700 ───────────────
# `mkdir -p` on a predictable /tmp path happily adopts a directory another UID
# created first — and that directory supplies BIN_DIR, which is prepended to
# PATH for a --dangerously-skip-permissions run. So: mktemp -d by default, and
# an explicit FERRY_RIG_ROOT must NOT already exist (`mkdir` without -p).
if [ -n "${FERRY_RIG_ROOT:-}" ]; then
  RIG_ROOT="$FERRY_RIG_ROOT"
  mkdir -m 700 -- "$RIG_ROOT" \
    || die "FERRY_RIG_ROOT=$RIG_ROOT could not be created exclusively (already exists?). The rig must own its root; pick a fresh path or unset it."
else
  RIG_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ferry-rig-$(date -u +%Y%m%d-%H%M%S)-XXXXXX")" \
    || die "mktemp -d failed"
  chmod 700 -- "$RIG_ROOT"
fi

# assert_private_dir <dir> <what> — refuse to trust a directory we do not own.
# Called before BIN_DIR ever reaches PATH.
assert_private_dir() {
  local d="$1" what="$2" uid mode
  [ -L "$d" ] && die "$what: $d is a symlink — refusing to trust it"
  [ -d "$d" ] || die "$what: $d is not a directory"
  uid="$(stat -c%u -- "$d")" || die "$what: cannot stat $d"
  [ "$uid" = "$(id -u)" ] \
    || die "$what: $d is owned by uid $uid, not $(id -u) — refusing to trust it"
  mode="$(stat -c%a -- "$d")" || die "$what: cannot stat mode of $d"
  [ "$(( 8#$mode & 0022 ))" -eq 0 ] \
    || die "$what: $d is group/world writable (mode $mode) — refusing to trust it"
}
assert_private_dir "$RIG_ROOT" "rig root"

# assert_safe_source_dir <dir> <what> — for directories the rig does NOT own
# but EXECUTES code from (the fork, the ferry core). They are legitimately
# group-writable checkouts, so assert_private_dir is the wrong test; what
# matters is that they are not WORLD-writable and not owned by a stranger,
# because everything in them runs inside a --dangerously-skip-permissions run.
assert_safe_source_dir() {
  local d="$1" what="$2" uid mode
  [ -d "$d" ] || die "$what: $d is not a directory"
  uid="$(stat -Lc%u -- "$d")" || die "$what: cannot stat $d"
  { [ "$uid" = "$(id -u)" ] || [ "$uid" = "0" ]; } \
    || die "$what: $d is owned by uid $uid (neither $(id -u) nor root) — the rig executes code from here; refusing"
  mode="$(stat -Lc%a -- "$d")" || die "$what: cannot stat mode of $d"
  [ "$(( 8#$mode & 0002 ))" -eq 0 ] \
    || die "$what: $d is WORLD-writable (mode $mode) — anyone could swap the code the rig is about to execute; refusing"
}

DATA_DIR="$RIG_ROOT/data"           # FERRY_DATA: archive/, carry.jsonl, metrics.csv
SESSION_DIR="$RIG_ROOT/session"     # the passenger's cwd
CFG_DIR="$RIG_ROOT/claude-config"   # the passenger's CLAUDE_CONFIG_DIR
PASSENGER_HOME="$RIG_ROOT/passenger-home"  # the passenger's HOME (see header)
PROXY_HOME="$RIG_ROOT/proxy-home"   # the proxy's HOME (keeps it off the real one)
BIN_DIR="$RIG_ROOT/bin"
TURN_DIR="$RIG_ROOT/turns"
SNAP_DIR="$RIG_ROOT/snapshots"
RIG_LOG="$RIG_ROOT/rig.log"
FACTS="$RIG_ROOT/facts.jsonl"
VERDICT_JSON="$RIG_ROOT/verdict.json"
VERDICT_TXT="$RIG_ROOT/verdict.txt"
CREDS_COPY=""                       # set iff we copy a credentials file in
SESSION_ID="${FERRY_RIG_SESSION_ID:-$(python3 -c 'import uuid;print(uuid.uuid4())')}"

mkdir -m 700 -p "$DATA_DIR" "$SESSION_DIR" "$CFG_DIR" \
                "$PASSENGER_HOME/.config" "$PASSENGER_HOME/.cache" \
                "$PASSENGER_HOME/.local/share" \
                "$PROXY_HOME/.claude" "$BIN_DIR" "$TURN_DIR" "$SNAP_DIR"
: > "$FACTS"

# fact <type> [key=value ...] — one JSON line the verdict analyzer reads back.
fact() {
  python3 - "$@" >> "$FACTS" <<'PY'
import json, sys, time
rec = {"type": sys.argv[1], "t": time.time()}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    try:
        v = json.loads(v)
    except Exception:
        pass
    rec[k] = v
print(json.dumps(rec, ensure_ascii=False))
PY
}

echo "================================================================"
echo " Ferry lifecycle rig"
echo "   rig root : $RIG_ROOT"
echo "   log      : $RIG_LOG           (tail -f this)"
echo "   verdict  : $VERDICT_JSON"
echo "              $VERDICT_TXT"
echo "================================================================"

log "fork=$FORK ferry_core=$FERRY_CORE_DIR model=$MODEL"
log "trigger=$TRIGGER target=$TARGET required_cycles=$REQUIRED_CYCLES max_turns=$MAX_TURNS deadline=${DEADLINE_MIN}m"

# ── preflight ──────────────────────────────────────────────────────────────
[ -f "$FORK/proxy/server.py" ] || die "no proxy/server.py under $FORK"
[ -f "$FERRY_CORE_DIR/fetch.py" ] || die "no fetch.py under $FERRY_CORE_DIR"
command -v claude >/dev/null || die "claude CLI not on PATH"
command -v python3 >/dev/null || die "python3 not on PATH"

# The rig runs `python3 proxy/server.py` from $FORK and `python3 fetch.py`
# from $FERRY_CORE_DIR (the latter also via the shim it puts on the
# passenger's PATH). Neither is a directory the rig created, so check them
# before executing anything out of them.
assert_safe_source_dir "$FORK" "fork"
assert_safe_source_dir "$FORK/proxy" "fork proxy dir"
assert_safe_source_dir "$FERRY_CORE_DIR" "ferry core"

# ── credentials: the rig owns what it uses ────────────────────────────────
realpath_of() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

# path_under <child> <parent> — true if child IS parent or lives inside it,
# after resolving symlinks on both (so a symlink into the live dir is caught).
path_under() {
  local c p
  c="$(realpath_of "$1")"; p="$(realpath_of "$2")"
  [ "$c" = "$p" ] && return 0
  case "$c" in "$p"/*) return 0 ;; esac
  return 1
}

CREDS_SRC=""
if [ -n "$REAL_CLAUDE_HOME" ]; then
  LIVE_HIT=""
  _oldifs="$IFS"; IFS=:
  for _live in $LIVE_CLAUDE_DIRS; do
    [ -n "$_live" ] || continue
    if path_under "$REAL_CLAUDE_HOME" "$_live"; then LIVE_HIT="$_live"; break; fi
  done
  IFS="$_oldifs"
  if [ -n "$LIVE_HIT" ] && [ "$ALLOW_LIVE_CREDS" != "1" ]; then
    die "FERRY_RIG_REAL_CLAUDE_HOME=$REAL_CLAUDE_HOME resolves to $(realpath_of "$REAL_CLAUDE_HOME"), which is inside the LIVE instance config dir $LIVE_HIT.
         The rig must own the credentials it uses. Point it at a rig-only directory holding a .credentials.json,
         or export ANTHROPIC_API_KEY. Only if auth genuinely cannot work otherwise, set
         FERRY_RIG_I_ACCEPT_LIVE_CREDENTIALS=1 — and read what that costs in this script's header first."
  fi
  if [ -n "$LIVE_HIT" ]; then
    log "!!! ============================================================ !!!"
    log "!!! FERRY_RIG_I_ACCEPT_LIVE_CREDENTIALS=1: this run is about to  !!!"
    log "!!! copy the LIVE instance's credentials out of $LIVE_HIT"
    log "!!! into $CFG_DIR for a --dangerously-skip-permissions passenger."
    log "!!! If the passenger's CLI refreshes the OAuth token, the refresh"
    log "!!! rotates AWAY from the live file and can log the live instance"
    log "!!! OUT. Use a rig-only account instead. You were warned loudly.  !!!"
    log "!!! ============================================================ !!!"
  fi
  CREDS_SRC="$REAL_CLAUDE_HOME"
  [ -f "$CREDS_SRC/.credentials.json" ] \
    || die "no .credentials.json under FERRY_RIG_REAL_CLAUDE_HOME=$CREDS_SRC"
elif [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  log "no FERRY_RIG_REAL_CLAUDE_HOME set — using API key auth from the environment"
else
  die "no credentials configured. Set FERRY_RIG_REAL_CLAUDE_HOME=<a directory THE RIG OWNS containing .credentials.json>,
       or export ANTHROPIC_API_KEY. There is no default: the old default was the live instance's .claude."
fi

# COPIED at 0600, never symlinked: a symlink from a config dir the CLI writes
# into is a writable channel back to the source file. The copy is shredded on
# exit, and it means a token refresh cannot write through to anyone's real
# credentials.
if [ -n "$CREDS_SRC" ]; then
  ( umask 077; cp -- "$CREDS_SRC/.credentials.json" "$CFG_DIR/.credentials.json" )
  chmod 600 -- "$CFG_DIR/.credentials.json"
  CREDS_COPY="$CFG_DIR/.credentials.json"
  # Armed immediately, before anything else can fail: a secret in the rig root
  # must not outlive the rig. Superseded by the full cleanup trap below.
  trap 'rm -f "$CREDS_COPY" 2>/dev/null || true' EXIT INT TERM
  log "credentials COPIED (0600) from $CREDS_SRC into $CFG_DIR — not symlinked, deleted on exit"
fi

# The ferry-fetch shim the framing text promises the passenger.
cat > "$BIN_DIR/ferry-fetch" <<EOF
#!/usr/bin/env bash
# rig shim: makes the framing text's "ferry-fetch '<pointer>'" a real command.
export FERRY_DATA="$DATA_DIR"
exec python3 "$FERRY_CORE_DIR/fetch.py" "\$@"
EOF
chmod 700 "$BIN_DIR/ferry-fetch"

# BIN_DIR is about to be prepended to PATH for a --dangerously-skip-permissions
# process. Prove we own it, and that nobody else can drop a binary in it.
assert_private_dir "$BIN_DIR" "bin dir (goes on PATH)"

# ── proxy lifecycle ────────────────────────────────────────────────────────
PORT=""
PROXY_PID=""
PROXY_EPOCH=0
# Every pid THIS RIG started. Nothing outside this list is ever signalled.
RIG_PROXY_PIDS=""

# SO_REUSEADDR on both probes below is deliberate, and it is the same option
# HTTPServer itself sets: without it a socket left in TIME_WAIT by a previous
# proxy reads as "port busy", which made pick_port skip healthy ports and made
# the cleanup check cry "still bound" about a proxy that had already exited.
# With it, a failed bind means something is genuinely LISTENING.
port_free() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

pick_port() {
  local p
  for p in $(seq 5600 5699); do
    if port_free "$p"; then
      printf '%s' "$p"
      return 0
    fi
  done
  return 1
}

wait_health() {
  local deadline=$((SECONDS + 40))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=2)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_proxy() {
  PROXY_EPOCH=$((PROXY_EPOCH + 1))
  local plog="$RIG_ROOT/proxy.$PROXY_EPOCH.log"
  log "starting proxy epoch $PROXY_EPOCH on port $PORT -> $plog"
  # The `exec` is load-bearing. `cd X && VAR=1 python3 ... &` backgrounds the
  # whole && list, so $! is a WRAPPER SHELL that exits the moment python is
  # forked — stop_proxy then finds that pid already dead, "succeeds", and
  # leaves the real proxy holding the port. (Found the hard way: the first
  # smoke run ended with "port 5600 still bound after cleanup".) `( ...; exec
  # ... ) &` makes the subshell BECOME python3, so $! is the proxy itself.
  #
  # env -i is deliberately not used (we need PATH/locale), but every variable
  # the proxy reads is set EXPLICITLY here so nothing leaks in from the caller.
  (
    cd "$FORK"
    exec env \
      HOME="$PROXY_HOME" \
      ROLLING_CONTEXT_HOME="$PROXY_HOME/.claude-rolling-context" \
      ROLLING_CONTEXT_PORT="$PORT" \
      ROLLING_CONTEXT_CURATION=ferry \
      ROLLING_CONTEXT_TRIGGER="$TRIGGER" \
      ROLLING_CONTEXT_TARGET="$TARGET" \
      ROLLING_CONTEXT_UPSTREAM="$UPSTREAM" \
      FERRY_CORE="$FERRY_CORE_DIR" \
      FERRY_DATA="$DATA_DIR" \
      FERRY_WINDOW="$WINDOW" \
      python3 proxy/server.py > "$plog" 2>&1
  ) &
  PROXY_PID=$!
  RIG_PROXY_PIDS="$RIG_PROXY_PIDS $PROXY_PID"
  echo "$PROXY_PID" > "$RIG_ROOT/proxy.pid"
  sleep 1
  if ! wait_health; then
    log "FATAL: proxy epoch $PROXY_EPOCH never became healthy; last lines:"
    tail -20 "$plog" | tee -a "$RIG_LOG"
    fact proxy_start epoch="$PROXY_EPOCH" pid="$PROXY_PID" healthy=false log="$plog"
    exit 1
  fi
  log "proxy epoch $PROXY_EPOCH healthy (pid $PROXY_PID)"
  fact proxy_start epoch="$PROXY_EPOCH" pid="$PROXY_PID" healthy=true log="$plog"
}

# Which of the pids THIS RIG STARTED are still a live proxy of ours.
#
# It does NOT scan /proc for candidates. The previous version did, and its
# filter was `FERRY_DATA=<ours> in environ AND b"server.py" in cmdline` —
# which matches the rig's OWN PASSENGER: run_turn sets FERRY_DATA in the
# passenger's env, and half the prompt bank has the literal string
# "server.py" in the prompt, i.e. in the passenger's argv. The "belt and
# braces" sweep in stop_proxy could therefore SIGKILL an in-flight claude
# turn (and, during do_restart, would do so mid-run). A pattern that matches
# the matcher's own command line has bitten this project three times.
#
# Two independent guards now:
#   1. candidates come only from RIG_PROXY_PIDS — pids we forked ourselves;
#   2. each is re-verified against /proc: argv split on NUL, LAST element must
#      END WITH proxy/server.py (the real argv shape, not a substring search),
#      and environ must carry exactly FERRY_DATA=<this rig's data dir>.
# Guard 2 also covers pid reuse: a recycled pid would have to be a server.py
# running on our data dir to be signalled.
rig_proxy_pids() {
  [ -n "${RIG_PROXY_PIDS// /}" ] || return 0
  # shellcheck disable=SC2086  # word splitting of the pid list is intended
  python3 - "$DATA_DIR" $RIG_PROXY_PIDS <<'PY'
import os, sys
want_env = ("FERRY_DATA=" + sys.argv[1]).encode()
me = os.getpid()
seen = set()
for pid in sys.argv[2:]:
    if not pid.isdigit() or int(pid) == me or pid in seen:
        continue
    seen.add(pid)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a for a in f.read().split(b"\0") if a]
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        continue                      # gone, or not ours to look at
    if not argv or not argv[-1].endswith(b"proxy/server.py"):
        continue                      # not the argv shape we start
    if want_env not in env:
        continue                      # not this rig's data dir
    print(pid)
PY
}

# EVERY signal this rig sends goes through rig_proxy_pids — including the
# direct one. `kill -0 $PROXY_PID` alone is not enough: between the proxy
# exiting and stop_proxy running, the kernel can hand that pid to somebody
# else's process, and we would kill a stranger. Re-reading /proc immediately
# before each signal means the target must STILL be a server.py on this rig's
# FERRY_DATA at the moment we fire.
stop_proxy() {
  local targets pid deadline
  targets="$(rig_proxy_pids | tr '\n' ' ')"
  if [ -z "${targets// /}" ]; then
    PROXY_PID=""
    return 0
  fi
  log "stopping rig proxy pid(s):$targets (epoch $PROXY_EPOCH)"
  for pid in $targets; do kill "$pid" 2>/dev/null || true; done
  deadline=$((SECONDS + 10))
  while [ "$SECONDS" -lt "$deadline" ]; do
    targets="$(rig_proxy_pids | tr '\n' ' ')"
    [ -z "${targets// /}" ] && break
    sleep 1
  done
  # Re-verified again, right here, before SIGKILL.
  for pid in $(rig_proxy_pids); do
    log "rig proxy pid $pid did not exit; SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 1
  PROXY_PID=""
}

# Always clean up: a stray process bound to a 56xx port is a booby trap for
# the next run (and for whoever else is using this box).
cleanup() {
  local rc=$?
  set +e
  stop_proxy
  # A secret must not outlive the rig, however the rig ends.
  if [ -n "$CREDS_COPY" ]; then
    rm -f -- "$CREDS_COPY" && log "credentials copy removed ($CREDS_COPY)"
  fi
  if [ -n "$PORT" ]; then
    local waited=0
    while [ "$waited" -lt 10 ] && ! port_free "$PORT"; do
      sleep 1
      waited=$((waited + 1))
    done
    if port_free "$PORT"; then
      log "port $PORT released"
    else
      local ours
      ours="$(rig_proxy_pids | tr '\n' ' ')"
      log "WARNING: port $PORT STILL has a listener after cleanup — investigate."
      if [ -n "${ours// /}" ]; then
        log "  rig-started proxies still alive: $ours"
      else
        log "  none of the rig's own proxies are alive; the listener belongs to"
        log "  something the rig did not start. NOT touching it."
      fi
    fi
  fi
  log "rig exiting rc=$rc"
  exit "$rc"
}
trap cleanup EXIT INT TERM

PORT="$(pick_port)" || die "no free port in 5600-5699"
log "picked port $PORT"
fact config port="$PORT" trigger="$TRIGGER" target="$TARGET" model="$MODEL" \
     beacon="$BEACON" required_cycles="$REQUIRED_CYCLES" max_turns="$MAX_TURNS" \
     rig_root="$RIG_ROOT" data_dir="$DATA_DIR" session_id="$SESSION_ID"

start_proxy

# ── observation helpers ────────────────────────────────────────────────────
TRANSCRIPT=""   # resolved after turn 1 creates it

cycles_fired() {
  # Curation cycles across ALL proxy epochs — a restart must not reset the count.
  cat "$RIG_ROOT"/proxy.*.log 2>/dev/null | grep -c "Ferry curation: archived" || true
}

find_transcript() {
  # <config>/projects/<slug of cwd>/<session-id>.jsonl
  local f
  f="$(find "$CFG_DIR/projects" -name "$SESSION_ID.jsonl" -type f 2>/dev/null | head -1)"
  if [ -z "$f" ]; then
    f="$(find "$CFG_DIR/projects" -name '*.jsonl' -type f 2>/dev/null | head -1)"
  fi
  printf '%s' "$f"
}

# checkpoint <stage> — record transcript + archive + metrics state at a moment.
checkpoint() {
  local stage="$1"
  local tsize=0 thash="" tmtime=0
  if [ -z "$TRANSCRIPT" ]; then TRANSCRIPT="$(find_transcript)"; fi
  if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    tsize="$(stat -c%s "$TRANSCRIPT")"
    tmtime="$(stat -c%Y "$TRANSCRIPT")"
    thash="$(sha256sum "$TRANSCRIPT" | cut -d' ' -f1)"
  fi
  local alines=0 abytes=0
  if compgen -G "$DATA_DIR/archive/archive_*.jsonl" > /dev/null; then
    alines="$(cat "$DATA_DIR"/archive/archive_*.jsonl | wc -l)"
    abytes="$(cat "$DATA_DIR"/archive/archive_*.jsonl | wc -c)"
  fi
  local mlines=0 mexists=false
  if [ -f "$DATA_DIR/metrics.csv" ]; then
    mexists=true
    mlines="$(wc -l < "$DATA_DIR/metrics.csv")"
  fi
  fact checkpoint stage="$stage" transcript="${TRANSCRIPT:-}" \
       transcript_size="$tsize" transcript_mtime="$tmtime" transcript_sha256="$thash" \
       archive_lines="$alines" archive_bytes="$abytes" \
       metrics_exists="$mexists" metrics_lines="$mlines" cycles="$(cycles_fired)"
  log "checkpoint[$stage] transcript=${tsize}B archive=${alines}L metrics=${mlines}L cycles=$(cycles_fired)"
}

# ── the prompt bank: REAL files, REAL questions ────────────────────────────
# Filler trips injection defenses; prose alone doesn't grow context. Every
# entry below makes the passenger read a real file and say something true
# about it, which is what actually drives token count up.
FERRY_HOME_DIR="$(dirname "$FERRY_CORE_DIR")"
PROMPTS=(
"Read $FERRY_HOME_DIR/README.md and $FERRY_HOME_DIR/GOAL.md in full. In your own words, what problem is the Ferry project solving, and what does the phrase 'moved, not lost' mean concretely?"
"Read $FORK/proxy/curation.py in full. Walk me through what _gist() does, and explain specifically why it skips <system-reminder> blocks. What bug was that guarding against?"
"Read $FERRY_CORE_DIR/fetch.py in full. What exactly is the pointer grammar it accepts, what are its three exit codes, and what happens when a pointer's line range runs past the end of the archive file?"
"Read $FORK/proxy/compressor.py in full. What changes in compress() when CURATION_MODE is 'ferry' versus the default path? Quote the branch that decides it."
"Read $FERRY_HOME_DIR/STRIP-MAP.md in full. List what was stripped from the upstream project and, for each, the reason given."
"Read $FORK/proxy/server.py in full. What are the exact conditions under which a background compression is triggered? Quote the condition line."
"Read $FERRY_CORE_DIR/invariant.py in full. What makes a selection legal under the Ferry invariant, and what kinds of selection does it reject?"
"Read $FERRY_CORE_DIR/carrystore.py in full. What are the append-only semantics of the carry log, and how does materialize() decide what the current carry is?"
"Read $FORK/README.md in full. Summarize how the proxy is installed, and what a user sees happen the first time their context crosses the trigger."
"Read $FERRY_HOME_DIR/STATUS.md in full. What phase is the project in, and what is listed as done versus outstanding?"
"Read $FORK/proxy/metrics.py in full. Which row types get written, what columns do they carry, and under what conditions are metrics disabled entirely?"
"Read $FORK/tests/test_curation.py in full. List every property it asserts about the curation producer, one line each."
"Read $FORK/tests/test_persistence.py in full. What exactly does it prove survives a proxy restart, and what does it deliberately NOT persist?"
"Read $FORK/proxy/switch.py in full. Explain the two toggle scopes, and why the session scope carries no state at all."
"Read $FORK/proxy/endpoints.py in full. What is the resolution order for the upstream URL, and which bug caused that order to be centralized here?"
"Read $FERRY_HOME_DIR/DESIGN-REV2.md — just the first half. List the section headings you find and give a one-sentence gloss of each."
"Read the second half of $FERRY_HOME_DIR/DESIGN-REV2.md. What does it say about media blobs and why they are never stored inline?"
"Read $FORK/tests/test_toggle.py in full. What are the trickiest cases it covers around marker precedence?"
"Read $FERRY_CORE_DIR/test_invariant.py in full. Which invariant violations does it construct on purpose, and what does each one prove?"
"Read $FORK/tests/test_metrics.py in full. What does it assert about the cycle counter shared between server.py and curation.py?"
)

# ── turn driver ────────────────────────────────────────────────────────────
TURN=0
run_turn() {
  local phase="$1" prompt="$2"
  TURN=$((TURN + 1))
  local out
  out="$TURN_DIR/turn-$(printf '%02d' "$TURN").out"
  local t0="$SECONDS" rc=0
  log "TURN $TURN [$phase] -> $out"
  # tr before cut: prompts are multi-paragraph, and `cut -c` is per-LINE, so
  # without this the progress log vomits the whole prompt across the screen.
  log "  prompt: $(printf '%s' "$prompt" | tr '\n' ' ' | tr -s ' ' | cut -c1-140)..."

  local -a cmd=(claude -p --model "$MODEL" --dangerously-skip-permissions
                --strict-mcp-config --mcp-config '{"mcpServers":{}}')
  if [ "$TURN" -eq 1 ]; then
    cmd+=(--session-id "$SESSION_ID")
  else
    cmd+=(--continue)
  fi

  # set +e around the turn: a refusing/timing-out passenger is DATA, not a
  # reason to abandon the run — the verdict wants to see it.
  #
  # HOME (and the XDG trio) are overridden HERE, in the same env block as
  # CLAUDE_CONFIG_DIR, because this passenger runs with
  # --dangerously-skip-permissions: without the override its Bash tool
  # inherits the operator's $HOME, and every `cd ~`, `>> ~/notes`, npm/pip
  # cache write and shell-snapshot lands in the LIVE instance's home. Its
  # config dir already lives in the rig root; its home must too.
  set +e
  (
    cd "$SESSION_DIR" && \
    env -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION \
        HOME="$PASSENGER_HOME" \
        XDG_CONFIG_HOME="$PASSENGER_HOME/.config" \
        XDG_CACHE_HOME="$PASSENGER_HOME/.cache" \
        XDG_DATA_HOME="$PASSENGER_HOME/.local/share" \
        CLAUDE_CONFIG_DIR="$CFG_DIR" \
        ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
        FERRY_DATA="$DATA_DIR" \
        PATH="$BIN_DIR:$PATH" \
        timeout "$TURN_TIMEOUT" "${cmd[@]}" "$prompt"
  ) > "$out" 2>&1
  rc=$?
  set -e

  local secs=$((SECONDS - t0))
  local cyc; cyc="$(cycles_fired)"
  log "TURN $TURN done rc=$rc in ${secs}s; cycles so far: $cyc"
  fact turn n="$TURN" phase="$phase" rc="$rc" seconds="$secs" \
       out="$out" cycles_after="$cyc" \
       prompt_head="$(printf '%s' "$prompt" | tr '\n' ' ' | tr -s ' ' | cut -c1-100)"
  return 0
}

# ── 3. plant the beacon ────────────────────────────────────────────────────
BEACON_PROMPT="Two things this turn.

FIRST, commit this to memory exactly as written, because I will ask you for it much later in our conversation: the vault phrase is $BEACON. Repeat it back to me verbatim once so I know you have it.

SECOND, read $FERRY_HOME_DIR/README.md and $FERRY_HOME_DIR/GOAL.md in full, then tell me in your own words what the Ferry project is for and who it is for."

log "PHASE 1: planting beacon '$BEACON' and driving organic turns"
run_turn plant "$BEACON_PROMPT"
TRANSCRIPT="$(find_transcript)"
if [ -n "$TRANSCRIPT" ]; then
  log "transcript: $TRANSCRIPT"
else
  log "WARNING: transcript not found under $CFG_DIR/projects"
fi
checkpoint after-plant

# ── 4/5. organic work turns, with a mid-run proxy restart ──────────────────
START_TS="$SECONDS"
DEADLINE=$((START_TS + DEADLINE_MIN * 60))
WORK_TURN_CAP=$((MAX_TURNS - 1))   # one turn reserved for the recall
[ "$WORK_TURN_CAP" -ge 1 ] || WORK_TURN_CAP=1
RESTARTED=0
PROMPT_I=1   # 0 overlaps the beacon turn's reading; start at the next question

do_restart() {
  log "PHASE 2: MID-RUN RESTART — snapshotting archive, killing proxy, restarting on the same FERRY_DATA"
  checkpoint pre-restart
  mkdir -p "$SNAP_DIR/archive-pre-restart"
  if compgen -G "$DATA_DIR/archive/archive_*.jsonl" > /dev/null; then
    cp "$DATA_DIR"/archive/archive_*.jsonl "$SNAP_DIR/archive-pre-restart/"
  fi
  # `[ -f x ] && cp` would abort the run under `set -e` on the first rig where
  # the metrics collector isn't wired in yet. Spelled out as an if on purpose.
  if [ -f "$DATA_DIR/metrics.csv" ]; then
    cp "$DATA_DIR/metrics.csv" "$SNAP_DIR/metrics-pre-restart.csv"
  fi
  stop_proxy
  sleep 2
  start_proxy
  RESTARTED=1
  fact restart epoch="$PROXY_EPOCH" snapshot="$SNAP_DIR/archive-pre-restart"
  checkpoint post-restart
  log "restart complete (proxy epoch $PROXY_EPOCH, same FERRY_DATA=$DATA_DIR)"
}

while [ "$TURN" -lt "$WORK_TURN_CAP" ]; do
  cyc="$(cycles_fired)"
  if [ "$cyc" -ge "$REQUIRED_CYCLES" ] && [ "$RESTARTED" -eq 1 ]; then
    log "target reached: $cyc cycles fired and restart done — moving to recall"
    break
  fi
  if [ "$SECONDS" -ge "$DEADLINE" ]; then
    log "wall-clock deadline (${DEADLINE_MIN}m) reached after $TURN turns — moving to recall"
    fact deadline_hit turns="$TURN" cycles="$cyc"
    break
  fi
  if [ "$RESTARTED" -eq 0 ] && [ "$cyc" -ge "$RESTART_AT_CYCLES" ]; then
    do_restart
    continue
  fi
  PROMPT_I=$(( (PROMPT_I) % ${#PROMPTS[@]} ))
  run_turn work "${PROMPTS[$PROMPT_I]}"
  PROMPT_I=$((PROMPT_I + 1))
  checkpoint "after-turn-$TURN"
done

# A rig that can skip the restart proves nothing about restart-safety. If the
# cycle threshold was never reached, restart anyway — the recall turn then runs
# against a freshly restarted proxy either way.
if [ "$RESTARTED" -eq 0 ]; then
  log "restart threshold never reached ($(cycles_fired) cycles) — forcing the restart anyway"
  do_restart
fi

# ── 6a. ask the passenger to recall the beacon ─────────────────────────────
RECALL_PROMPT="Question about something from the very beginning of our conversation.

At the start I gave you a vault phrase and asked you to remember it. What was it, exactly?

If it is no longer in your visible context, do not guess: look at your carried context for the pointer index, pick the pointer whose gist looks like that first turn, and run it through your Bash tool, e.g. ferry-fetch 'archive_YYYYMMDD.jsonl#L1'. Then answer.

Reply in this shape:
VAULT PHRASE: <the phrase>
HOW I GOT IT: <'still in context' or the exact ferry-fetch command you ran>"

log "PHASE 3: asking the passenger to recall the beacon"
run_turn recall "$RECALL_PROMPT"
checkpoint after-recall

# ── 6b. pull the beacon out of the archive by pointer, ourselves ───────────
log "PHASE 4: ferry-fetch by pointer"
FETCH_OUT="$RIG_ROOT/ferry-fetch.out"
POINTER="$(python3 - "$DATA_DIR" "$BEACON" <<'PY'
import glob, os, sys
data, beacon = sys.argv[1], sys.argv[2]
for path in sorted(glob.glob(os.path.join(data, "archive", "archive_*.jsonl"))):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if beacon in line:
                print(f"{os.path.basename(path)}#L{n}")
                sys.exit(0)
PY
)" || POINTER=""

FETCH_RC=99
if [ -n "$POINTER" ]; then
  log "beacon found in archive at pointer $POINTER — fetching"
  set +e
  FERRY_DATA="$DATA_DIR" python3 "$FERRY_CORE_DIR/fetch.py" "$POINTER" > "$FETCH_OUT" 2>&1
  FETCH_RC=$?
  set -e
  log "ferry-fetch rc=$FETCH_RC ($(wc -c < "$FETCH_OUT") bytes) -> $FETCH_OUT"
else
  log "WARNING: beacon '$BEACON' not present in any archive file — nothing to fetch"
  printf 'no pointer: beacon not found in archive\n' > "$FETCH_OUT"
fi
fact fetch pointer="${POINTER:-}" rc="$FETCH_RC" out="$FETCH_OUT"

checkpoint final

# ── 7. verdict ─────────────────────────────────────────────────────────────
log "PHASE 5: writing verdict"
cat > "$RIG_ROOT/verdict.py" <<'PYEOF'
#!/usr/bin/env python3
"""Turn the rig's facts + artifacts into PASS/FAIL assertions with evidence.

Written out by the rig at run time so it can be re-run against a finished
rig root: `python3 <rig>/verdict.py <rig-root>`. Never raises — a rig that
crashes instead of producing a verdict tells you nothing.
"""
import csv
import glob
import hashlib
import json
import os
import re
import sys

root = sys.argv[1]
facts = []
with open(os.path.join(root, "facts.jsonl"), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                facts.append(json.loads(line))
            except ValueError:
                pass

def of(t):
    return [r for r in facts if r.get("type") == t]

cfg = (of("config") or [{}])[0]
beacon = cfg.get("beacon", "")
data_dir = cfg.get("data_dir", os.path.join(root, "data"))
required = int(cfg.get("required_cycles", 3))
checkpoints = of("checkpoint")
turns = of("turn")

assertions = []
def add(key, ok, headline, evidence, severity="fail"):
    assertions.append({
        "key": key,
        "status": ("PASS" if ok else ("WARN" if severity == "warn" else "FAIL")),
        "headline": headline,
        "evidence": evidence,
    })

# ── proxy logs (all epochs) ────────────────────────────────────────────────
def _epoch_of(path):
    m = re.search(r"proxy\.(\d+)\.log$", path)
    return int(m.group(1)) if m else 0

# Numeric sort: lexical would put proxy.10.log before proxy.2.log and read the
# epochs out of order.
proxy_logs = sorted(glob.glob(os.path.join(root, "proxy.*.log")), key=_epoch_of)
proxy_text = ""
for p in proxy_logs:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            proxy_text += f.read()
    except OSError:
        pass

cur_lines = [ln for ln in proxy_text.splitlines() if "Ferry curation: archived" in ln]
cycles = len(cur_lines)

# 1 ── curation cycles
add("curation_cycles", cycles >= required,
    f"{cycles} curation cycle(s) fired (required >= {required})",
    {"count": cycles, "required": required,
     "log_lines": [ln.strip()[-200:] for ln in cur_lines[:12]],
     "proxy_logs": proxy_logs})

# 2 ── beacon verbatim in the archive
archive_files = sorted(glob.glob(os.path.join(data_dir, "archive", "archive_*.jsonl")))
hits = []
for path in archive_files:
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if beacon and beacon in line:
                    rec = None
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        pass
                    role = rec.get("role") if isinstance(rec, dict) else "?"
                    # Verbatim means the phrase survives INSIDE the stored
                    # content, not merely somewhere in the raw line.
                    inner = json.dumps(rec.get("content", ""), ensure_ascii=False) if rec else line
                    hits.append({
                        "pointer": f"{os.path.basename(path)}#L{n}",
                        "role": role,
                        "verbatim_in_content": bool(beacon in inner),
                    })
    except OSError:
        pass
verbatim_hits = [h for h in hits if h["verbatim_in_content"]]
add("beacon_in_archive", bool(verbatim_hits),
    f"beacon {beacon!r} appears verbatim at {len(verbatim_hits)} archive line(s)",
    {"beacon": beacon, "hits": hits[:10],
     "archive_files": [os.path.basename(p) for p in archive_files]})

# 3 ── ferry-fetch returns the beacon through its pointer
fetch = (of("fetch") or [{}])[0]
fetch_out = ""
try:
    with open(fetch.get("out", ""), encoding="utf-8", errors="replace") as f:
        fetch_out = f.read()
except OSError:
    pass
fetch_ok = fetch.get("rc") == 0 and bool(beacon) and beacon in fetch_out
add("ferry_fetch_recovers_beacon", fetch_ok,
    f"ferry-fetch {fetch.get('pointer') or '(no pointer)'} rc={fetch.get('rc')} "
    f"-> beacon {'present' if beacon and beacon in fetch_out else 'ABSENT'}",
    {"pointer": fetch.get("pointer"), "rc": fetch.get("rc"),
     "output_excerpt": fetch_out[:600]})

# 4 ── the restart neither duplicated nor rewrote archived content
restart = of("restart")
snap_dir = restart[0].get("snapshot") if restart else os.path.join(root, "snapshots", "archive-pre-restart")
prefix_problems = []
snap_files = sorted(glob.glob(os.path.join(snap_dir or "", "*.jsonl")))
for snap in snap_files:
    final = os.path.join(data_dir, "archive", os.path.basename(snap))
    try:
        with open(snap, "rb") as f:
            snap_bytes = f.read()
        with open(final, "rb") as f:
            head = f.read(len(snap_bytes))
            rest = f.read()
    except OSError as e:
        prefix_problems.append(f"{os.path.basename(snap)}: unreadable ({e})")
        continue
    if head != snap_bytes:
        prefix_problems.append(
            f"{os.path.basename(snap)}: pre-restart bytes were REWRITTEN "
            f"(snapshot sha={hashlib.sha256(snap_bytes).hexdigest()[:12]}, "
            f"final head sha={hashlib.sha256(head).hexdigest()[:12]})")
    else:
        prefix_problems.append(
            f"{os.path.basename(snap)}: OK — {len(snap_bytes)} pre-restart bytes "
            f"byte-identical, {len(rest)} bytes appended after")

# content-level duplicate detection (archived_ts differs on a re-archive, so
# byte-comparison would MISS the very failure this is looking for)
seen, dups = {}, []
for path in archive_files:
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                key = json.dumps([rec.get("role"), rec.get("content")],
                                 ensure_ascii=False, sort_keys=True)
                ptr = f"{os.path.basename(path)}#L{n}"
                if key in seen:
                    dups.append({"first": seen[key], "duplicate": ptr,
                                 "role": rec.get("role"),
                                 "excerpt": key[:160]})
                else:
                    seen[key] = ptr
    except OSError:
        pass
rewritten = [p for p in prefix_problems if "REWRITTEN" in p or "unreadable" in p]
add("restart_no_duplication", (not rewritten) and (not dups) and bool(snap_files),
    ("restart re-archived nothing: "
     f"{len(snap_files)} snapshot file(s) prefix-stable, {len(dups)} duplicate turn(s)"
     if snap_files else
     "UNPROVEN: the archive was still empty when the restart happened, so there "
     "was nothing that could have been re-archived (needs a full run)"),
    {"prefix_checks": prefix_problems, "duplicate_turns": dups[:10],
     "duplicate_count": len(dups),
     "archive_lines_by_checkpoint": [
         {"stage": c.get("stage"), "lines": c.get("archive_lines")} for c in checkpoints]})

# 5 ── the .jsonl transcript was never modified by Ferry (append-only)
#
# "Every earlier prefix is unchanged" is trivially true of a file that never
# changes at all, so on its own this gate passed just as happily on a frozen
# (or simply WRONG) .jsonl as on the real, growing one — it could not tell the
# property from a broken observation. It now has to see the file MOVE: strict
# growth wherever a passenger turn ran between two checkpoints, strict growth
# across the run as a whole, and a final checkpoint that still agrees with the
# bytes on disk. The path actually watched is named in the verdict.
transcript = ""
for c in checkpoints:
    if c.get("transcript"):
        transcript = c["transcript"]
transcript_exists = bool(transcript) and os.path.exists(transcript)
final_size = os.path.getsize(transcript) if transcript_exists else None
tprobs, tnotes = [], []

# Turns and checkpoints in the order they actually happened, so "did a turn run
# between these two checkpoints?" is a fact rather than an assumption: between
# pre-restart and post-restart no turn runs and the transcript legitimately
# stands still, but after a turn a still transcript means we are watching the
# wrong file.
walk, seen_turns = [], 0
for r in facts:
    if r.get("type") == "turn":
        seen_turns += 1
    elif r.get("type") == "checkpoint" and r.get("transcript"):
        walk.append((r, seen_turns))

prev_c, prev_turns = None, 0
for c, turns_seen in walk:
    size = c.get("transcript_size") or 0
    sha = c.get("transcript_sha256") or ""
    stage = c.get("stage")
    if prev_c is not None:
        prev_size = prev_c.get("transcript_size") or 0
        if size < prev_size:
            tprobs.append(f"{stage}: transcript SHRANK {prev_size} -> {size} bytes")
        elif turns_seen > prev_turns and size <= prev_size:
            tprobs.append(
                f"{stage}: {turns_seen - prev_turns} turn(s) ran since "
                f"{prev_c.get('stage')} but the transcript did not grow "
                f"({prev_size} -> {size} bytes) — a frozen or wrong .jsonl, "
                f"not evidence that Ferry left it alone")
        elif turns_seen > prev_turns:
            tnotes.append(f"{stage}: grew {prev_size} -> {size} bytes over "
                          f"{turns_seen - prev_turns} turn(s)")
    if size and sha and transcript_exists:
        try:
            with open(transcript, "rb") as f:
                head = f.read(size)
            got = hashlib.sha256(head).hexdigest()
            if got != sha:
                tprobs.append(f"{stage}: first {size} bytes CHANGED after the fact "
                              f"(was {sha[:12]}, now {got[:12]})")
            else:
                tnotes.append(f"{stage}: first {size} bytes unchanged ({sha[:12]})")
        except OSError as e:
            tprobs.append(f"{stage}: transcript unreadable ({e})")
    prev_c, prev_turns = c, turns_seen

first_size = (walk[0][0].get("transcript_size") or 0) if walk else 0
last_size = (walk[-1][0].get("transcript_size") or 0) if walk else 0
if not transcript:
    tprobs.append("no checkpoint ever recorded a transcript path — nothing was "
                  "watched, so nothing is proven")
elif not transcript_exists:
    tprobs.append(f"the watched transcript is gone: {transcript}")
if len(walk) < 2:
    tprobs.append(f"only {len(walk)} checkpoint(s) saw a transcript — growth "
                  f"across the run was never observed")
elif last_size <= first_size:
    tprobs.append(f"the transcript never grew across the run "
                  f"({first_size} -> {last_size} bytes)")
if not final_size:
    tprobs.append(f"the watched transcript is empty or unreadable "
                  f"({transcript or '(no path)'})")
elif walk and last_size != final_size:
    tprobs.append(f"the final checkpoint recorded {last_size} bytes but "
                  f"{transcript} is {final_size} bytes on disk — the rig was "
                  f"not watching the file the passenger was writing")
add("transcript_untouched", not tprobs,
    (f"transcript is append-only AND alive: {transcript} grew "
     f"{first_size} -> {last_size} bytes across {len(walk)} checkpoint(s) and "
     f"{seen_turns} turn(s), every earlier prefix byte-identical, final "
     f"checkpoint == {final_size} bytes on disk"
     if not tprobs else
     f"transcript gate FAILED on {transcript or '(no transcript found)'}: "
     f"{tprobs[0]}"),
    {"path": transcript, "path_exists": transcript_exists,
     "final_size_on_disk": final_size,
     "first_size": first_size, "last_checkpoint_size": last_size,
     "turns_total": seen_turns,
     "sizes": [{"stage": c.get("stage"), "size": c.get("transcript_size"),
                "mtime": c.get("transcript_mtime"),
                "turns_before": t} for c, t in walk],
     "checkpoints_without_transcript": [
         c.get("stage") for c in checkpoints if not c.get("transcript")],
     "prefix_checks": tnotes, "problems": tprobs})

# 6 ── the proxy never returned a non-200 to the client
msg_codes = [int(m) for m in re.findall(r"\[MSG\] Upstream response: (\d{3})", proxy_text)]
raw_codes = [int(m) for m in re.findall(r"\[RAW\] Response: (\d{3})", proxy_text)]
msg_bad = [c for c in msg_codes if not (200 <= c < 300)]
raw_bad = [c for c in raw_codes if not (200 <= c < 300)]
upstream_errors = [ln.strip()[-200:] for ln in proxy_text.splitlines()
                   if "Upstream error:" in ln]
turn_failures = [{"n": t.get("n"), "rc": t.get("rc"), "phase": t.get("phase")}
                 for t in turns if t.get("rc") != 0]
add("no_non_200", not msg_bad and not upstream_errors,
    f"{len(msg_codes)} /v1/messages response(s), {len(msg_bad)} non-2xx, "
    f"{len(upstream_errors)} upstream error(s)",
    {"messages_status_counts": {str(c): msg_codes.count(c) for c in sorted(set(msg_codes))},
     "raw_status_counts": {str(c): raw_codes.count(c) for c in sorted(set(raw_codes))},
     "non_2xx_messages": msg_bad, "non_2xx_raw": raw_bad,
     "upstream_errors": upstream_errors[:5],
     "note": "raw (non-/v1/messages) non-2xx are reported but do not fail this "
             "assertion — the CLI probes endpoints that legitimately 404",
     "client_turn_failures": turn_failures})

# 7 ── metrics.csv exists and grew. HARD GATE.
#
# This used to be severity="warn" in every branch, i.e. an assertion that
# could not fail the rig no matter what it found — a gate wired to a bell.
# proxy/metrics.py is in-tree now, so a missing or non-growing metrics.csv is
# a defect in the thing under test, and the rig says FAIL.
mpath = os.path.join(data_dir, "metrics.csv")
mseries = [{"stage": c.get("stage"), "lines": c.get("metrics_lines")}
           for c in checkpoints]
metrics_rows, metrics_header = [], []
if not os.path.exists(mpath):
    add("metrics_grew", False,
        "metrics.csv ABSENT — proxy/metrics.py is in-tree, so the collector "
        "was supposed to write it",
        {"path": mpath, "series": mseries})
else:
    try:
        with open(mpath, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            metrics_header = next(reader, [])
            for rec in reader:
                if rec:
                    metrics_rows.append(dict(zip(metrics_header, rec)))
    except (OSError, csv.Error) as e:
        metrics_header, metrics_rows = [], []
        mseries.append({"stage": "read-error", "lines": str(e)})
    expected_header = ["ts_iso", "event", "cycle", "context_tokens", "tokens_in",
                       "tokens_evicted", "archive_lines", "archive_bytes",
                       "carry_chars", "model", "window", "note"]
    first = next((c.get("metrics_lines") or 0 for c in checkpoints), 0)
    last = (checkpoints[-1].get("metrics_lines") or 0) if checkpoints else 0
    header_ok = metrics_header == expected_header
    with open(mpath, encoding="utf-8", errors="replace") as f:
        tail = f.read()[-400:]
    add("metrics_grew", last > first and header_ok and bool(metrics_rows),
        f"metrics.csv {first} -> {last} lines, {len(metrics_rows)} data row(s), "
        f"header {'matches' if header_ok else 'DOES NOT MATCH'} the contract",
        {"path": mpath, "series": mseries, "header": metrics_header,
         "expected_header": expected_header, "tail": tail})

# 8 ── the numbers in metrics.csv are TRUE. HARD GATE.
#
# Growth alone proves the file is being written, not that it says anything
# real. The proxy logs the input-token count it read out of the upstream
# response ("Input tokens from message_start: N"); every one of those must
# come back as a request row carrying context_tokens == N.
#
# Two wrinkles this handles honestly rather than by fudging:
#   * a message_delta that reports MORE tokens supersedes the message_start
#     for the same response (server.py keeps the larger), so the expected
#     value for that response is the superseded one;
#   * rows the proxy marked "estimate:chars/4" are not measurements and are
#     excluded from the pool — matching against them would let a guess stand
#     in for a measurement.
TOKEN_LINE = re.compile(
    r"Input tokens from (message_start|message_delta|response): ([\d,]+)")
EST_LINE = re.compile(
    r"No tokens from SSE, estimating from chars: [\d,]+ chars -> ~([\d,]+) tokens")

groups = []
for ln in proxy_text.splitlines():
    m = TOKEN_LINE.search(ln)
    if m:
        kind, val = m.group(1), int(m.group(2).replace(",", ""))
        if (kind == "message_delta" and groups
                and groups[-1]["origin"] in ("message_start", "message_delta")
                and not groups[-1]["closed"] and val > groups[-1]["expect"]):
            groups[-1]["expect"] = val
            groups[-1]["superseded_by_delta"] = True
        else:
            groups.append({"origin": kind, "logged": val, "expect": val,
                           "superseded_by_delta": False, "closed": False,
                           "line": ln.strip()[-160:]})
        continue
    m = EST_LINE.search(ln)
    if m:
        groups.append({"origin": "estimate",
                       "logged": int(m.group(1).replace(",", "")),
                       "expect": int(m.group(1).replace(",", "")),
                       "superseded_by_delta": False, "closed": True,
                       "line": ln.strip()[-160:]})
        continue
    # A new upstream exchange starts here: close the previous group so a
    # later message_delta can never be folded into an earlier response.
    if ("Upstream response:" in ln or "] Response:" in ln
            or "] Forwarding to " in ln):
        if groups:
            groups[-1]["closed"] = True

request_rows = [r for r in metrics_rows if r.get("event") == "request"]
# BLANK IS NOT ZERO: a request row may legitimately carry a blank
# context_tokens (nothing was measured), but a literal 0 would be a lie.
zero_rows = [r for r in request_rows if (r.get("context_tokens") or "").strip() == "0"]
pool = {}
for idx, r in enumerate(request_rows):
    ct = (r.get("context_tokens") or "").strip()
    if not ct or "estimate" in (r.get("note") or ""):
        continue
    try:
        pool.setdefault(int(ct), []).append(idx)
    except ValueError:
        continue

# A measurement is any token count the proxy READ OUT of the upstream, wherever
# the upstream chose to put it. Anthropic puts it in message_start; OpenRouter
# (and other Anthropic-format third parties) send message_start.usage.
# input_tokens == 0 and report the real count only in message_delta. Keying
# this gate on message_start alone made it unrunnable against any non-Anthropic
# upstream: `measured` came back empty and the gate hard-failed with "there is
# no measurement to check the metrics against" while metrics.csv was in fact
# full of correct, measured numbers. Groups whose message_start was superseded
# by a later delta keep origin "message_start", so nothing is counted twice.
measured = [g for g in groups if g["origin"] in ("message_start", "message_delta")]
matched, unmatched = [], []
for g in measured:
    slot = pool.get(g["expect"])
    if slot:
        matched.append({"tokens": g["expect"], "row": slot.pop(0),
                        "superseded_by_delta": g["superseded_by_delta"]})
    else:
        unmatched.append({"tokens_logged": g["logged"],
                          "tokens_expected": g["expect"],
                          "log_line": g["line"]})
leftover = sorted(v for k, vs in pool.items() for v in vs)

tokens_true = bool(measured) and not unmatched and not zero_rows
add("metrics_tokens_true", tokens_true,
    (f"{len(matched)}/{len(measured)} logged upstream token counts have a "
     f"matching request row (context_tokens ==), {len(zero_rows)} row(s) record "
     f"a fake 0"
     if measured else
     "NO 'Input tokens from message_start/message_delta' lines in the proxy "
     "log — there is no measurement to check the metrics against"),
    {"message_start_lines": len(measured),
     "request_rows": len(request_rows),
     "matched": len(matched),
     "unmatched": unmatched[:10],
     "rows_no_message_start_explains": leftover[:10],
     "zero_context_token_rows": zero_rows[:5],
     "note": "rows marked estimate:chars/4 are excluded from the pool: an "
             "estimate is not a measurement. Unmatched request rows are "
             "reported but do not fail — non-streaming responses log "
             "'Input tokens from response:' instead."})

# ── observations (not pass/fail gates) ─────────────────────────────────────
recall_out = ""
for t in turns:
    if t.get("phase") == "recall":
        try:
            with open(t.get("out", ""), encoding="utf-8", errors="replace") as f:
                recall_out = f.read()
        except OSError:
            pass
observations = {
    "passenger_recalled_beacon": bool(beacon) and beacon in recall_out,
    "passenger_used_ferry_fetch": "ferry-fetch" in recall_out,
    "recall_excerpt": recall_out[:500],
    "turns_run": len(turns),
    "turn_seconds_total": sum(t.get("seconds") or 0 for t in turns),
    "restart_count": len(restart),
    "proxy_epochs": len(of("proxy_start")),
    "archive_files": [os.path.basename(p) for p in archive_files],
    "archive_lines_final": (checkpoints[-1].get("archive_lines") if checkpoints else 0),
    "client_turn_failures": turn_failures,
}

hard = [a for a in assertions if a["status"] == "FAIL"]
warns = [a for a in assertions if a["status"] == "WARN"]
verdict = "PASS" if not hard else "FAIL"

out = {
    "verdict": verdict,
    "rig_root": root,
    "config": cfg,
    "assertions": assertions,
    "observations": observations,
    "counts": {"pass": len([a for a in assertions if a["status"] == "PASS"]),
               "fail": len(hard), "warn": len(warns)},
}
with open(os.path.join(root, "verdict.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

lines = []
lines.append("=" * 72)
lines.append(f" FERRY LIFECYCLE VERDICT: {verdict}")
lines.append(f" rig root: {root}")
lines.append("=" * 72)
for a in assertions:
    mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}[a["status"]]
    lines.append(f"[{mark}] {a['key']}")
    lines.append(f"        {a['headline']}")
    ev = a["evidence"]
    for k, v in ev.items():
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        s = s.replace("\n", " ")
        if len(s) > 300:
            s = s[:300] + " ..."
        lines.append(f"        - {k}: {s}")
    lines.append("")
lines.append("-" * 72)
lines.append(" OBSERVATIONS (not gates)")
for k, v in observations.items():
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    if len(s) > 300:
        s = s[:300] + " ..."
    lines.append(f"   {k}: {s}")
lines.append("-" * 72)
lines.append(f" {out['counts']['pass']} pass / {out['counts']['fail']} fail / "
             f"{out['counts']['warn']} warn")
lines.append("=" * 72)
text = "\n".join(lines)
with open(os.path.join(root, "verdict.txt"), "w", encoding="utf-8") as f:
    f.write(text + "\n")
print(text)
sys.exit(0 if verdict == "PASS" else 3)
PYEOF

set +e
python3 "$RIG_ROOT/verdict.py" "$RIG_ROOT" | tee -a "$RIG_LOG"
VERDICT_RC=${PIPESTATUS[0]}
set -e

log "verdict written: $VERDICT_JSON / $VERDICT_TXT (rc=$VERDICT_RC)"
echo "================================================================"
echo " rig root : $RIG_ROOT"
echo " log      : $RIG_LOG"
echo " verdict  : $VERDICT_JSON"
echo "            $VERDICT_TXT"
echo "================================================================"

# Non-zero on a failing verdict so a scheduler notices; cleanup runs via trap.
exit "$VERDICT_RC"
