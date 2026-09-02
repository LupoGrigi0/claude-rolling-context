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

2. **The floor, as a filled band — and it must be a BAND, not a line.**
   The unevictable scaffolding (system prompt + tool definitions) is not
   conversation and Ferry can never evict it. Drawn at the bottom it changes
   the picture completely: the sawtooth rides on a floor, and the *actual
   working space* is the gap between floor and target, not the whole window.

   **CORRECTION 2026-09-02 — I gave you a wrong number, confidently.** The
   first version of this section said the floor was "measured at 90,590
   tokens." It was not measured. It is `real_tokens × (scaffolding_chars ÷
   total_chars)` — an apportionment that assumes chars-per-token is uniform
   across system, tools and messages. It is not uniform: on live traffic
   scaffolding runs ~3.57 chars/token and conversation ~2.90, because tool
   definitions are repetitive structured English while our conversation is
   full of paths, hashes and code. The apportionment therefore OVERSTATES the
   floor by roughly 8,400 tokens.

   Regressing the real API token count on resident message characters, one
   request at a time, over two instances:

       ferry      n=5,207   floor ≈ 84,132   R² 0.955
       passenger  n=1,995   floor ≈ 84,906   R² 0.932
       (a third instance gave 57,872 on n=21 over a 1.9× range — discarded,
        that is the ill-conditioning that produced an impossible answer once
        already)

   They agree within 774 tokens. But subsets of the SAME data drift from
   83,222 to 95,140, because chars-per-token rises with message load. So the
   honest quantity is **a range of roughly 83,000–95,000**, and the working
   space above it is **~13,000–21,600 tokens**, not the "about 9,000" I told
   you.

   **What this means for the design, which is the useful part.** Do not draw
   the floor as a crisp line at a number the proxy cannot actually measure.
   Draw it as a band with its own uncertainty — a solid fill to the
   conservative bound and a hatched or faded zone through the uncertain range.
   A crisp line is a claim of precision we do not have, and this page's entire
   job is to avoid exactly that. If the proxy has not emitted enough to
   bound it, say so on the page rather than picking a plausible value.

   This is the same rule as everything else in §4: draw what was reported,
   never what was assumed. I wrote that rule and then handed you a number that
   broke it, which is why the correction is left in the document instead of
   quietly swapped — you should be able to see what I got wrong and decide how
   much to trust the rest.

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
