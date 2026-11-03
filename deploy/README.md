# deploy/ — supervised Ferry (ship item S5)

`hacs-ferry@.service` runs one mind's Ferry proxy under systemd. Instance = the
mind's unix user, exact case per `id`. Modelled on `hacs-doorbell@.service`.
**Root installs it. Nothing here installs itself.**

## What the unit does

- Runs as `User=%i`, from the **pinned checkout** `FERRY_HOME` (one line in the
  unit, default `/usr/local/lib/ferry`). That is a release copy root owns —
  **never a working tree a mind edits**, or a `git checkout` there silently
  changes what the proxy loads on its next restart.
- Reads config from `~USER/preferences.json`, key `"ferry"`, through
  `hooks/ferry_config.py` — the same resolver the SessionStart hook and
  `install.sh` use. One declared source.
- Optional root-owned overrides: `/etc/ferry/USER.env` (`KEY=value` lines). Use it
  for `CLAUDE_CONFIG_DIR` if the mind has one, and for tuning knobs
  (`ROLLING_CONTEXT_TARGET`, `ROLLING_CONTEXT_TRIGGER`, ...): **the unit's proxy
  does not read settings.json `env`** — only a hook-started proxy inherits it
  from Claude Code. Env beats preferences.json.
- Sets `FERRY_SUPERVISED=1`. `Restart=on-failure`, every 10 s, never gives up.
- `ExecStartPost` runs `tools/ferry-canary --wait 30 --pid $MAINPID`: the start
  only counts if the port is held by the unit's own process, which echoes a
  nonce, names **this** mind's data dir, is not in
  thrash lockout, and forwards a real request upstream answers. A failed canary
  fails the start and the unit retries.
- Proxy output goes to its rotating debug log, not the journal (tracebacks still
  reach the journal via stderr).

## Before installing — the mind's `~/preferences.json`

```json
{"ferry": {"port": 5701, "data_dir": "/home/USER/ferry-data",
           "curation": "ferry", "supervised": true}}
```

`port` is required and must be unique per mind (no shared 5588).
`"curation": "ferry"` must be declared: only `install.sh` defaults it, so a unit
from preferences alone would otherwise run native (LLM-summary) mode.
**`"supervised": true` is required**: without it the SessionStart hook also
starts a proxy and the two fight for the port. The file may be root-owned and
read-only to the mind.

## Install (root — Bastion)

```sh
# 1. Pin the code: a release copy, not a working tree.
install -d -o root -g root -m 0755 /usr/local/lib/ferry
git -C /PATH/TO/REVIEWED/CLONE archive <commit> | tar -x -C /usr/local/lib/ferry
chmod 0755 /usr/local/lib/ferry/tools/ferry-canary

# 2. The unit.
install -o root -g root -m 0644 /usr/local/lib/ferry/deploy/hacs-ferry@.service \
        /etc/systemd/system/hacs-ferry@.service
systemctl daemon-reload

# 2b. The canary timer (S7: a later thrash lockout must reach a human).
install -o root -g root -m 0644 /usr/local/lib/ferry/deploy/hacs-ferry-canary@.service \
        /usr/local/lib/ferry/deploy/hacs-ferry-canary@.timer /etc/systemd/system/
systemctl daemon-reload

# 3. Optional overrides for this mind (only if it needs any).
install -d -m 0755 /etc/ferry
printf 'CLAUDE_CONFIG_DIR=/home/USER/.claude\n' > /etc/ferry/USER.env   # example
chmod 0644 /etc/ferry/USER.env

# 4. Migrating a mind whose proxy the SessionStart hook started? Stop THAT one
#    first, or the unit cannot bind (its canary's --pid check fails it loudly).
#    Pidfile: $CLAUDE_CONFIG_DIR (else ~USER/.claude)/rolling-context-proxy.pid.
#    Verify /proc/<pid>/cmdline is server.py before `kill <pid>`; never by pattern.

# 5. Start it. Exact case per `id`.
systemctl enable --now hacs-ferry@USER
systemctl enable --now hacs-ferry-canary@USER.timer
```

## Verify (by acting, not by `active`)

```sh
systemctl status hacs-ferry@USER          # active (running); ExecStartPost status=0
runuser -u USER -- /usr/bin/python3 /usr/local/lib/ferry/tools/ferry-canary
#   ferry-canary: OK: port 5701 is this mind's proxy (...), no thrash lockout, upstream answered HTTP 401
```

`HTTP 401` is expected: the canary carries no credentials. Exit codes: 0 OK,
1 FAIL (we looked; it is wrong), 2 COULD-NOT-LOOK. Then point the mind at it —
`ANTHROPIC_BASE_URL=http://127.0.0.1:PORT` in its settings.json (`install.sh`
does this) — and relaunch the mind.

## Canary on a timer (required for S7)

`ExecStartPost` only proves the start. A thrash lockout (curation disabled, the
mind back on native compaction) or a dead upstream appears LATER, and only a
canary run after the fact sees it. `hacs-ferry-canary@.service` + `.timer` run
it every 15 min; a failure shows in `systemctl --failed`. Installed in step 2b
above. Keep its `FERRY_HOME` the same as the proxy unit's.

## Upgrade / roll back the code

Replace `/usr/local/lib/ferry` with another reviewed commit, then
`systemctl restart hacs-ferry@USER`. The canary re-proves it. To take a mind
off Ferry entirely, see `docs/RUNBOOK.md`.

## Verified (2026-11-03, Crossing-2d23, no root)

- `systemd-analyze verify` on an instance of the unit: exit 0; a planted bad
  `ExecStart` path in a copy: exit 1.
- The `ExecStart` / `ExecStartPost` lines, run exactly as systemd expands them
  (`$$`→`$`) as an unprivileged user against a stub upstream: canary exit 0; with
  no port configured, ExecStart exits 1 naming the missing port.
- **Not verified:** an actual `systemctl enable` — that needs root.
