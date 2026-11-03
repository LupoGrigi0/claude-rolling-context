#!/usr/bin/env bash
# Ensure THIS mind's Ferry proxy is running, on THIS mind's port.
# Pure stdlib — no venv needed, just python.
#
# The only launcher: install.sh runs this too. Config comes from
# hooks/ferry_config.py (~/preferences.json "ferry", env overriding).
#   supervised  -> start nothing, write nothing; the supervisor owns the port.
#   otherwise   -> start (or reuse) the proxy, then VERIFY that the pid we
#                  launched holds the port. Only then point settings.json at it.
# A proxy that failed to bind because another mind's proxy is on the port must
# never be mistaken for ours: that is mind #2 routing through mind #1's archive.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
CURRENT_VERSION=$(cat "$SCRIPT_DIR/../.claude-plugin/plugin.json" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

# Detect Windows (git bash)
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
# stderr, then non-zero. settings.json is NOT touched on this path.
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

CONFIG_OUT=$($CONF env) || fail "ferry_config.py failed to run"
eval "$CONFIG_OUT"
[ -n "$FERRY_CONFIG_ERROR" ] && fail "$FERRY_CONFIG_ERROR"

CLAUDE_DIR="$FERRY_CONFIG_DIR"
mkdir -p "$CLAUDE_DIR"
HOOKLOG="$CLAUDE_DIR/rolling-context-hook.log"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
VERFILE="$CLAUDE_DIR/rolling-context-proxy.version"
PORT="$ROLLING_CONTEXT_PORT"

log "Hook started. PROXY_DIR=$PROXY_DIR PORT=$PORT FERRY_DATA=$FERRY_DATA SUPERVISED=$FERRY_SUPERVISED"

if [ "$FERRY_SUPERVISED" = 1 ]; then
    log "Supervised: the supervisor owns port $PORT. Not starting a proxy, not touching settings.json."
    exit 0
fi

point_settings() {
    local result
    result=$($CONF point 2>&1) || fail "could not update $CLAUDE_DIR/settings.json: $result"
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

# Alive AND still our proxy: a stale pidfile's pid may now be anything.
_our_proxy_alive() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null && $CONF is-proxy "$pid"
    fi
}

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if _our_proxy_alive "$PID"; then
        RUNNING_VERSION=$(cat "$VERFILE" 2>/dev/null)
        if [ "$CURRENT_VERSION" = "$RUNNING_VERSION" ]; then
            $CONF holds-port "$PID" "$PORT" \
                || fail_withdraw "proxy PID $PID is running but does NOT hold port $PORT (config changed? restart it)."
            log "Proxy already running (PID $PID, v$RUNNING_VERSION, holds port $PORT)"
            point_settings
            exit 0
        fi
        log "Version changed ($RUNNING_VERSION -> $CURRENT_VERSION), restarting proxy (PID $PID)"
        _kill_pid "$PID"
    fi
    rm -f "$PIDFILE" "$VERFILE"
fi

# Start proxy directly — no venv needed (pure stdlib). The exported config
# (ROLLING_CONTEXT_PORT, FERRY_DATA, ROLLING_CONTEXT_CURATION, ...) is its env.
# exec, so $! is the python process itself and not a wrapper shell.
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
        echo "$CURRENT_VERSION" > "$VERFILE"
        log "Proxy started with PID $PID (v$CURRENT_VERSION), verified on port $PORT"
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
