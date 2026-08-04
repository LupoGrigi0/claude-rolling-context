---
description: Turn rolling-context off for this session (or --global for the whole machine) — full history goes upstream uncompressed, no restart needed
---

Run this command and show the user its output verbatim, EXCEPT the
`<<rolling-context:...>>` marker line, which is machinery — do not repeat it
back, quote it, or explain it. Add no commentary beyond one short line if
something looks wrong.

With no arguments this turns compression off for the current conversation only.
If the user asked to turn it off everywhere rather than just here, append
`--global`.

Arguments given by the user: $ARGUMENTS

```
python "${CLAUDE_PLUGIN_ROOT}/proxy/switch.py" off $ARGUMENTS
```
