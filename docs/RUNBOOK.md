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

> **The emergency off-switch for Ferry is to SET ANTHROPIC_BASE_URL to https://api.anthropic.com (or your real upstream). Never DELETE it: a running session ignores a deleted key and keeps sending to the dead port.**

1. Run `bash ~/claude-rolling-context/uninstall.sh` (it replaces only what Ferry wrote with your
   real route: `https://api.anthropic.com`, or a declared non-Anthropic `ferry.upstream`).
   By hand: SET `env.ANTHROPIC_BASE_URL` to `https://api.anthropic.com`. Do NOT delete it:
   a running session ignores the deletion (measured 2026-11-26, 14 h of ECONNREFUSED).
   **Not on Anthropic?** Set `env.ANTHROPIC_BASE_URL` to the `ferry.upstream` value instead of deleting — deleting sends that key to Anthropic.
2. Stop the hook re-pointing it next session: uninstall (above), or remove Ferry's
   `SessionStart` entry from `S` (`hooks` → the command running `.../hooks/start-proxy.sh`).
3. **No restart needed, because step 1 SETS the key:** Claude Code picks up a set or changed
   value live (install: 25 s on Linux 2026-11-16, 8 s on Windows 2026-11-25; a set to
   api.anthropic.com: seconds, Crossing 2026-11-26). It does NOT pick up a deletion. n=1 per
   direction, Claude Code 2.1.285; if in doubt, read the receiver (metrics.csv stops growing). Never `claude --bg --resume` a running session — it forks.

Restore later: `bash install.sh` again, canary. No restart.

## 3. Restart the proxy — the mind can do this itself

Ferry is its own process. Claude Code only knows `127.0.0.1:<port>`, which does
not change when the proxy restarts, and curation state is on disk — so **the
mind does not need landing.** Run from the mind's own Bash tool, nothing is in
flight (its last request finished; its next leaves after this returns):

```sh
~/repos/claude-rolling-context/bin/ferry-restart
```

It stops only **this user's** Ferry `server.py` on the configured port (anything
else on the port is refused, never killed), starts the new proxy through the
hook, verifies the port, and runs the canary. Exit 0 = serving, in Ferry mode.
Use it after `git pull`, or after editing the `ferry` block. Never `pkill -f
server.py` — that kills other minds' proxies.

**"does not match preferences.json"** at session start: you changed `ferry.upstream`
or `data_dir`, but the running proxy still has the old one. The hook has taken
Ferry's pointer back (the session runs direct). Run `ferry-restart`.

### The watermark

`~/preferences.json` → `"ferry"`: `trigger` (default `800000`) is when Ferry
curates; `target` (default `600000`) is about where it curates down to. Must be
`target < trigger`. Edit, then `ferry-restart`. Check what is running:

```sh
curl -s 127.0.0.1:<port>/health | grep -oE '"(curation_mode|trigger_tokens|target_tokens)": [^,]+'
```
`curation_mode` must be `"ferry"`. Anything else **summarises** (paraphrases).

### File modes are owner-only on purpose
Every file Ferry's scripts create -- ~/ferry-data/*, the watchdog log and lock,
and `settings.json` when Ferry rewrites it -- is 0600 (dirs 0700). That includes
`settings.json` going from 0644 to 0600: the same user reads it, so it is
harmless and intentional. **Do not "fix" it back.** (Forge, 2026-11-27.)

### Exit codes, so a 3am log reads right
| tool | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| `tools/ferry-canary` | OK | FAIL (Ferry broken) | COULD-NOT-LOOK | -- | UPSTREAM-DOWN (Ferry fine; wait) |
| `bin/ferry-restart` | serving new code | WITHDRAWN (mind direct, uncurated) | -- | ROLLED BACK (serving old commit) | -- |

### Automatic repair (tools/ferry-watchdog, run from the mind's own cron)
`*/5 * * * * <clone>/tools/ferry-watchdog` -- no `>> log`: it writes its own
`~/ferry-data/watchdog.log`, owner-only (a shell redirect would create it world-readable).
- **Healthy:** silent. **Upstream down** (canary exit 4, `UPSTREAM-DOWN`): it
  WAITS -- a network blip is not Ferry's fault and never triggers a restart.
- **Ferry broken:** `ferry-restart` restarts; if the new code fails it **rolls
  back** to the last good commit; if nothing works it **withdraws** Ferry (the
  mind goes direct, uncurated). Each outcome ALERTS the mind and `alert_to`.
- **After a rollback** the mind runs fine on an OLD commit, on a detached HEAD,
  and looks healthy. The watchdog alerts once; recover with
  `git -C <clone> checkout master && <clone>/bin/ferry-restart`.

## 4. Thrash lockout ("curation disabled")

The unevictable floor (system prompt + tools + CLAUDE.md) is above `target`;
Ferry passes requests through untouched and clears itself when the context
changes enough. Not an emergency. `grep -n "CURATION DISABLED" <debug log>`
names the floor. Fix: raise `target` in the `ferry` block above it, then
`ferry-restart`.

## 5. Install / hook refuses: "routes this mind to ..."

The mind is already routed somewhere that may not be Anthropic (a non-Ferry
`ANTHROPIC_BASE_URL` in `S` or the launcher env, an `apiKeyHelper`, or an
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`) and `ferry.upstream` is not declared.
The message prints the exact line to add to the `"ferry"` block — add it, run
again. Ferry never edits `preferences.json` itself.
