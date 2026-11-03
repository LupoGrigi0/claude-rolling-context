# Ferry runbook — one page, for 3am

`USER` = the mind's unix user, exact case per `id`. Run as root unless noted.
**A mind whose proxy is dead cannot fix itself — its API calls are the broken thing.**

## 1. Is Ferry broken for this mind?

```sh
runuser -u USER -- /usr/bin/python3 /usr/local/lib/ferry/tools/ferry-canary; echo "exit=$?"
```

| Output | Meaning | Go to |
|---|---|---|
| `OK: ... upstream answered HTTP 401` | Fine. 401 is expected (the canary sends no key). | — |
| `FAIL: nothing listening` | Proxy dead. Mind's calls fail (connection refused). | §3, else §2 |
| `FAIL: ... another mind's proxy` | Wrong proxy on this mind's port. | §2, tell Crossing/Bastion |
| `FAIL: ... could not reach its upstream (HTTP 502)` | Proxy up, API unreachable from it. | check network; §2 if it persists |
| `FAIL: curation disabled (thrash lockout)` | Mind is passing through **uncurated** — native compaction will hit it. Not an outage. | §4 |
| `COULD-NOT-LOOK: config: ...` | `~USER/preferences.json` "ferry" block is missing/broken. | fix it, §3 |

Other signs: the mind reports `API Error` / connection errors on every turn (proxy
dead); `systemctl status hacs-ferry@USER` is `failed` or restarting.

## 2. Roll the mind back to direct Anthropic (no Ferry)

The switch is `env.ANTHROPIC_BASE_URL` in the mind's **user settings.json**:
`$CLAUDE_CONFIG_DIR/settings.json` if the mind has `CLAUDE_CONFIG_DIR` (check
`/etc/ferry/USER.env` and its launcher), else `~USER/.claude/settings.json`.

**If `env.ROLLING_CONTEXT_UPSTREAM` is set, the mind was going somewhere else
before Ferry (e.g. OpenRouter): restore THAT, do not delete.** Deleting would
send an OpenRouter key to Anthropic. Run as the user so ownership is kept:

```sh
S=/home/USER/.claude/settings.json        # or $CLAUDE_CONFIG_DIR/settings.json
runuser -u USER -- cp "$S" "$S.pre-rollback"
runuser -u USER -- python3 - "$S" <<'EOF'
import json, sys
p = sys.argv[1]; s = json.load(open(p)); env = s.setdefault("env", {})
up = env.get("ROLLING_CONTEXT_UPSTREAM")
if up: env["ANTHROPIC_BASE_URL"] = up
else: env.pop("ANTHROPIC_BASE_URL", None)
json.dump(s, open(p, "w"), indent=2); print("ANTHROPIC_BASE_URL ->", env.get("ANTHROPIC_BASE_URL", "(default Anthropic)"))
EOF
```

Then:
- Also check the mind's project settings (`<cwd>/.claude/settings.json`,
  `settings.local.json`) and its launcher env for `ANTHROPIC_BASE_URL` — any of
  them can still point at Ferry.
- **Relaunch the mind** so it reads the change (not measured whether a running
  session picks it up — assume it does not). v2: chassis `land.sh`, then
  `resume-stopped.sh <full-uuid>`. **Never `claude --bg --resume` on a running
  session — it forks.**
- Keep Ferry from re-pointing it: supervised minds — `systemctl disable --now
  hacs-ferry@USER`. Hook-mode minds (no unit) — the SessionStart hook re-points
  on every start; run `uninstall.sh` as the user instead of the edit above (it
  does the same restore, honours `CLAUDE_CONFIG_DIR`, never touches the archive).

**Restore Ferry later:** `runuser -u USER -- cp "$S.pre-rollback" "$S"`, start
the unit (§3), canary, relaunch the mind.

## 3. Stop / start / restart the unit

```sh
systemctl status  hacs-ferry@USER
systemctl restart hacs-ferry@USER       # start succeeds only if the canary passes
systemctl stop    hacs-ferry@USER       # the mind's calls fail until rolled back (§2)
systemctl disable --now hacs-ferry@USER
```

**Restarting a proxy is Lupo's call** — the mind is mid-session on it. Never kill
by pattern (`pkill -f server.py` kills other minds' proxies); use the unit, or
check `/proc/<pid>/cmdline` and the port first.

## 4. Thrash lockout ("curation disabled")

Ferry tried three times and could not get the context under target: the
unevictable floor (system prompt + tools + CLAUDE.md) is above
`ROLLING_CONTEXT_TARGET`. It now passes requests through untouched and clears
itself if the context changes enough. Not an emergency; the mind will meet
native compaction instead. The log line names the floor:
`grep -n "CURATION DISABLED" <debug log>`. Fix = raise the target above that
floor and restart (§3). Supervised: `ROLLING_CONTEXT_TARGET=...` in
`/etc/ferry/USER.env` — the unit's proxy does **not** read settings.json `env`.
Hook-mode: settings.json `env.ROLLING_CONTEXT_TARGET`.

## 5. Where the logs are

| What | Where |
|---|---|
| Proxy debug log (rotates, 10 MB × 3) | `~USER/.claude/rolling-context-debug-<basename of data_dir>.log`, `.1`–`.3` — `~/.claude` if it exists, even with `CLAUDE_CONFIG_DIR`; else `$CLAUDE_CONFIG_DIR` |
| Unit start failures, canary output, tracebacks | `journalctl -u hacs-ferry@USER` |
| Metrics (one row per curation/gate/error) | `<data_dir>/metrics.csv` |
| Archive (never delete) | `<data_dir>/` |
| Hook-mode only | `$CLAUDE_CONFIG_DIR/rolling-context-hook.log`, `rolling-context-proxy.log` (not rotated) |

`data_dir` = `"ferry"."data_dir"` in `~USER/preferences.json`, default `~USER/ferry-data`.

## 6. Before the FIRST restart of a live mind on this code

- **Archive its debug log if it is over 10 MB** — with the proxy stopped,
  `mv L L.pre-rotation && gzip L.pre-rotation` (as the older `.log.gz` were). Rotation is new: the first write renames an oversize log to `.1`,
  and three rollovers (~30 MB) later that history is deleted.
- **Hook-mode minds: the SessionStart hook now fails LOUD.** No declared port, or
  a port held by a proxy it did not start (no `rolling-context-proxy.pid`), puts
  `[FERRY] ERROR` in the mind's context and withdraws its settings.json pointer.
  Check each mind's pidfile and port first, or declare `"supervised": true`.
- **Curation mode is no longer defaulted by the hook** — only `install.sh` sets
  `ferry`. A mind that never ran it keeps the mode it had.
