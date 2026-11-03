#!/usr/bin/env python3
"""Ferry's upstream: ONE route, ONE source (Lupo, 2026-11-03).

CONTRACT: endpoints.load_upstream() is env ROLLING_CONTEXT_UPSTREAM if set,
else DEFAULT_UPSTREAM. The hook sets that variable from ~/preferences.json
"ferry"."upstream". settings.json scopes and the process ANTHROPIC_BASE_URL are
NOT upstream sources any more -- a second source of the same truth is the
debugging headache Lupo named.

HISTORY, kept because the measurement is still true: Forge-ba0e (2026-10-09)
showed a mind whose backend was OpenRouter resolving to Anthropic when the
backend lived anywhere but ~/.claude/settings.json -- its key sent to the wrong
provider. The old answer was to walk Claude Code's scopes. The answer now is
at INSTALL: hooks/ferry_config.py refuses to point a mind whose settings route
away from Anthropic until preferences names "upstream" (tested in
test_install.py). So the A/B/C cases below now assert the proxy IGNORES those
places -- the route comes from preferences, through the hook, or nowhere.

The refuse-loudly half (unroutable_scope, cases D-H) is kept as measured
knowledge of Claude Code's scopes; nothing in the proxy calls it now.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OR = "https://openrouter.ai/api"

ANTHROPIC = "https://api.anthropic.com"
# case -> (description, the upstream load_upstream() must return)
CASES = {
    "A": ("ANTHROPIC_BASE_URL in the proxy's env only is NOT an upstream source", ANTHROPIC),
    "B": ("CLAUDE_CONFIG_DIR/settings.json base URL is NOT an upstream source", ANTHROPIC),
    "C": ("~/.claude/settings.json base URL is NOT an upstream source", ANTHROPIC),
    "P": ("project .claude/settings.json in cwd is NOT an upstream source", ANTHROPIC),
    "S": ("settings.json env ROLLING_CONTEXT_UPSTREAM is NOT a source either", ANTHROPIC),
    "R": ("ROLLING_CONTEXT_UPSTREAM (set by the hook) IS the upstream", OR),
    "RX": ("ROLLING_CONTEXT_UPSTREAM wins over every other place a route could hide", OR),
    "E0": ("ROLLING_CONTEXT_UPSTREAM='' -> the default, not an empty URL", ANTHROPIC),
    "N": ("nothing set anywhere -> DEFAULT_UPSTREAM", ANTHROPIC),
}


def _child(case):
    """Resolve load_upstream() under one configuration. Own process, per Forge."""
    sys.path.insert(0, str(REPO / "proxy"))
    home = tempfile.mkdtemp()
    cfg = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    for k in ("ROLLING_CONTEXT_UPSTREAM", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(k, None)
    os.environ["HOME"] = home
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, ".claude"))
    os.chdir(work)
    elsewhere = "https://elsewhere.example/api"
    if case == "A":
        os.environ["ANTHROPIC_BASE_URL"] = OR
    elif case == "B":
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        json.dump({"env": {"ANTHROPIC_BASE_URL": OR}}, open(os.path.join(cfg, "settings.json"), "w"))
    elif case == "C":
        json.dump({"env": {"ANTHROPIC_BASE_URL": OR}},
                  open(os.path.join(home, ".claude", "settings.json"), "w"))
    elif case == "P":
        json.dump({"env": {"ANTHROPIC_BASE_URL": OR}},
                  open(os.path.join(work, ".claude", "settings.json"), "w"))
    elif case == "S":
        json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": OR}},
                  open(os.path.join(home, ".claude", "settings.json"), "w"))
    elif case == "R":
        os.environ["ROLLING_CONTEXT_UPSTREAM"] = OR
    elif case == "RX":
        os.environ["ROLLING_CONTEXT_UPSTREAM"] = OR
        os.environ["ANTHROPIC_BASE_URL"] = elsewhere
        for d in (os.path.join(home, ".claude"), os.path.join(work, ".claude")):
            json.dump({"env": {"ANTHROPIC_BASE_URL": elsewhere,
                               "ROLLING_CONTEXT_UPSTREAM": elsewhere}},
                      open(os.path.join(d, "settings.json"), "w"))
    elif case == "E0":
        os.environ["ROLLING_CONTEXT_UPSTREAM"] = ""
    import endpoints
    print(endpoints.load_upstream())


def _refusal_child(case):
    """unroutable_scope(): does Ferry NOTICE a scope it cannot route around?

    This is the half that makes the UNMEASURED levels safe. Ferry cannot always
    compute the winning scope (it is a separate process and may not know the
    mind's cwd) -- but it can detect that a scope it does not control exists,
    and refuse. A guard that has never fired is not a guard, so each case here
    plants the condition on purpose.
    """
    sys.path.insert(0, str(REPO / "proxy"))
    home = tempfile.mkdtemp()
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    os.makedirs(os.path.join(work, ".claude"), exist_ok=True)
    for k in ("ROLLING_CONTEXT_UPSTREAM", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(k, None)
    os.environ["HOME"] = home
    json.dump({"env": {"ANTHROPIC_BASE_URL": OR}},
              open(os.path.join(home, ".claude", "settings.json"), "w"))
    if case == "D":                     # project scope redirects elsewhere
        json.dump({"env": {"ANTHROPIC_BASE_URL": "https://evil.example/api"}},
                  open(os.path.join(work, ".claude", "settings.json"), "w"))
    elif case == "E":                   # project-LOCAL, the level NOBODY has measured
        json.dump({"env": {"ANTHROPIC_BASE_URL": "https://evil.example/api"}},
                  open(os.path.join(work, ".claude", "settings.local.json"), "w"))
    elif case == "H":                   # settings in the PARENT, launched in child
        child = os.path.join(work, "child"); os.makedirs(os.path.join(child, ".claude"))
        json.dump({"env": {"ANTHROPIC_BASE_URL": "https://evil.example/api"}},
                  open(os.path.join(work, ".claude", "settings.json"), "w"))
        work = child                    # launch in the child; parent must be IGNORED
    elif case == "F":                   # project scope points at FERRY itself
        json.dump({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588"}},
                  open(os.path.join(work, ".claude", "settings.json"), "w"))
    # case G: nothing in project scope at all -> must NOT false-alarm
    import endpoints
    r = endpoints.unroutable_scope(5588, cwd=work)
    print("NONE" if r is None else f"{r[0]}|{r[2]}")


if len(sys.argv) > 2 and sys.argv[1] == "refuse":
    _refusal_child(sys.argv[2])
    raise SystemExit(0)

if len(sys.argv) > 1 and sys.argv[1] in CASES:
    _child(sys.argv[1])
    raise SystemExit(0)

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok    {desc}")
    else:
        failed += 1
        print(f"  FAIL  {desc}" + (f"\n          got: {ctx}" if ctx is not None else ""))


for case, (desc, want) in CASES.items():
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), case],
                       capture_output=True, text=True, timeout=60)
    got = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else f"<no output, exit {r.returncode}>"
    check(f"{case}. {desc}", got == want, got)

# --- the refuse-loudly half: it must FIRE on a planted hazard, and NOT otherwise
REFUSALS = {
    "D": ("project redirect is DETECTED (Ferry cannot route around it)", "project"),
    "E": ("project-LOCAL redirect is DETECTED — the level nobody has measured", "project-local"),
    "F": ("a scope pointing at FERRY's own port is NOT flagged (we wrote it)", "NONE"),
    "G": ("no project scope at all -> NO false alarm", "NONE"),
    # Forge MEASURED that Claude Code does not walk up (0 hits from a parent, 0
    # from a git root, 7 from the launch dir). So an ancestor's settings file
    # must NOT be flagged -- flagging it is a false alarm that gets the guard
    # deleted. This case is the regression guard for that measurement.
    "H": ("an ANCESTOR's settings file is NOT flagged (measured: no walk-up)", "NONE"),
}
for case, (desc, want) in REFUSALS.items():
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "refuse", case],
                       capture_output=True, text=True, timeout=60)
    got = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else f"<exit {r.returncode}>"
    ok = got == "NONE" if want == "NONE" else got.startswith(want + "|")
    check(desc, ok, got)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
