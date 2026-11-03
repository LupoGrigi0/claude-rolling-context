#!/usr/bin/env bash
# Ensure THIS mind's Ferry proxy is running, on THIS mind's port, AS THIS USER.
# Claude Code's SessionStart hook runs it; install.sh runs it too. Pure stdlib.
#
# Config: ~/preferences.json "ferry" via hooks/ferry_config.py -- the only source.
#   1. The declared port is already held by a server.py of this uid -> reuse it.
#   2. Otherwise start one, and VERIFY the pid we launched holds the port.
#   Only then point settings.json's ANTHROPIC_BASE_URL at it. On failure, take
#   back a base URL Ferry wrote (never anyone else's) and fail loud.
# A proxy that failed to bind because another mind's proxy is on the port must
# never be mistaken for ours: that is mind #2 routing through mind #1's archive.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"

if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]]; then
    IS_WINDOWS=true
else
    IS_WINDOWS=false
fi
if [ "$IS_WINDOWS" = true ]; then PY_CMD="python"
elif command -v python3 &>/dev/null; then PY_CMD="python3"
else PY_CMD="python"
fi
CONF="$PY_CMD $SCRIPT_DIR/ferry_config.py"

HOOKLOG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/rolling-context-hook.log"
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG" 2>/dev/null
}
# Loud: the hook log, stdout (SessionStart adds it to the mind's context) and
# stderr, then non-zero.
fail() {
    log "ERROR: $1"
    echo "[FERRY] ERROR: $1"
    echo "[FERRY] ERROR: $1" >&2
    exit 1
}
# No proxy of ours holds the port: an earlier run's pointer would send the NEXT
# session to a dead port or someone else's proxy. Take it back (only if Ferry
# wrote it), then fail loud.
fail_withdraw() {
    local w
    w=$($CONF withdraw 2>&1 | tr '\n' ' ')
    fail "$1 settings.json: ${w:-no Ferry pointer to withdraw}"
}

# The exports below are the TRANSPORT to the proxy we start; they replace
# whatever the parent process carried (e.g. settings.json env Claude Code injected).
CONFIG_OUT=$($CONF env) || fail "ferry_config.py failed to run"
eval "$CONFIG_OUT"
# A config error must not leave an earlier run's pointer aimed at a port no
# proxy will hold: take it back (only if Ferry wrote it).
[ -n "$FERRY_CONFIG_ERROR" ] && fail_withdraw "$FERRY_CONFIG_ERROR"

CLAUDE_DIR="$FERRY_CONFIG_DIR"
mkdir -p "$CLAUDE_DIR"
HOOKLOG="$CLAUDE_DIR/rolling-context-hook.log"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
PORT="$ROLLING_CONTEXT_PORT"

log "Hook started. PORT=$PORT UPSTREAM=$ROLLING_CONTEXT_UPSTREAM FERRY_DATA=$FERRY_DATA"

point_settings() {
    local result
    result=$($CONF point 2>&1) || fail "settings.json NOT pointed at port $PORT: $result"
    log "settings.json: $result (ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT)"
}

_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
}

# 1. Reuse: whoever of THIS uid holds the port (who-holds sees only our own
# processes), else the pidfile's pid (no /proc, e.g. macOS) -- either way it
# must be a server.py that holds the declared port.
for CAND in $($CONF who-holds "$PORT" 2>/dev/null) $(cat "$PIDFILE" 2>/dev/null); do
    if kill -0 "$CAND" 2>/dev/null && $CONF is-proxy "$CAND" && $CONF holds-port "$CAND" "$PORT"; then
        # Ours -- but it keeps the upstream it was STARTED with. If preferences
        # changed since, reusing it would make it a second source of the route.
        if ! WHY=$($CONF proxy-matches "$PORT" 2>&1); then
            fail_withdraw "proxy PID $CAND on port $PORT does not match ~/preferences.json: $WHY. Stop that PID (docs/RUNBOOK.md section 3) and start a new session."
        fi
        echo "$CAND" > "$PIDFILE"
        log "Reusing proxy PID $CAND on port $PORT"
        point_settings
        exit 0
    fi
done
rm -f "$PIDFILE"

# 2. Start one. exec, so $! is the python process itself and not a wrapper shell.
log "Starting proxy on port $PORT..."
# server.py puts its debug log in ~/.claude, which a CLAUDE_CONFIG_DIR mind may
# not have -- the FileHandler then kills it at import. Keep it in the config dir.
[ -d "$HOME/.claude" ] || export ROLLING_CONTEXT_LOG="${ROLLING_CONTEXT_LOG:-$CLAUDE_DIR/rolling-context-debug-$(basename "$FERRY_DATA").log}"
(cd "$PROXY_DIR" && exec nohup $PY_CMD server.py > "$CLAUDE_DIR/rolling-context-proxy.log" 2>&1) &
PID=$!

# Verify: the pid WE started must hold the port. Up to ~15s.
for _ in $(seq 60); do
    if $CONF holds-port "$PID" "$PORT"; then
        echo "$PID" > "$PIDFILE"
        log "Proxy started with PID $PID, verified on port $PORT"
        point_settings
        exit 0
    fi
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.25
done

HOLDER=$($CONF who-holds "$PORT" 2>/dev/null)
if kill -0 "$PID" 2>/dev/null; then
    _kill_pid "$PID"       # the process we just started, never another
fi
TAIL=$(tail -3 "$CLAUDE_DIR/rolling-context-proxy.log" 2>/dev/null | tr '\n' ' ')
fail_withdraw "proxy PID $PID did not take port $PORT${HOLDER:+ (held by PID $HOLDER)}. Log: $TAIL."
