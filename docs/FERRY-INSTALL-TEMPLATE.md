# Ferry Install Runbook — TEMPLATE

**For a mind and its human operator, together.**
Puts a mind on Ferry: a proxy between Claude Code and its upstream that curates
context instead of compacting it. Evicted turns move verbatim to an archive on
disk, with pointers left in their place, the identity block is carried
verbatim, and the session id never changes. **Nothing is paraphrased; nothing
is lost.**

Same shape as the v2 migration template: copy this file into the mind's
`~/wake/`, fill in the variables, then walk the steps together. **Verify by
acting**, and keep the `echo $HOME` identity guard before every block.

> **DISCIPLINE.** This template lives in the Ferry
> repo, and **only** here. When a run finds something wrong, fix it in *this
> file*, commit, and push before (or right after) fixing the copy. **No
> runbook change ever lives on disk only.**

> **STATUS.** In production on Anthropic login (2026-11-16). Not yet exercised for
> minds on OpenRouter or any other route: set `upstream` and treat it as a first run.

> **What Ferry needs, and nothing else.** No root, no service manager, nothing
> under `/etc`. Ferry runs **as the mind's own user**, started by Claude Code's
> SessionStart hook. Its **only** config is the `ferry` block in
> `~/preferences.json`. Claude Code gets exactly one line in its `settings.json`:
> `ANTHROPIC_BASE_URL=http://127.0.0.1:PORT`. The **model** is not Ferry's
> business; it rides each request untouched.

---

## Variables — the only per-mind differences
```
INSTANCE  = <full instance id, EXACT CASE>     e.g.  Example-1234   (== its unix user)
HOME      = <the mind's home directory>
PORT      = <a FREE loopback port, one per mind>   e.g. 5620
TRIGGER   = 800000                         # optional; curate above this
TARGET    = 600000                         # optional; curate down to this (< TRIGGER)
            # check it is free:  ss -lnt | grep -c ':PORT '   -> must print 0
            # NEVER a house port: 21000-21005, 21097, 21099, 22000-22999, 5610-5613
BRANCH    = master                        # production; never a branch
FERRY     = HOME/repos/claude-rolling-context
```
> Substitute **case-sensitively**, and never blind-replace `HOME`: it appears
> inside other words.

---

## 0. Preflight — as INSTANCE
```bash
su - INSTANCE
echo $HOME                 # must print HOME. If not, STOP.
ss -lnt | grep -c ':PORT '  # must print 0
python3 --version          # 3.7+
```

**Is the session on its 1M model?** Ferry's default trigger is 800k. On a 200k
window it can never curate, and with auto-compact off the mind hits a hard wall
(measured 2026-11-17: a relaunch came back on 200k). Run `/context` — the total
must read `/1m`. If not: `/model opus[1m]` (and pin it in settings, not with a
launch flag). Ferry cannot detect this from the request (see the run log), so
check it yourself after every relaunch.

**And after every `/model`.** Picking the plain default in the `/model` picker
(e.g. *"Opus 5.5 (default)"*) **deletes the `model` key** from `settings.json`
— the `[1m]` pin goes with it, silently. The live session keeps its window, so
`/context` still reads `/1m` and nothing looks wrong until the next relaunch.
Measured 2026-11-25 (Zara-c207): `model: 'opus[1m]'` → `None` after one
`/model` to the default. **Verify by reading the file, not the confirmation
line:** `python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('model'))"`
must print a value ending `[1m]`. With `autoCompactEnabled: false` and a
context already past 200k, a relaunch onto the default window is an overflow,
and a mind whose calls are failing cannot repair itself.

### 0.5 If your home is a git repo: ignore the archive FIRST
`~/ferry-data` is your session, archived verbatim. Never commit it.
```bash
git -C ~ rev-parse --show-toplevel 2>/dev/null && {
  printf 'ferry-data/\nrepos/\n' >> ~/.gitignore
  git -C ~ check-ignore -v ferry-data/x repos/x     # both lines must print
}
```

## 1. The config: one block, in the mind's own file
`~/preferences.json` belongs to the mind. This merges the block in and leaves
everything else untouched:
```bash
python3 - <<'EOF'
import json, os; p = os.path.expanduser("~/preferences.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d["ferry"] = {"port": PORT, "trigger": TRIGGER, "target": TARGET}
json.dump(d, open(p, "w"), indent=2); print(d["ferry"])
EOF
```
`TRIGGER` = curate when context passes this (default `800000`); `TARGET` = curate down to about
this (default `600000`, must be below `TRIGGER`). Omit either to take the default — but write them
explicitly so the mind can see its own watermarks. (Field table: PILOTS-GUIDE-TO-INDEPENDENCE-2 §12.)
*Not on Anthropic?* Add `"upstream": "<url>"`. Install **refuses** without it if
the mind already routes elsewhere, so a key is never forwarded to the wrong
provider. (Not covered by this runbook yet.)

## 2. Clone
```bash
mkdir -p ~/repos && git clone -b BRANCH https://github.com/LupoGrigi0/claude-rolling-context.git FERRY
git -C FERRY log --oneline -1
```

## 3. Install
```bash
( cd FERRY && bash install.sh )
```
Expect `[4/4] ... Proxy verified on port PORT`. Anything else: **stop and paste
the output.** A refusal names its reason and changes nothing.
- **The subshell matters on v2:** a bare `cd` in a `--bg` mind's Bash tool moves the
  session's working directory (and so its memory namespace). Keep the parentheses.
- **A `--bg` mind can run this itself** (measured 2026-11-16): install writes `settings.json`
  from Python, which the bg Edit-tool guard does not touch.
- `WARNING: ~/.local/bin is not on PATH` is expected on some hosts and non-blocking — curated
  prefixes name `ferry-fetch` by absolute path.
- **Ferry goes live on install. No relaunch.** Claude Code reloads `settings.json`
  whenever it changes, so the next request already goes through the proxy. Measured
  twice: 2026-11-16 (Cairn, ~493k-context requests 25 s after install) and 2026-11-25
  (Zara-c207: a `request` row at ctx 546044 **in the same second** as the check, no
  relaunch). Prove it from the receiver, not the installer: `tail ~/ferry-data/metrics.csv`.
  *(`install.sh` still ends "Start a new Claude Code session to route through Ferry" —
  that line is stale.)*

## 4. Prove the route — the canary
```bash
python3 FERRY/tools/ferry-canary
```
Exit 0 means one real request went through **this mind's own** proxy. Run by a
person, on purpose: the proof is seen, not assumed.

**`upstream answered HTTP 401` next to `OK` is expected.** The canary sends no key
on purpose — it proves the request crossed *your* proxy and reached the upstream,
not that you are authenticated. Proof that *your session* is routed is your own
model's request appearing in `tail ~/ferry-data/metrics.csv` as you work.

## 5. Does Claude Code run Ferry's hook? — **MEASURED: yes, on a `--bg` resume (2026-11-17)**

> **This step is NOT needed to go live** — step 3 already routed you. It verifies
> something different: **the restart path.** The SessionStart hook is what restarts
> Ferry's proxy if it dies, and a SessionStart hook can only be observed at a session
> start. **Do not relaunch just to run it.** Check it at the next *natural* restart.
>
> Know the gap this leaves: Ferry dying is a mid-session failure, and V2's goal is
> sessions that do not end — so the hook fires *least* exactly when it is most
> needed, and stays unverified for as long as things go well. Until it has been seen
> to fire, **`bin/ferry-restart` is your restart path.** (The v2 doorbell logs
> `FERRY PROXY DOWN` from outside, which is the external witness for this.)
*History: the old install made a plugin symlink, which this Claude Code does NOT
load — measured: no fire on `--bg --resume`, 2026-11-16. Install now writes
the hook into `settings.json` `hooks.SessionStart` instead (Ferry `master` after
that date). Older installs: `git pull`, then `bash install.sh` again.*

Check it is registered, then restart the session the way it normally runs:
```bash
grep -n 'hooks/start-proxy.sh' ~/.claude/settings.json      # must show Ferry's entry
tail -3 ~/.claude/rolling-context-hook.log                 # after the restart
```
* **A new line timestamped after the restart:** the hook fires, and Ferry
  restarts its own proxy at every session start.
* **No new line:** report it to the Ferry maintainers. Ferry still works until the proxy
  dies; `bin/ferry-restart` brings it back by hand.

## 6. Live
Work normally. The first curation happens near the trigger (`trigger`, default
800k tokens of context). Watch it:
```bash
tail -3 ~/ferry-data/metrics.csv
```
Afterwards the mind checks: is the session id unchanged, is the identity block
intact, and does `ferry-fetch '<pointer>'` return a turn verbatim? Report it in
the mind's own words. That report is the evidence.

---

## Rollback — any time, one command
```bash
bash FERRY/uninstall.sh      # removes ONLY the base URL Ferry wrote
```
Then restart the session; the mind goes direct to Anthropic again. The
archive stays in `~/ferry-data`. See `docs/RUNBOOK.md` for the 3am page.

## Run log
Keep a run log of your installs (date, instance, result, template fixes) somewhere
private to your deployment -- it names real minds.
