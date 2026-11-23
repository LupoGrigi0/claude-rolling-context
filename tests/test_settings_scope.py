#!/usr/bin/env python3
"""preferences "settings": Ferry writes its route and hook into ONE named settings
file instead of Claude Code's user settings.json.

Why: on .nexus each mind is its own unix user, so the user settings.json is that
mind's alone. On a single-user box (lupos-lap, Windows) the user file is shared by
every Claude process of that user -- sentinel probes, doorbell ringers, a human's
own sessions -- and pointing it at one mind's proxy routes all of them through it,
and takes all of them dark if it dies. A project's .claude/settings.local.json
routes only the sessions launched in that directory.

Every "writes there" check has a "does NOT write here" twin, and every refusal
checks that nothing was written.

Isolation: Python's expanduser reads USERPROFILE on Windows, HOME elsewhere, so
both are pointed at the scratch home. (Setting HOME alone on Windows would read
the REAL ~/preferences.json.)

Author: Lodestone <lodestone@smoothcurves.nexus> / Collaborator: Lupo
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "hooks" / "ferry_config.py"
passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


root = Path(tempfile.mkdtemp(prefix="ferry-scope-"))
home, cfg, proj = root / "home", root / "cfg", root / "proj" / ".claude"
for d in (home, cfg, proj):
    d.mkdir(parents=True)
env = {k: v for k, v in os.environ.items()
       if k not in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(cfg))
USER_S = cfg / "settings.json"
PROJ_S = proj / "settings.local.json"
PREFS = home / "preferences.json"
PORT = 5799


def conf(*args):
    r = subprocess.run([sys.executable, str(CONF), *args], env=env, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def prefs(**ferry):
    PREFS.write_text(json.dumps({"ferry": {"port": PORT, **ferry}}), encoding="utf-8")


def read(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def reset():
    for p in (USER_S, PROJ_S):
        if p.exists():
            p.unlink()


def ferry_hooks(s):
    return [h for g in ((s or {}).get("hooks", {}).get("SessionStart") or [])
            for h in g.get("hooks", []) if "start-proxy" in str(h.get("command", ""))]


# The isolation itself: a test that reads the real home proves nothing.
# Positive, not "nothing found": the real home may have no preferences either, so
# only a value that exists ONLY in the scratch home proves the resolver read it.
prefs()
code, out = conf("env")
check("isolation: the resolver reads the scratch preferences, not the real home",
      f"ROLLING_CONTEXT_PORT={PORT}" in out, out[:200])

print("=== default (no \"settings\"): unchanged behaviour, the user settings.json ===")
reset(); prefs()
code, out = conf("register-hook")
check("register-hook exits 0", code == 0, out)
check("hook written to the USER settings.json", len(ferry_hooks(read(USER_S))) == 1)
check("and no project file was created", not PROJ_S.exists())
code, out = conf("point")
check("point writes the base URL to the USER file",
      (read(USER_S) or {}).get("env", {}).get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{PORT}", out)

print("=== \"settings\" = a project .claude/settings.local.json ===")
reset(); prefs(settings=str(PROJ_S))
code, out = conf("register-hook")
check("register-hook exits 0", code == 0, out)
check("hook written to the PROJECT file", len(ferry_hooks(read(PROJ_S))) == 1)
check("and the USER file was not created", not USER_S.exists())
code, out = conf("point")
check("point exits 0", code == 0, out)
check("base URL in the PROJECT file",
      (read(PROJ_S) or {}).get("env", {}).get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{PORT}")
check("USER file still absent: other processes of this user stay direct", not USER_S.exists())

# Merges, never clobbers: what the project file already held survives.
reset()
PROJ_S.write_text(json.dumps({"worktree": {"bgIsolation": "none"}, "env": {"MINE": "1"}}), encoding="utf-8")
conf("register-hook"); conf("point")
s = read(PROJ_S) or {}
check("existing project keys kept (worktree.bgIsolation)", s.get("worktree") == {"bgIsolation": "none"})
check("existing env keys kept", s.get("env", {}).get("MINE") == "1")

print("=== uninstall paths act on the PROJECT file ===")
code, out = conf("withdraw")
check("withdraw removes the base URL from the project file",
      "ANTHROPIC_BASE_URL" not in (read(PROJ_S) or {}).get("env", {}), out)
conf("point")
code, out = conf("unpoint")
check("unpoint removes it too", "ANTHROPIC_BASE_URL" not in (read(PROJ_S) or {}).get("env", {}), out)
code, out = conf("unregister-hook")
check("unregister-hook removes Ferry's hook from the project file", len(ferry_hooks(read(PROJ_S))) == 0, out)
check("and still leaves the project's own keys", (read(PROJ_S) or {}).get("worktree") == {"bgIsolation": "none"})

print("=== the guard reads the PROJECT file ===")
reset()
PROJ_S.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://openrouter.example/api"}}), encoding="utf-8")
code, out = conf("point")
check("a foreign route in the project file -> point REFUSES", code != 0, out)
check("and the foreign route is untouched",
      (read(PROJ_S) or {}).get("env", {}).get("ANTHROPIC_BASE_URL") == "https://openrouter.example/api")
check("and it names the project file", str(PROJ_S) in out, out)

print("=== bad \"settings\" values refuse and write NOTHING ===")
bad = {
    "relative path": "proj/.claude/settings.local.json",
    "not a .json file": str(proj / "settings.txt"),
    "not inside a .claude dir": str(root / "proj" / "settings.json"),
    "directory does not exist": str(root / "nowhere" / ".claude" / "settings.json"),
}
for label, val in bad.items():
    reset(); prefs(settings=val)
    code, out = conf("point")
    check(f"{label}: point refuses (exit {code})", code != 0, out)
    code, out = conf("env")
    check(f"{label}: env reports FERRY_CONFIG_ERROR, so the hook starts nothing",
          "FERRY_CONFIG_ERROR=''" not in out and "FERRY_CONFIG_ERROR=" in out, out[-200:])
    check(f"{label}: neither file written", not USER_S.exists() and not PROJ_S.exists())
check("no stray file next to the bad paths", not (root / "proj" / "settings.json").exists()
      and not (root / "nowhere").exists())

print()
print(f"  {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
