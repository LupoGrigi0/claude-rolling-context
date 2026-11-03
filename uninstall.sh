#!/usr/bin/env bash
# Uninstall the Rolling Context (Ferry) plugin for ONE mind.
# Honours CLAUDE_CONFIG_DIR. Never touches FERRY_DATA (the archive) itself.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 &>/dev/null; then PY_CMD="python3"
elif command -v python &>/dev/null; then PY_CMD="python"
else PY_CMD=""
fi
CONF="$PY_CMD $SCRIPT_DIR/hooks/ferry_config.py"

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PORT=""
FERRY_SUPERVISED=0
if [ -n "$PY_CMD" ]; then
    # A config error (e.g. no port) is fine here: we only lose the port fallback.
    if CONFIG_OUT=$($CONF env); then
        eval "$CONFIG_OUT"
        CLAUDE_DIR="$FERRY_CONFIG_DIR"
        PORT="$ROLLING_CONTEXT_PORT"
    fi
fi
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
PLUGIN_LINK="$CLAUDE_DIR/plugins/rolling-context"
MARKETPLACE_CACHE="$CLAUDE_DIR/plugins/cache/rolling-context-marketplace"
MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/rolling-context-marketplace"

echo "=== Uninstalling Rolling Context ==="

# Stop proxy — PID file first, then whoever of OURS holds the port. Only ever a
# pid whose cmdline is server.py: a stale pidfile or a shared port may name
# something else entirely.
if [ "$FERRY_SUPERVISED" = 1 ]; then
    echo "Supervised: the supervisor owns the proxy on port $PORT; not stopping it."
else
    STOPPED=false
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null && $CONF is-proxy "$PID"; then
            kill "$PID"
            echo "Stopped proxy (PID $PID)"
            STOPPED=true
        fi
    fi
    if [ "$STOPPED" = false ] && [ -n "$PORT" ] && [ -n "$PY_CMD" ]; then
        PROXY_PID=$($CONF who-holds "$PORT" 2>/dev/null || true)
        if [ -n "$PROXY_PID" ] && $CONF is-proxy "$PROXY_PID"; then
            kill "$PROXY_PID" 2>/dev/null || true
            echo "Stopped proxy on port $PORT (PID $PROXY_PID)"
        fi
    fi
fi
rm -f "$PIDFILE" "$CLAUDE_DIR/rolling-context-proxy.version"

# Remove all log files
rm -f "$CLAUDE_DIR/rolling-context-proxy.log"
rm -f "$CLAUDE_DIR/rolling-context-proxy.log.err"
rm -f "$CLAUDE_DIR/rolling-context-debug.log"
rm -f "$CLAUDE_DIR/rolling-context-hook.log"

# Remove plugin link (manual install)
if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
    echo "Removed plugin link"
fi

# Remove the ferry-fetch link install.sh made -- only if it is still ours
FETCH_LINK="$HOME/.local/bin/ferry-fetch"
if [ -L "$FETCH_LINK" ] && [ "$(readlink "$FETCH_LINK")" = "$SCRIPT_DIR/bin/ferry-fetch" ]; then
    rm -f "$FETCH_LINK"
    echo "Removed ferry-fetch link"
fi

# Remove marketplace-installed plugin cache
if [ -d "$MARKETPLACE_CACHE" ]; then
    rm -rf "$MARKETPLACE_CACHE"
    echo "Removed marketplace plugin cache"
fi

# Remove marketplace registration
if [ -d "$MARKETPLACE_DIR" ]; then
    rm -rf "$MARKETPLACE_DIR"
    echo "Removed marketplace registration"
fi

# Clean installed_plugins.json
INSTALLED_FILE="$CLAUDE_DIR/plugins/installed_plugins.json"
if [ -f "$INSTALLED_FILE" ] && [ -n "$PY_CMD" ]; then
    $PY_CMD - "$INSTALLED_FILE" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
if 'rolling-context@rolling-context-marketplace' in data.get('plugins', {}):
    del data['plugins']['rolling-context@rolling-context-marketplace']
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed from installed plugins')
PYEOF
fi

# Clean known_marketplaces.json
MARKETPLACES_FILE="$CLAUDE_DIR/plugins/known_marketplaces.json"
if [ -f "$MARKETPLACES_FILE" ] && [ -n "$PY_CMD" ]; then
    $PY_CMD - "$MARKETPLACES_FILE" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
if 'rolling-context-marketplace' in data:
    del data['rolling-context-marketplace']
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed marketplace')
PYEOF
fi

# Clean settings.json: ANTHROPIC_BASE_URL ONLY if Ferry wrote it (restoring any
# upstream it displaced), then Ferry's own env keys.
if [ -n "$PY_CMD" ]; then
    $CONF unpoint
fi

echo ""
echo "Uninstalled."
