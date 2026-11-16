#!/usr/bin/env python3
"""Production watermark + an absolute ferry-fetch path (first passenger, 2026-11-16).

1. trigger/target come from ~/preferences.json "ferry", defaulting to 800k/600k
   (Lupo: production minds run 1M windows); bad values are refused, never guessed.
2. The curated framing names ferry-fetch by ABSOLUTE path: WakeTest2-b0ec's Bash
   tool had no ~/.local/bin on PATH, so a bare `ferry-fetch` read as "command
   not found" -- i.e. "my archive is unreachable".
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


home = Path(tempfile.mkdtemp(prefix="ferry-wm-"))
(home / ".claude").mkdir()
env = {"HOME": str(home), "PATH": os.environ["PATH"], "CLAUDE_CONFIG_DIR": str(home / ".claude")}


def prefs(block):
    (home / "preferences.json").write_text(json.dumps({"ferry": block}))


def conf_env():
    r = subprocess.run([sys.executable, str(REPO / "hooks" / "ferry_config.py"), "env"],
                       capture_output=True, text=True, env=env)
    return r.stdout


prefs({"port": 5799})
out = conf_env()
check("default trigger is 800000", "ROLLING_CONTEXT_TRIGGER=800000" in out, out)
check("default target is 600000", "ROLLING_CONTEXT_TARGET=600000" in out, out)
check("defaults are not a config error", "FERRY_CONFIG_ERROR=''" in out or "FERRY_CONFIG_ERROR=" not in out.replace("FERRY_CONFIG_ERROR=''", ""), out)

prefs({"port": 5799, "trigger": 300000, "target": 200000})
out = conf_env()
check("preferences trigger is honoured", "ROLLING_CONTEXT_TRIGGER=300000" in out, out)
check("preferences target is honoured", "ROLLING_CONTEXT_TARGET=200000" in out, out)

prefs({"port": 5799, "trigger": 500000, "target": 500000})
out = conf_env()
check("target >= trigger is refused", "must be below" in out, out)

prefs({"port": 5799, "trigger": "lots"})
out = conf_env()
check("non-numeric trigger is refused", "not a positive whole number" in out, out)

sys.path.insert(0, str(REPO / "proxy"))
import curation  # noqa: E402

check("FERRY_FETCH is an absolute path", os.path.isabs(curation.FERRY_FETCH), curation.FERRY_FETCH)
check("FERRY_FETCH is the repo's own bin/ferry-fetch",
      Path(curation.FERRY_FETCH) == REPO / "bin" / "ferry-fetch", curation.FERRY_FETCH)
check("FERRY_FETCH exists and is executable", os.access(curation.FERRY_FETCH, os.X_OK))
check("framing names the absolute path", curation.FERRY_FETCH + " '<pointer>'" in curation.CURATION_FRAMING_USER)
check("framing never names a bare ferry-fetch",
      " ferry-fetch '" not in curation.CURATION_FRAMING_USER.replace(curation.FERRY_FETCH, "X"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
