#!/usr/bin/env bash
# Install Ferry (rolling context, curation=ferry) for ONE mind.
#
# Config: ~/preferences.json "ferry" block, env vars overriding -- see
# hooks/ferry_config.py. A port is REQUIRED; there is no shared default.
# Honours CLAUDE_CONFIG_DIR for settings.json and the plugin link.
#
# Pure stdlib — no pip install needed. Just requires Python 3.7+.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxy"

echo "=== Ferry Installer ==="
echo ""

# 1. Check Python is available
echo "[1/4] Checking Python..."
if command -v python3 &>/dev/null; then PY_CMD="python3"
elif command -v python &>/dev/null; then PY_CMD="python"
else
    echo "  ERROR: Python not found. Install Python 3.7+ and try again."
    exit 1
fi
echo "  Found $($PY_CMD --version 2>&1) (pure stdlib — no pip install needed)"
CONF="$PY_CMD $SCRIPT_DIR/hooks/ferry_config.py"

# 2. Resolve config from the one declared source and persist it. `install`
# runs BEFORE the eval below: it ranks each value by where it came from, and
# our own exports would otherwise read back to it as env overrides.
echo "[2/4] Resolving Ferry config..."
$CONF install || exit 1
CONFIG_OUT=$($CONF env)     # set -e: a resolver that crashed stops the install
eval "$CONFIG_OUT"
CLAUDE_DIR="$FERRY_CONFIG_DIR"

# 3. Register plugin
echo "[3/4] Registering Claude Code plugin..."
PLUGIN_LINK="$CLAUDE_DIR/plugins/rolling-context"
mkdir -p "$CLAUDE_DIR/plugins"
if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
fi
ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"
echo "  Plugin linked at $PLUGIN_LINK"
# Every curated prefix tells the mind to run a bare `ferry-fetch`.
mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/bin/ferry-fetch" "$HOME/.local/bin/ferry-fetch"
echo "  ferry-fetch linked at $HOME/.local/bin/ferry-fetch"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "  WARNING: $HOME/.local/bin is not on PATH -- add it, or the mind's"
       echo "           ferry-fetch (named in every curated prefix) is 'command not found'." ;;
esac

# 4. Start + verify the proxy, through the ONE launcher (the SessionStart hook).
# It points ANTHROPIC_BASE_URL at the port only after its own pid holds it.
echo "[4/4] Starting proxy on port $ROLLING_CONTEXT_PORT..."
if [ "$FERRY_SUPERVISED" = 1 ]; then
    # The supervisor launches from the same config, so the port is declared,
    # not guessed. Point only once the canary proves THIS mind's proxy is on it.
    echo "  Supervised: not starting a proxy here."
    $PY_CMD "$SCRIPT_DIR/tools/ferry-canary" --wait 10 \
        || { echo "  Supervised proxy not proven (above); settings.json NOT pointed. Install NOT complete."; exit 1; }
    echo "  ANTHROPIC_BASE_URL: $($CONF point)"
else
    bash "$SCRIPT_DIR/hooks/start-proxy.sh" || { echo "  Install NOT complete."; exit 1; }
    echo "  Proxy verified on port $ROLLING_CONTEXT_PORT; ANTHROPIC_BASE_URL points at it."
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "  ROLLING_CONTEXT_PORT     = $ROLLING_CONTEXT_PORT"
echo "  ROLLING_CONTEXT_CURATION = $ROLLING_CONTEXT_CURATION"
echo "  FERRY_DATA               = $FERRY_DATA"
echo "  settings.json            = $CLAUDE_DIR/settings.json"
echo ""
echo "To start it manually: bash $SCRIPT_DIR/hooks/start-proxy.sh"
echo "Start a new Claude Code session to route through Ferry."
