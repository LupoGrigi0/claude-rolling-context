#!/usr/bin/env python3
"""_safe_cut — Ferry must not silently decline to curate.

THE INCIDENT (2026-08-25): three peer instances doing real project work hit
the 30,000-token trigger five times between them and evicted NOTHING. One
reached 129,278 tokens while Ferry watched. The proxy logged
"Not enough old messages to compress, passing through" at INFO and carried on.

CAUSE: eviction may not strand a tool_result whose tool_use is gone, so the
cut point walks BACKWARD to a legal boundary — a plain user turn whose
predecessor carries no tool_use. Tool-dense work (read, run, edit, repeat)
produces long unbroken runs with no such boundary, so the walk crossed the
whole conversation and gave up.

Ferry degraded to a no-op exactly under the traffic that needs it most.

Every test below is about one rule: WHEN IN DOUBT, EVICT MORE — never nothing.
Run: python3 tests/test_safe_cut.py
Crossing-2d23 <crossing-2d23@smoothcurves.nexus>. Stdlib only.
"""
import os, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "proxy"))
os.environ.setdefault("ROLLING_CONTEXT_CURATION", "ferry")
os.environ.setdefault("FERRY_DATA", tempfile.mkdtemp())
from compressor import RollingCompressor  # noqa: E402

passed = 0
failed = []
def check(name, cond, detail=None):
    global passed
    if cond: passed += 1; print(f"  PASS  {name}")
    else: failed.append(name); print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))

C = RollingCompressor()

def user(t="hello"):                 return {"role": "user", "content": t}
def asst(t="ok"):                    return {"role": "assistant", "content": t}
def asst_tool():                     return {"role": "assistant",
                                             "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}
def user_result():                   return {"role": "user",
                                             "content": [{"type": "tool_result", "tool_use_id": "t", "content": "out"}]}

print("backward search (preferred — keeps MORE than target):")
m = [user(), asst(), user("clean"), asst(), user(), asst()]
check("finds a clean boundary walking back", C._safe_cut(m, 4, 0) == 4, C._safe_cut(m, 4, 0))
check("walks back past an assistant turn to the user turn below it",
      C._safe_cut(m, 3, 0) == 2, C._safe_cut(m, 3, 0))

print("\nthe observed failure shape — an unbroken tool run:")
# read/run/edit/repeat: no plain user turn anywhere in the middle.
tool_run = [user("start")] + [asst_tool(), user_result()] * 6 + [user("finally"), asst()]
old_style_backward_only = 0
i = 12
while i > 0:
    mm = tool_run[i]
    if (mm.get("role") == "user" and not C._has_tool_result(mm)
            and not C._has_tool_use(tool_run[i-1])):
        old_style_backward_only = i; break
    i -= 1
check("backward-only search finds NOTHING in a tool run (the bug)",
      old_style_backward_only == 0, old_style_backward_only)
cut = C._safe_cut(tool_run, 12, 0)
check("_safe_cut now searches FORWARD and returns a real cut instead of the floor",
      cut > 0, cut)
check("the forward cut is a LEGAL boundary (plain user turn, no tool_result, "
      "predecessor carries no tool_use)",
      cut > 0
      and tool_run[cut].get("role") == "user"
      and not C._has_tool_result(tool_run[cut])
      and not C._has_tool_use(tool_run[cut - 1]), cut)
check("forward means we keep LESS than asked — evicting MORE is the right way "
      "to be wrong; evicting nothing disables the whole mechanism",
      cut > 12, (cut, 12))

print("\nsafety rails:")
check("never keeps zero messages", C._safe_cut(tool_run, len(tool_run) - 1, 0) < len(tool_run))
nowhere = [user("only"), asst_tool(), user_result()]
check("genuinely no legal cut returns the floor (caller passes through) "
      "rather than inventing an illegal one",
      C._safe_cut(nowhere, 2, 0) == 0, C._safe_cut(nowhere, 2, 0))
check("a cut at index 0 is never legal — the prefix must precede a user turn",
      C._safe_cut([user(), user()], 0, 0) == 0)

print(f"\n{passed} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
