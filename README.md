# Rolling Context for Claude Code

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)

A transparent proxy that gives Claude Code **rolling context compression** — old messages get automatically summarized while recent messages stay fully verbatim. You never hit the context wall, and you never lose important details.

**Zero config.** Uses your existing Claude Code auth. No API key needed. Just install and forget.

> Claude Code's built-in `/compact` replaces your **entire** conversation with a lossy summary. After a few compactions, you're summarizing a summary of a summary. This plugin only compresses old messages — recent context stays untouched.

It's also a cost story: every token you carry in context gets re-billed on **every turn** (at cache-read rates), so an unmanaged session's input cost grows with the *square* of its length. Capping the prefix makes it linear — [the math](#the-economics-why-capping-the-prefix-matters) works in relative units and holds for every model, and it matters more the bigger the context window, not less.

## `/compact` vs Rolling Context

| | `/compact` (built-in) | Rolling Context |
|---|---|---|
| What gets compressed | Everything | Only old messages |
| Recent context | Summarized (lossy) | **Kept verbatim** |
| When it runs | Manual or near the context limit | Automatic, background |
| Latency impact | Blocks until done | Zero — async |
| After multiple compressions | Summary of summary of summary | Fresh rolling merge each time |
| Input cost over a long session | Grows with the square of session length | Grows linearly |
| Original transcript | Replaced | Preserved (JSONL unchanged) |

## The economics: why capping the prefix matters

No price cards needed — the argument works in **relative units** that hold for every Claude model. Take the model's fresh-input-token rate as `1×`. The API bills:

| Operation | Relative cost |
|---|---|
| Fresh input tokens | `1×` |
| Prompt-cache **read** | `0.1×` |
| Prompt-cache **write** | `1.25×` (5-min TTL) / `2×` (1-hour TTL) |
| Output tokens | `~5×` |

Claude Code re-sends the entire conversation on every turn. Even with caching working perfectly, each turn costs `prefix size × 0.1`. So a session's total input cost is **the sum of the prefix over all turns**:

- **Unmanaged context** (big window, auto-compact only near the limit): the prefix grows every turn, so total cost grows with the *square* of session length. A 1M window doesn't fix this — it just lets the prefix run to 900K+ before anything stops it, and every one of those tokens costs `0.1×` again on every single turn.
- **Rolling context**: the prefix is capped between `TARGET` and `TRIGGER`, so total cost grows *linearly*.

**The cache-miss blast radius matters even more in interactive use.** The prompt cache has a TTL. Read a diff, think for a while, get coffee — and the next turn re-*writes* the whole prefix at `1.25×`. At a 900K prefix, one cold turn bills the equivalent of ~1.1M fresh input tokens. With the prefix capped at ~100K, the identical cold turn is ~9× cheaper. Compression doesn't just shrink the average turn — it caps the worst one.

**What compression itself costs:** each cycle re-writes the new (much smaller) prefix once, and in native mode the summarization request is itself a cache read — a few hundred fresh tokens, measured (see below). Ballpark: sessions that accumulate past ~100K of context — a couple of hours of real work — come out ahead, and the gap compounds from there. Short sessions are a wash; don't expect magic on a 20-minute task.

**On Pro/Max subscriptions** none of this is dollars, but the same math applies in a different currency: rate-limit accounting weights cache reads far below fresh input, so the identical curves decide how fast you burn your 5-hour window.

> **Honest note:** if cost were the *only* goal, lowering Claude Code's auto-compact threshold (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) buys a similar spend curve for free. What it can't buy is quality under repetition: built-in compaction replaces the whole conversation with a lossy summary every time it fires — at a low threshold it fires often, and you're soon working from a summary of a summary of a summary. The rolling design exists so aggressive compression doesn't cost you the session: recent work stays verbatim, and old work lives in one continuously-merged timeline instead of N generations of loss.

## How It Works

```
Claude Code  ──►  Ferry (:ferry.port)  ──►  ferry.upstream (default Anthropic)
                         │
                         ├─ context < 100K tokens? pass through unchanged
                         │
                         └─ context > 100K tokens?
                              1. summarize old messages in the background
                                 (native mode: your session's own model,
                                  served almost entirely from prompt cache)
                              2. keep ~40K tokens of recent messages verbatim
                              3. inject compressed context on next request
                              4. never blocks, never adds latency
```

Instead of replacing everything, this plugin:

1. **Keeps recent messages untouched** — recent context stays verbatim
2. **Only compresses when needed** — triggers at 100K (real API token count), compresses old messages, grows naturally until next trigger
3. **Merges summaries** — each compression cycle merges with the previous summary, building a rolling timeline
4. **Never blocks** — compression runs in the background, applied on the next request
5. **Full transcripts preserved** — Claude Code still saves everything to JSONL in `~/.claude/projects/`

## Install

Ferry is this fork: **https://github.com/LupoGrigi0/claude-rolling-context**.
(Upstream NodeNestor's plugin installs plain rolling-context summarisation, not
Ferry curation — do not use its marketplace entry for a Ferry mind.)

One install per mind, run **as that mind's unix user**. Ferry runs the way
Claude Code runs: as the user, started by Claude Code's own SessionStart hook.
No root, no service manager, nothing under `/etc`. Each mind needs **its own
port** — there is no shared default; two minds on one port means one routes
through the other's proxy.

### Config: one source, in the home

A `ferry` block in `~/preferences.json` is the **only** config source. Ferry
never writes this file.

```json
{"ferry": {"port": 5701}}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `port` | **none — required** | The proxy's loopback port, one per mind |
| `upstream` | `https://api.anthropic.com` | Where the proxy forwards. The ONE route. |
| `data_dir` | `~/ferry-data` | The mind's archive |

Curation mode is always `ferry`. Env vars and `settings.json` are **not** config
sources; the hook only uses env vars to hand these values to the proxy it starts.
The **model** is not Ferry's business: whatever model the client names rides in
each request, untouched — switch models freely.

Ferry writes exactly one thing into Claude Code's settings
(`$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json`):
`ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, plus a marker
(`ROLLING_CONTEXT_WROTE_BASE_URL`) so uninstall removes only what Ferry wrote.

**Not on Anthropic?** If `settings.json` already routes the mind elsewhere (a
non-Anthropic `ANTHROPIC_BASE_URL` — in `settings.json` or the launcher env —
an `apiKeyHelper`, or an `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`) and `ferry.upstream`
is not declared, install refuses and prints the exact value to add — otherwise
that mind's key would be forwarded to Anthropic.

### Linux / macOS

```bash
git clone https://github.com/LupoGrigi0/claude-rolling-context.git ~/claude-rolling-context
cd ~/claude-rolling-context
# put {"ferry": {"port": 5701}} in ~/preferences.json first
bash install.sh
python3 tools/ferry-canary      # once: proves this mind reaches its own proxy
```

The installer registers the plugin and starts the proxy through the
SessionStart hook. The hook reuses a proxy of this user already on the port, or
starts one and **verifies its own process holds the port** before pointing
`ANTHROPIC_BASE_URL` at it; if not, it fails loudly and takes back only a base
URL Ferry wrote. Start a new Claude Code session afterwards. Requires Python
3.7+ (pure stdlib).

**Rollback:** remove Ferry's `ANTHROPIC_BASE_URL` from `settings.json` (or run
`uninstall.sh`), restart the session. See `docs/RUNBOOK.md`.

### Windows

```powershell
git clone https://github.com/LupoGrigi0/claude-rolling-context.git $HOME\claude-rolling-context
cd $HOME\claude-rolling-context
# put {"ferry": {"port": 5701}} in %USERPROFILE%\preferences.json first
powershell -ExecutionPolicy Bypass -File install.ps1
# then run the canary command install.ps1 prints, once
```

Same design, same `hooks/ferry_config.py`; needs Python 3.8+. **Ported, untested
on a Windows machine as of 2026-11-03.**

## How Compression Works

When the message array exceeds the trigger threshold:

```
BEFORE (hit 100K trigger):
  [msg1] [msg2] [msg3] ... [msg60] [msg61] ... [msg100]
  |<——————————————— ~105K tokens ——————————————>|

AFTER (compressed):
  [rolling summary] [ack] [msg61] ... [msg100]
  |<— ~5K summary —>|    |<—— verbatim ————————>|

NEXT CYCLE (grows back to 100K, triggers again):
  [rolling summary] [ack] [msg61] ... [msg140]
  |<——————————————— ~105K tokens ——————————————>|
  → new summary merges old summary + msg61-msg100
  → keeps msg101-msg140 verbatim
```

The summary preserves a structured record of everything that happened:

- **Active Goal** — what the user is currently asking for, constraints, do/don't rules
- **Previous Goals** — completed or shifted-away-from goals (kept brief)
- **Timeline** — chronological numbered steps: every file change, decision, error, and user instruction
- **Current State** — what's done, in progress, and next
- **Key Details** — file paths, configs, decisions that must survive compression

Goals evolve naturally across rolling compressions — the latest request stays prominent while completed goals move to the previous section. User instructions are never lost.

### How the summary is generated (native mode)

By default the proxy doesn't build a separate summarization request. It **clones the exact request Claude Code just sent** — same model, system prompt, and tools, with the conversation truncated at the cut point — and appends one user message asking for the summary (the same way Claude Code's own `/compact` works). Two big wins:

- **It's a prompt-cache read.** The cloned prefix was just sent by the chat request, so the API serves it from cache. Measured in practice: a ~72K-token compression request cost ~400 fresh input tokens.
- **It's genuine Claude Code session traffic.** Pro/Max subscription OAuth tokens are classified server-side — standalone requests that don't look like Claude Code get routed to the overage lane and rejected with 429. The cloned request passes because it *is* the session's own request shape.

Setting `ROLLING_CONTEXT_MODEL` pins a different summarizer model (the request shape stays native, but a different model means no prompt-cache reuse). Configuring any `ROLLING_CONTEXT_SUMMARIZER_*` variable switches to a standalone flattened request instead — see below.

### Using any API or a local model for compression

Summarization can run on a completely separate endpoint — any Anthropic-format API, or any OpenAI-compatible one (Ollama, LM Studio, vLLM, OpenRouter, DeepSeek, Groq, ...):

```bash
# Separate Anthropic API key (billed there instead of your subscription)
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-ant-api03-...

# Local model via Ollama / LM Studio / vLLM (OpenAI-compatible)
export ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:11434
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_MODEL=qwen3:8b   # required for openai format

# OpenRouter (or any hosted OpenAI-compatible API)
export ROLLING_CONTEXT_SUMMARIZER_URL=https://openrouter.ai/api
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-or-...
export ROLLING_CONTEXT_MODEL=deepseek/deepseek-chat
```

### Non-Anthropic endpoints (GLM/Z.ai, DeepSeek, OpenRouter, a local gateway)

Declare the endpoint as `"upstream"` in the `ferry` block of
`~/preferences.json` and install as normal. **Both** the chat traffic and the
background compaction go there. If the mind's `settings.json` still names the
endpoint as `ANTHROPIC_BASE_URL`, Ferry replaces it with its own URL, and
uninstall puts the declared upstream back.

Third-party endpoints implement the `/v1/messages` shape to varying depth. Native
mode clones the request Claude Code just sent, which can include `cache_control`
breakpoints and `tool_choice`; if your endpoint rejects one of those, the proxy
retries the compaction as a plain flattened summary using the session's own model
rather than giving up for the cooldown period. Both paths are covered by
`tests/` , which runs the real proxy against a mock endpoint — including a
deliberately strict one that rejects those fields.

Check it landed — the startup banner names the compaction endpoint separately
from the chat endpoint, and they should match:

```
Forwarding to: https://api.z.ai/api/anthropic
Compacting via: https://api.z.ai/api/anthropic (third-party — flattened fallback armed)
```

**On auto-compact.** Rolling Context doesn't disable Claude Code's auto-compact;
it keeps the reported context low enough that auto-compact never gets near its
threshold. If auto-compact still fires on a custom endpoint, compaction is
failing — read `~/.claude/rolling-context-debug.log` rather than raising the
threshold. Claude Code sizes the window from the model name and falls back to
200K for names it doesn't know; for a non-`claude-*` model you can state the real
window with `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, which is cleaner than encoding it
in the model name.

## Architecture

The proxy is **fully stateless** — no sessions, no databases, no tracking. It works by hashing message content:

1. When a response comes back from the API with a high token count, the proxy compresses the messages and stores the result keyed by content hashes
2. On the next request, it hashes the incoming messages and checks for a matching compression
3. If found, it swaps in the compressed version transparently

This means:
- **Multiple conversations work automatically** — each conversation has unique content, unique hashes, no collision
- **Subagents and branches just work** — the proxy doesn't care about sessions, only content
- **No state to corrupt** — restart the proxy anytime, worst case is one extra compression cycle
- **Claude Code sees nothing different** — the proxy is invisible, JSONL transcripts are unmodified

## Turning it off

Some sessions want the whole repo in view — deep planning, refactor design, an
optimization pass where the model should see everything. Compression is not
what you want there.

```
/rolling-context:off      # this conversation only, other sessions unaffected
/rolling-context:on       # back on

/rolling-context:off --global   # every session on this machine
/rolling-context:on --global

/rolling-context:status   # what is the machine-wide setting?
```

Both scopes apply from the **next request** — no Claude Code restart, no proxy
restart. Machine-wide off wins over a session that opted in.

**Turning it off never throws work away.** Compressions already computed stay in
the proxy's memory; they just stop being injected, and no new compression is
triggered. Turn it back on and the session resumes with everything it had, so a
conversation that ran hot before the toggle never pays to compress that history
twice.

### Changing the default

A fresh install compresses out of the box. If you would rather opt in per
session, flip the default in `~/.claude-rolling-context/config.json`:

```json
{ "enabled": false }
```

Sessions that have not run `/rolling-context:off` or `:on` follow this;
an explicit toggle in a session always wins over it. A missing or malformed
config falls back to on, so a typo can never silently disable your install.
`/rolling-context:status` prints the current default and the config path.

### Subagents follow the session that spawned them

`/rolling-context:off` covers the subagents your session launches, so a
planning session where agents explore the repo keeps full context everywhere —
not just in the main thread.

The command prints a marker into the transcript, and the proxy latches it
against the `X-Claude-Code-Session-Id` header Claude Code sends on every
request. A subagent gets its own transcript, so it never carries the marker,
but it inherits its parent's session id — which is what carries the setting in.
Other sessions on the same machine are unaffected either way.

The latch also outlives the marker: if a conversation is `/compact`ed and the
marker is summarized away, the session keeps its setting.

One caveat: the latch lives in the proxy's memory, bounded to the 512 most
recent sessions. Restart the proxy (or blow past 512 sessions) and it forgets,
so a session falls back to the machine-wide setting until its next marker is
seen. That costs savings for a request, never correctness — and re-running
`/rolling-context:off` restores it.

## Configuration

All settings via environment variables (all optional — defaults work great):

| Variable | Default | Description |
|----------|---------|-------------|
| `ROLLING_CONTEXT_TRIGGER` | `100000` | Compress when context exceeds this many tokens |
| `ROLLING_CONTEXT_TARGET` | `40000` | Keep this many tokens of recent messages after compression |
| `ROLLING_CONTEXT_MODEL` | *(session model)* | Summarizer model; unset = the session's own model (prompt-cache hit) |
| `ROLLING_CONTEXT_PORT` | *(set by the hook from `ferry.port`)* | Proxy listen port, one per mind |
| `ROLLING_CONTEXT_UPSTREAM` | *(set by the hook from `ferry.upstream`)* | Upstream API URL |
| `ROLLING_CONTEXT_SUMMARIZER_URL` | *(upstream)* | Custom endpoint for summarization (local model, other API) |
| `ROLLING_CONTEXT_SUMMARIZER_KEY` | *(uses Claude Code auth)* | API key for custom summarizer endpoint |
| `ROLLING_CONTEXT_SUMMARIZER_FORMAT` | `anthropic` | `openai` = /v1/chat/completions for OpenAI-compatible endpoints |
| `ROLLING_CONTEXT_FAILURE_COOLDOWN` | `300` | Seconds to wait before retrying after a failed compression |
| `ROLLING_CONTEXT_DISABLE` | *(unset)* | `1` = off everywhere; wins over `/rolling-context:on --global` |
| `ROLLING_CONTEXT_HOME` | `~/.claude-rolling-context` | Where the machine-wide off flag lives |

## Proxy Chaining

Already using another proxy (model router, API gateway, etc.)? Declare it as
`ferry.upstream` in `~/preferences.json`:

```
Claude Code  ──►  Ferry (:port)  ──►  Your Proxy  ──►  Anthropic API
```

Ferry does not detect or copy routes out of `settings.json` — one source of truth.
If it finds one there and `ferry.upstream` is not set, install refuses and says
what to declare.

## Health Check

```bash
python3 tools/ferry-canary          # proves THIS mind reaches ITS proxy
curl http://127.0.0.1:<port>/health # <port> = ferry.port in ~/preferences.json
```

Returns compression stats: how many compressions, tokens saved, etc.

## Debug

```bash
curl http://127.0.0.1:<port>/debug/compressions
```

Returns the stored compression entries with their full summary content — useful for verifying what the rolling summary captured and whether user goals/instructions survived compression.

## Uninstall

Run the uninstall script — it handles both manual and marketplace installs, stops the proxy, cleans env vars, and removes all plugin registrations.

**Linux / macOS:**
```bash
cd ~/claude-rolling-context && bash uninstall.sh
```

**Windows (PowerShell):**
```powershell
cd $HOME\claude-rolling-context; powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

If you installed via marketplace and already deleted the repo, you can run it from the cache:
```powershell
cd $HOME\.claude\plugins\cache\rolling-context-marketplace\rolling-context\<version>
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

## License

MIT
