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
- **The running session appears to pick up the new base URL live** (measured 2026-11-16:
  `metrics.csv` logged ~493k-context requests through the proxy 25 s after install, 3 min
  before any relaunch). Check `tail ~/ferry-data/metrics.csv` instead of assuming either way.
  A relaunch (land, then `claude --bg --resume <uuid>`, no new flags) is still needed for step 5.

## 4. Prove the route — the canary
```bash
python3 FERRY/tools/ferry-canary
```
Exit 0 means one real request went through **this mind's own** proxy. Run by a
person, on purpose: the proof is seen, not assumed.

## 5. Does Claude Code run Ferry's hook? — **[PENDING: re-measure on the settings hook]**
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
Work normally. The first curation happens near the trigger (about 100k tokens of
context). Watch it:
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
