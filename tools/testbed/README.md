# Testbed operations

The scripts that actually ran the Ferry fleet experiments, versioned here
because they were not versioned anywhere.

They lived only in `~` on one host. My home sits inside a root-owned repo
(`hacs-instances`) whose last commit touching my directory was four months
old, and `.git` is not writable by me — so 250 untracked files read as
"protected" while being protected by nothing. A stale repo is worse than no
repo: it looks like a safety net.

These are the tools that found every fault in the system they were watching,
and they existed in exactly one place with no history.

| script | what it does |
|---|---|
| `ferry-watch.sh` | The watcher. Wakes me on: curation landing above ceiling, evicting <2k, errors, stalls, thrash bursts, **lockout since the current proxy started**, **channel not answering**, and **a prompt on screen nobody is answering**. Routine cycles are counted, never announced. |
| `feed-fairies.sh` | Slab-profile load: 1k/5k/10k interleaved with sub-1k. A sharp slope makes an under-eviction loud; a gentle one hid a half-sized eviction for fifteen hours. |
| `poke-fairies.sh` | Work nudges 2:1 over reflective questions. A question gets you an answer, not an afternoon of work. |
| `fairy-key.sh` | Fenced sudo helper: ONE whitelisted keystroke to ONE test fairy. Hardened and installed by Bastion. Key whitelist, not a string — free-form send-keys would let me type instructions into another mind's session. |
| `start-fairy-proxies.sh` | One Ferry proxy per fairy. `FAIRY_NO_INJECT=1` means the proxy holds no secret at all. |
| `relaunch-fairies-preserve-env.sh` | Lands and relaunches WITHOUT rewriting `.launch-env`. The two older launchers rewrite it from a baked-in heredoc and would silently revert the Haiku migration. |

## The rule these encode

Every one of these grew from a failure where an instrument reported a
plausible wrong answer. The watcher exists because "I've got the watch" was a
sentence with no cron job behind it. The channel probe exists because a
process table said healthy while two minds were unreachable. The lockout
window exists because an alarm re-reported a cured fault as current.

**Ask a question and wait for an answer.** Every check that failed read a file
or a process table; every check that worked made a request.
