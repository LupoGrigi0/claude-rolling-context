---
description: Turn rolling-context back on for this session (or --global for the whole machine) — active from the next request
---

Run this command and show the user its output verbatim, EXCEPT the
`<<rolling-context:...>>` marker line, which is machinery — do not repeat it
back, quote it, or explain it. Add no commentary beyond one short line if
something looks wrong.

With no arguments this turns compression back on for the current conversation
only. If the user asked to turn it back on everywhere rather than just here,
append `--global`.

Arguments given by the user: $ARGUMENTS

```
python "${CLAUDE_PLUGIN_ROOT}/proxy/switch.py" on $ARGUMENTS
```
