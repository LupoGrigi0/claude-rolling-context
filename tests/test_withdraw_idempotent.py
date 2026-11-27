#!/usr/bin/env python3
"""Withdraw is idempotent and never deletes (Forge, Den drill T5, 2026-11-27).

A failed restart withdraws TWICE in milliseconds (the hook's fail_withdraw, then
ferry-restart's last resort). If the second one deleted the explicit route the
first one wrote, a settings watcher that coalesces the two writes would show
the running session only a DELETION -- which it ignores, staying on the dead port.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolate import home_env  # noqa: E402

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


root = Path(tempfile.mkdtemp(prefix="ferry-wd2-"))
home, cfg = root / "home", root / "cfg"
home.mkdir(); cfg.mkdir()
(home / "preferences.json").write_text(json.dumps({"ferry": {"port": 5799}}))
ours = "http://127.0.0.1:5799"
S = cfg / "settings.json"
S.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": ours, "ROLLING_CONTEXT_WROTE_BASE_URL": ours}}))
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
env.update(home_env(home), CLAUDE_CONFIG_DIR=str(cfg))


def withdraw():
    subprocess.run([sys.executable, str(REPO / "hooks" / "ferry_config.py"), "withdraw"],
                   env=env, capture_output=True, text=True)
    return json.loads(S.read_text()).get("env", {})


e1 = withdraw()
check("first withdraw: Ferry's port -> explicit https://api.anthropic.com (a CHANGE: live)",
      e1.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com", e1)
e2 = withdraw()
check("second withdraw: STILL https://api.anthropic.com -- never deleted", e2.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com", e2)
check("...the file is unchanged by the second withdraw (idempotent)", e1 == e2, (e1, e2))
e3 = withdraw()
check("third withdraw: still idempotent", e3 == e1, e3)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
