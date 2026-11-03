# Ferry runbook — one page, for 3am

Ferry runs **as the mind's own user**, started by Claude Code's SessionStart
hook. No root, no service, nothing in `/etc`. `USER` = the mind's unix user.
Run every command below **as that user** (`sudo -u USER -i`, or from the mind).
**A mind whose proxy is dead cannot fix itself — its API calls are the broken thing.**

## 0. Where everything is

| What | Where |
|---|---|
| Config — the ONLY source | `~USER/preferences.json`, key `"ferry"`: `port` (required), `upstream` (default `https://api.anthropic.com`), `data_dir` (default `~/ferry-data`) |
| The switch Claude Code reads | `env.ANTHROPIC_BASE_URL` in `$CLAUDE_CONFIG_DIR/settings.json` if the mind has `CLAUDE_CONFIG_DIR`, else `~USER/.claude/settings.json` (call it `S`) |
| Hook log, proxy stdout, pidfile | `rolling-context-hook.log`, `rolling-context-proxy.log`, `rolling-context-proxy.pid` next to `S` |
| Proxy debug log (rotates, 10 MB × 3) | `~USER/.claude/rolling-context-debug-<basename of data_dir>.log` (in the dir of `S` if `~/.claude` does not exist) |
| Metrics / archive (never delete) | `<data_dir>/metrics.csv`, `<data_dir>/` |

## 1. Is Ferry broken for this mind?

```sh
python3 ~/claude-rolling-context/tools/ferry-canary; echo "exit=$?"
```

| Output | Meaning | Go to |
|---|---|---|
| `OK: ... upstream answered HTTP 401` | Fine. 401 is expected (the canary sends no key). | — |
| `FAIL: nothing listening` | Proxy dead. Mind's calls fail (connection refused). | §3, else §2 |
| `FAIL: ... another mind's proxy` | Wrong proxy on this mind's port — two minds share a port. | §2, fix `ferry.port` |
| `FAIL: ... could not reach its upstream (HTTP 502)` | Proxy up; `ferry.upstream` unreachable from it. | check network / `upstream`; §2 if it persists |
| `FAIL: curation disabled (thrash lockout)` | Passing through **uncurated**. Not an outage. | §4 |
| `COULD-NOT-LOOK: config: ...` | `preferences.json` `"ferry"` missing/broken, or the route guard (§5). | fix it, §3 |

Also: `[FERRY] ERROR: ...` at the top of the mind's session is the hook failing — it says why.

## 2. Rollback: take the mind off Ferry

1. Remove Ferry's `ANTHROPIC_BASE_URL` from `S` — or run `bash ~/claude-rolling-context/uninstall.sh`
   (removes only what Ferry wrote; if a non-Anthropic `ferry.upstream` is declared it puts that route back).
   By hand: delete `env.ANTHROPIC_BASE_URL` and `env.ROLLING_CONTEXT_WROTE_BASE_URL`.
   **Not on Anthropic?** Set `env.ANTHROPIC_BASE_URL` to the `ferry.upstream` value instead of deleting — deleting sends that key to Anthropic.
2. Stop the hook re-pointing it next session: uninstall (above), or remove the `"ferry"` block / the plugin link `<dir of S>/plugins/rolling-context`.
3. **Restart the session** so Claude Code reads `S` again. Never `claude --bg --resume` a running session — it forks.

Restore later: `bash install.sh` again, canary, restart the session.

## 3. Restart the proxy

**Restarting a proxy is Lupo's call** — the mind is mid-session on it.
The next session start restarts it by itself (the hook). By hand, as the user:

```sh
P=<dir of S>/rolling-context-proxy.pid
cat /proc/$(cat $P)/cmdline | tr '\0' ' '     # MUST be ... server.py — check before any kill
kill $(cat $P)
bash ~/claude-rolling-context/hooks/start-proxy.sh   # starts, verifies the port, points S
```

Never kill by pattern (`pkill -f server.py` kills other minds' proxies).

**"does not match preferences.json"** at session start: you changed `ferry.upstream`
or `data_dir`, but the running proxy still has the old one. The hook has taken
Ferry's pointer back (the session runs direct). Restart the proxy (above).

## 4. Thrash lockout ("curation disabled")

The unevictable floor (system prompt + tools + CLAUDE.md) is above
`ROLLING_CONTEXT_TARGET`; Ferry passes requests through untouched and clears
itself when the context changes enough. Not an emergency. `grep -n "CURATION
DISABLED" <debug log>` names the floor. Fix: `env.ROLLING_CONTEXT_TARGET` above
it in `S`, then §3.

## 5. Install / hook refuses: "routes this mind to ..."

The mind is already routed somewhere that may not be Anthropic (a non-Ferry
`ANTHROPIC_BASE_URL` in `S` or the launcher env, an `apiKeyHelper`, or an
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`) and `ferry.upstream` is not declared.
The message prints the exact line to add to the `"ferry"` block — add it, run
again. Ferry never edits `preferences.json` itself.
