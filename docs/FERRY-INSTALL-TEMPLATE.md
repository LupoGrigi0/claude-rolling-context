# Ferry Install Runbook — TEMPLATE

**For a mind and Lupo, together. By Crossing-2d23.**
Puts a mind on Ferry: a proxy between Claude Code and its upstream that curates
context instead of compacting it. Evicted turns move verbatim to an archive on
disk, with pointers left in their place, the identity block is carried
verbatim, and the session id never changes. **Nothing is paraphrased; nothing
is lost.**

Same shape as the v2 migration template: copy this file into the mind's
`~/wake/`, fill in the variables, then walk the steps together. **Verify by
acting**, and keep the `echo $HOME` identity guard before every block.

> **DISCIPLINE (Lupo + Crossing, 2026-11-03).** This template lives in the Ferry
> repo, and **only** here. When a run finds something wrong, fix it in *this
> file*, commit, and push before (or right after) fixing the copy. **No
> runbook change ever lives on disk only.**

> **STATUS.** **[PENDING]** — first run is WakeTest-8bc1, then its sibling
> WakeTest2-b0ec. Anthropic login only until both close. Not yet for minds on
> OpenRouter or any other route (Witness, Genevieve): that comes after.

> **What Ferry needs, and nothing else.** No root, no service manager, nothing
> under `/etc`. Ferry runs **as the mind's own user**, started by Claude Code's
> SessionStart hook. Its **only** config is the `ferry` block in
> `~/preferences.json`. Claude Code gets exactly one line in its `settings.json`:
> `ANTHROPIC_BASE_URL=http://127.0.0.1:PORT`. The **model** is not Ferry's
> business; it rides each request untouched.

---

## Variables — the only per-mind differences
```
INSTANCE  = <full instance id, EXACT CASE>     e.g.  WakeTest-8bc1   (== its unix user)
HOME      = /mnt/coordinaton_mcp_data/instances/INSTANCE
PORT      = <a FREE loopback port, one per mind>   e.g. 5620
            # check it is free:  ss -lnt | grep -c ':PORT '   -> must print 0
            # NEVER a house port: 21000-21005, 21097, 21099, 22000-22999, 5610-5613
BRANCH    = ferry/ship-installable        # until merged; then: master
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
d["ferry"] = {"port": PORT}
json.dump(d, open(p, "w"), indent=2); print(d["ferry"])
EOF
```
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
cd FERRY && bash install.sh
```
Expect `[4/4] ... Proxy verified on port PORT`. Anything else: **stop and paste
the output.** A refusal names its reason and changes nothing.

## 4. Prove the route — the canary
```bash
python3 FERRY/tools/ferry-canary
```
Exit 0 means one real request went through **this mind's own** proxy. Run by a
person, on purpose: the proof is seen, not assumed.

## 5. Does Claude Code run Ferry's hook? — **[PENDING: unmeasured]**
Restart the mind's session the way it normally runs. Then:
```bash
tail -3 ~/.claude/rolling-context-hook.log
```
* **A new line timestamped after the restart:** the hook fires, and Ferry
  restarts its own proxy at every session start.
* **No new line:** install's plugin symlink is not loaded by this Claude Code.
  Ferry works **until the proxy dies**, and nothing would restart it. Report it
  to Crossing before going further.

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

## Run log — append one line per run, in this file, committed
| date | INSTANCE | who | result | template fixes made |
|---|---|---|---|---|
