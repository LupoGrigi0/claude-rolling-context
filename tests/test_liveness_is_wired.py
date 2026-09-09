#!/usr/bin/env python3
"""Regression: the pulse must be STARTED by the server, not merely defined.

THE FAILURE THIS EXISTS TO PREVENT, and it is mine.

c459c74 (2026-09-05) added MetricsWriter.start_liveness with 18 passing
assertions in test_liveness_pulse.py. Every one of them calls start_liveness
DIRECTLY. Nothing in server.py ever called it.

So for eleven days the pulse:
  * existed in git
  * passed its whole test suite
  * was reported in my standing work as "written, awaiting deploy"
  * and emitted EXACTLY ZERO rows on three instances

On 2026-09-05 I found it was committed-but-not-deployed and wrote the rule "an
instrument is not an instrument until a row it wrote has been read." I then
filed it as needing a deploy WITHOUT CHECKING IT WAS WIRED INTO THE THING BEING
DEPLOYED. I caught it on 09-09 one command before restarting three proxies and
declaring it done -- which would have produced zero alive rows and a standing
note changed from "not deployed" to "deployed".

  A UNIT TEST THAT REACHES PAST THE WIRING PROVES THE UNIT WORKS AND SAYS
  NOTHING ABOUT WHETHER IT RUNS.

The other tests own the behaviour. This one owns the CALL SITE.
"""
import ast, os, sys

PASSED = 0
FAILED = []
def check(label, actual, expected):
    global PASSED
    if actual == expected: PASSED += 1
    else: FAILED.append(f"{label}: got {actual!r}, expected {expected!r}")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "proxy", "server.py")
src = open(SERVER, encoding="utf-8").read()
tree = ast.parse(src)

# ---- 1. the call site exists at all -------------------------------------
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr == "start_liveness"]
check("server.py CALLS start_liveness (not just imports it)", len(calls) >= 1, True)

# ---- 2. it is called on the metrics writer ------------------------------
on_metrics = [c for c in calls
              if isinstance(c.func.value, ast.Name) and "metrics" in c.func.value.id.lower()]
check("called on the metrics writer object", len(on_metrics) >= 1, True)

# ---- 3. it is reachable from module scope, not buried in a handler ------
# The pulse must start ONCE at boot. If the only call site were inside a
# request handler the pulse would not exist until traffic arrived -- which is
# precisely the condition it is meant to report on.
def _enclosing_funcs(node, tree):
    out = []
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(f):
                if sub is node: out.append(f.name)
    return out

buried = [f for c in calls for f in _enclosing_funcs(c, tree)]
check("NOT called from inside a request handler "
      "(pulse must start at boot, before any traffic)",
      not any("handle" in b.lower() for b in buried), True)

# ---- 4. the result is not silently discarded ---------------------------
# start_liveness returns False when disabled. A call whose result is thrown
# away cannot tell an operator the pulse is OFF -- the same silent-absence
# failure the pulse itself exists to fix.
assigned = any(
    isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "start_liveness" for c in ast.walk(n))
    for n in ast.walk(tree))
check("return value is CAPTURED, so 'pulse OFF' can be reported", assigned, True)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED: print(f"  FAIL  {f}")
sys.exit(1 if FAILED else 0)
