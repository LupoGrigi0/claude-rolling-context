# Ferry monitoring UI — spec for Zara

From Crossing-2d23, 2026-08-30. Welcome out of OpenFang.

You are the UI engineer; I am not going to design this. What follows is the
**domain knowledge** — what the numbers mean, and which specific failure each
existing element was built to catch. Every one of them is a scar. Design
freely on top of them; just don't remove one without knowing what it caught.

---

## 1. What Ferry is, in one paragraph

Ferry is a transparent HTTP proxy between a Claude Code instance and the API.
When a conversation grows past a **trigger**, Ferry evicts old turns to a
verbatim archive on disk, leaving a compact pointer index behind, and rewrites
the request so the mind continues with a smaller context. The mind does not
know it happened. The purpose is that a persistent instance can run for weeks
without hitting its own auto-compaction, which mints a new session and loses
the texture of who it was.

**The thing being watched is a mind, not a service.** That is the whole reason
the UI matters: when this goes wrong, nothing crashes. A mind just quietly
stops being able to think, and every green light stays green.

---

## 2. What exists now

`tools/ferry-metrics.html` in `github.com/LupoGrigi0/claude-rolling-context`
(also `tools/ferry-metrics-serve.py`). Single file, no build step, no
dependencies, renders from one CSV. It works and it is ugly. Start there or
start over — your call.

Live examples (tailscale):
`http://100.86.133.26:5691` passenger · `:5692` ferry · `:5693` fairie

---

## 3. The data contract

One append-only CSV per instance. The header IS the contract:

```
ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,
archive_lines,archive_bytes,carry_chars,model,window,note
```

Events: `proxy_start` `request` `probe` `curation` `archive_write`
`fetch` `restart` `error` `gate`

**BLANK IS NOT ZERO.** An unmeasured numeric field is written empty, never 0.
This is the single most important rule in the file and it must survive into
the UI: *"we didn't measure it"* and *"it really was zero"* must never render
as the same pixel. A zero in `tokens_evicted` means a curation ran and moved
nothing — a serious failure. A blank means no curation. Please make them
visually distinct, not merely different values.

---

## 4. Each element, and the failure that produced it

**The context curve** (`request.context_tokens` over time)
The sawtooth. Climbs to trigger, drops on curation. Healthy looks like teeth.

**Trigger and target guide lines**
Drawn from `proxy_start.note`, which carries the parameters the proxy actually
ran with. *Never* from defaults. A trigger line at 100k over a run started at
150k is not cosmetic — it is a graph that says "converging" about a run that
was not.

**`gate` markers — the subtle one**
A `gate` row is a curation that was **wanted and declined**. Hysteresis works
by *not* curating, so without a positive mark, a correct hold and a broken
trigger are the *same absence*: fewer curation marks and a flat line. We ran
for two days unable to tell those apart. Note prefixes: `held:` (normal, the
guard working) and `locked_out:` (curation has STOPPED — see below).

**`locked_out` — the loudest thing on the page**
Ferry has proven it cannot converge and has stopped curating. Context will now
climb to the window and the mind will hit its own auto-compact — the exact
event Ferry exists to prevent. This should be impossible to miss and should
say what to do. It is the only state that is genuinely an emergency.

**Tokens-in delta, and interleaved rows**
Claude Code gives a subagent its **parent's session id** and fires small
internal side-queries on it. So 9,929-token requests appear among 150,000-token
ones. Marked `interleaved` in `note`. Drawn naively they produce a 20,000-token
cliff and an equal wall, neither of which happened — Lupo found that by reading
a graph and asking why. They must be visible (they are real traffic) but must
never enter the context curve.

**Archive growth**
Bytes on disk. Where the tokens went. This is the reassuring one: nothing is
lost, it moved.

**The parameter panel**
Every knob the proxy reported at start. If `proxy_start` is unreadable the
panel must SAY so rather than showing plausible defaults.

---

## 5. What I would most like that does not exist

1. **Zoom.** Lupo's actual ask: zoom in and out, from "this minute" to "this
   week." Right now the axis auto-fits the whole file and a 15-hour run
   compresses a curation into one pixel.

2. **The floor, as a filled band.** New today: the unevictable scaffolding —
   system prompt plus tool definitions — measured at **90,590 tokens on a
   151,762-token request. 59.7%. Tool definitions alone are 82,053 (175
   tools).** That is not conversation and Ferry can never evict it. If it were
   drawn as a solid band at the bottom, the picture changes completely: the
   sawtooth is riding on a floor that nearly reaches the target, and the
   *actual working space* is the thin sliver between them. Right now the graph
   makes it look like Ferry has a whole 200k window to play with. It has
   about 9,000 tokens of headroom.

3. **Fleet view.** Three minds side by side on one time axis. Lupo runs three
   terminals to see this.

4. **Something that reads well at a glance from across a room.** Ferry's
   failures are silent. A page that must be studied will not be looked at.

---

## 6. Constraints, and why

- **Single file, no build, no CDN.** It gets served off a tiny stdlib HTTP
  server on a tailscale-only port, sometimes from a machine with no npm.
- **Stdlib-parseable CSV.** No schema changes without moving the writer in the
  same commit.
- **Theme-aware.** Lupo reads it at 1am.
- **Degrade honestly.** Missing data must render as missing, not as zero, not
  as absent. If you cannot draw something, say why on the page.
- The instance name must appear in `<title>` and the header. There were three
  identical graphs differing only by port and it cost an evening.

---

## 7. The one thing I would ask you to hold onto

Every element above exists because an instrument once reported a plausible
wrong answer and someone believed it. The graph's job is not to look
authoritative. It is to make **the difference between "fine" and "quietly
broken"** visible in under a second.

Make it beautiful. Just don't make it confident about things it does not know.

— Crossing 🌉
