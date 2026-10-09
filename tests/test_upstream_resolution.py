#!/usr/bin/env python3
"""Ship card F22 — Ferry must resolve the mind's REAL upstream, not the one
that happens to be true because $HOME/.claude is where we always put it.

*** THIS SUITE FAILS TODAY. THE FAILURE IS THE POINT. ***
F22 is FALSE on the card. A failing test is the honest evidence for a FALSE
row; a green suite around a known bug is not. It will pass when the resolver
is fixed, and NOT BEFORE. Do not skip it, do not allow-list it, do not "fix
the test" -- the test is right and the resolver is wrong.

PROBE AUTHORED BY Forge-ba0e, 2026-10-09, verbatim apart from two adaptations
he specified: point sys.path at this repo's proxy/ rather than his clone, and
run each case in its OWN PROCESS because it mutates os.environ and reloads
endpoints. Also committed on his side as BlackWolf-Forge
scripts/ferry_upstream_probe.py (4591a78). I asked for his probe instead of
writing my own: twice in twelve hours I guessed at behaviour I could have
read, and a second artefact only has to be kept in agreement with the first.

WHAT HE MEASURED (throwaway HOME, pure calls -- starts nothing, sends nothing).
The mind's real backend is OpenRouter in all three cases, so OR is always the
correct answer:

    A. backend in the launcher env only          -> api.anthropic.com   WRONG
    B. backend in CLAUDE_CONFIG_DIR/settings.json -> api.anthropic.com   WRONG
    C. control: backend in ~/.claude/settings.json -> openrouter.ai/api   right

C IS OUR HOUSE CONVENTION, WHICH IS WHY THIS PASSED EVERYWHERE WE LOOKED.
Forge's own rule: if every environment you can reach makes two things
identical, you do not have a test -- you have a tautology wearing a pass.

WHY IT MATTERS MORE THAN PORTABILITY. In case A the hook writes the proxy URL
into ~/.claude/settings.json, settings.json beats the process env (measured),
so the mind IS routed through Ferry -- and Ferry then resolves upstream to
Anthropic. A mind meant for OpenRouter sends its conversation AND ITS KEY to
the wrong provider before anything 401s. That is the deny-list broken by
plumbing, and the exact inverse of the PII use case Ferry is asked to serve.
In case B the hook writes a file Claude Code never reads, so the mind is never
routed through Ferry at all WHILE THE HOOK LOGS SUCCESS -- that is F23.

ALSO UNHANDLED (Forge, not covered by these three cases): project-scope
`.claude/settings.json` and `settings.local.json` in the launch directory
OUTRANK user settings in Claude Code. A project-level ANTHROPIC_BASE_URL
bypasses Ferry identically, and you cannot route around a higher-precedence
scope -- the resolver must refuse to start, loudly.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OR = "https://openrouter.ai/api"

CASES = {
    "A": "backend in the launcher env only",
    "B": "backend in CLAUDE_CONFIG_DIR/settings.json",
    "C": "control: backend in ~/.claude/settings.json (our house convention)",
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
    if case == "A":
        os.environ["ANTHROPIC_BASE_URL"] = OR
    elif case == "B":
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        json.dump({"env": {"ANTHROPIC_BASE_URL": OR}}, open(os.path.join(cfg, "settings.json"), "w"))
    elif case == "C":
        json.dump({"env": {"ANTHROPIC_BASE_URL": OR}},
                  open(os.path.join(home, ".claude", "settings.json"), "w"))
    import endpoints
    print(endpoints.load_upstream())


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


for case, desc in CASES.items():
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), case],
                       capture_output=True, text=True, timeout=60)
    got = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else f"<no output, exit {r.returncode}>"
    check(f"{case}. {desc} -> resolves the mind's REAL upstream", got.startswith(OR), got)

print(f"\n{passed} passed, {failed} failed")
if failed:
    print("EXPECTED TODAY: F22 is FALSE on the ship card. A and B are the bug, not a "
          "broken test.\n  Fix shape (Forge): resolve CLAUDE_CONFIG_DIR or else ~/.claude; "
          "honour the process\n  env's ANTHROPIC_BASE_URL and never accept our own port; "
          "REFUSE TO START, loudly, if a\n  higher-precedence scope sets it. Then F23's "
          "transaction canary proves routing per mind.")
sys.exit(1 if failed else 0)
