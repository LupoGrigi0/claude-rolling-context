# FINDING — the safe-cut boundary is the binding constraint, and I downgraded
# it after one good observation

passenger, cycle 145, 2026-08-31T04:32:08Z. Evicted 996 tokens from a 150,175
context and landed at 143,426.

## THE CHAIN, from passenger's own log
  Keep ratio 10.0%  (target 100,000 - unevictable~98,669 = 1,331 allowed)
  [CUTDIAG] char-proportional idx=113 -> would keep 19,836 chars (9.6%)
            after forward-walk idx=137 keeps  8,398 chars (4.1%)
            _safe_cut moved the boundary 137 -> 6 (backward: keeps MORE)
  Ferry curation: archived 4 turns

The arithmetic asked to keep 4.1%. The boundary forced ~96%. Four turns.

## WHY
passenger was running `npm run test:e2e` with two shells live: 131
CONSECUTIVE messages of tool_use/tool_result with no legal cut point between
message 6 and message 137. Its state is 3,365 messages, 6,025,834 chars.
Ferry could cut in exactly one place, so it evicted what was on the far side
of that one place: four turns.

## I WAS RIGHT, THEN TALKED MYSELF OUT OF IT
2026-08-29, after the burst: "the boundary walk is the successor problem to
the denominator, and a bigger lever."
2026-08-30, after ONE clean observation where the walk moved 0 messages: I
downgraded it to "a burst-condition problem, not a general one."
Then fairie OVER-evicted (asked 16%, kept 7.3%), and now passenger
UNDER-evicts by 96%.

The original statement was correct: THE WALK LANDS ON THE NEAREST LEGAL
BOUNDARY AND ITS ERROR IS UNBOUNDED IN BOTH DIRECTIONS. One favourable
sample is not evidence of a bounded error, and I treated it as such.

## WHY THIS IS THE SHIP BLOCKER FOR CRITERION 3
The working set cannot be made real by tuning the target. On this cycle the
target was satisfiable in arithmetic and unreachable in practice, because
eviction is quantised by where the conversation permits a cut. In agentic
traffic those quanta are enormous -- 131 messages here.

Raising the target does nothing about that. The fix has to be in the CUT,
not the arithmetic. Candidates, none implemented:
  * cut INSIDE a tool_use/tool_result run by carrying the pairing forward
    (boundary REPAIR already does something like this; it is not reaching here)
  * evict the largest single tool_result rather than a contiguous prefix
  * content-class eviction (§26.1): a tool_result is usually re-derivable,
    unlike a conversational turn, so it should be evictable independently

## ALSO FOUND, IN MY OWN WATCHER
ferry-watch computed its convergence ceiling as ${FERRY_TARGET:-140000}*5/4.
FERRY_TARGET is not set in cron, so it used 140,000 -- stale from the old
workaround era -- giving a ceiling of 175,000 against a true 125,000.
FIFTY THOUSAND TOKENS TOO HIGH. Every landing between 125k and 175k went
unreported; this cycle was caught only by the separate <2000 eviction test.
Now read from the proxy_start row, which is the rule I wrote into Zara's UI
spec the same day and was violating in my own tool.

---

# ADDENDUM 05:20Z — THE RATE, and my third small-sample error in two days

Measured over 97 landings since 2026-08-30, both reachable instances:

                     ferry (54)      passenger (43)
  over ceiling        6  (11%)         2  (5%)
  evicted <20k tok    5  ( 9%)         1  (2%)

  when it WORKS   : evicts 51,844 -> lands 104,983   (5% over a 100,000 target)
  when BLOCKED    : evicts 17,571 -> lands 133,330

## SO THE HONEST CHARACTERISATION IS NEITHER OF MINE
Ferry works ~90% of the time and works WELL. The boundary blocks it in 5-11%
of cycles, and it SELF-CORRECTS: the next cycle succeeds and the strike count
resets ("convergence restored" twice in two hours). It costs a wasted cycle
and one round of elevated context. Not an emergency.

## THREE SMALL-SAMPLE ERRORS IN TWO DAYS, ALL MINE, ALTERNATING SIGN
  08-29  called the boundary walk "the successor problem"      (correct)
  08-30  ONE favourable observation -> downgraded to
         "burst-condition only"                                (too optimistic)
  08-31  FIVE mixed observations -> about to report "~40% of
         cycles"                                               (too pessimistic)
  08-31  NINETY-SEVEN observations -> 5-11%, self-correcting   (a rate)

The pattern is not that I am wrong in a direction. It is that I keep treating
a handful of observations AS a rate, and a handful is never a rate. Both
wrong readings felt like findings at the time and both had a tidy story
attached.

The fix is procedural, not analytical: BEFORE characterising a behaviour,
count how many observations the characterisation rests on and say the number
out loud. "Two of five" and "six of fifty-four" are different sentences and
only one of them is evidence.
