#!/usr/bin/env python3
"""Ferry's SessionStart hook lives in settings.json `hooks`, merged, never clobbering.

Cairn measured (2026-11-16) that the old plugin symlink is not loaded by this
Claude Code: the hook never fired on --bg --resume, so a dead proxy was never
restarted. Registration now writes one SessionStart entry running THIS clone's
hooks/start-proxy.sh -- and must leave every other hook exactly as it found it.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# POSIX BY DESIGN. On Windows, announce a NAMED could-not-run before touching anything.
# The exemption itself lives in tests/run_all.py ENV_BOUND_WINDOWS, granted by a human --
# a suite never grants itself one. Lodestone-8ec9, 2026-11-26.
if os.name == "nt":
    print("SKIP(env): asserts the bash SessionStart hook form (bash .../hooks/start-proxy.sh); on Windows ferry_config.py registers the PowerShell form, which this suite does not measure")
    print("0 passed, 0 failed")
    raise SystemExit(0)

REPO = Path(__file__).resolve().parent.parent
from _isolate import home_env  # HOME alone is not isolation on Windows
CONF = REPO / "hooks" / "ferry_config.py"
OURS = f"bash {REPO}/hooks/start-proxy.sh"
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


root = Path(tempfile.mkdtemp(prefix="ferry-hookreg-"))
home, cfg = root / "home", root / "cfg"
home.mkdir()
cfg.mkdir()
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
env.update(home_env(home), CLAUDE_CONFIG_DIR=str(cfg))
S = cfg / "settings.json"

THEIRS_PERM = {"matcher": "*", "hooks": [{"type": "command", "command": "node ~/mirror/permission-hook.mjs; exit 0", "timeout": 604800}]}
THEIRS_START = {"hooks": [{"type": "command", "command": "bash ~/my-own-startup.sh"}]}
S.write_text(json.dumps({"autoCompactEnabled": False,
                         "hooks": {"PermissionRequest": [THEIRS_PERM], "SessionStart": [THEIRS_START]}}))


def conf(cmd):
    r = subprocess.run([sys.executable, str(CONF), cmd], env=env, capture_output=True, text=True)
    return r.returncode, json.loads(S.read_text(encoding="utf-8"))


def ferry_cmds(s):
    return [h["command"] for g in s.get("hooks", {}).get("SessionStart", []) for h in g["hooks"]
            if "/hooks/start-proxy." in h["command"]]


code, s = conf("register-hook")
check("register-hook exits 0", code == 0)
check("exactly one Ferry SessionStart entry", ferry_cmds(s) == [OURS], ferry_cmds(s))
check("it runs THIS clone's start-proxy.sh", OURS in ferry_cmds(s))
check("their SessionStart hook is untouched", THEIRS_START in s["hooks"]["SessionStart"], s["hooks"])
check("their PermissionRequest hook is untouched", s["hooks"]["PermissionRequest"] == [THEIRS_PERM])
check("unrelated settings are untouched", s.get("autoCompactEnabled") is False)

code, s = conf("register-hook")
check("registering twice still leaves exactly one Ferry entry", ferry_cmds(s) == [OURS], ferry_cmds(s))

code, s = conf("unregister-hook")
check("unregister-hook exits 0", code == 0)
check("Ferry's entry is gone", ferry_cmds(s) == [], ferry_cmds(s))
check("their SessionStart hook survives unregister", s["hooks"]["SessionStart"] == [THEIRS_START], s["hooks"])
check("their PermissionRequest hook survives unregister", s["hooks"]["PermissionRequest"] == [THEIRS_PERM])

before = S.read_text(encoding="utf-8")
code, s = conf("unregister-hook")
check("unregister with nothing of ours changes nothing (control)", json.loads(before) == s)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
