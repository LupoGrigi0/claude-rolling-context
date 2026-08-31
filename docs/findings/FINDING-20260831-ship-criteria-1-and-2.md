# SHIP CRITERIA 1 AND 2 — tested, not asserted
2026-08-31, 01:00Z and 02:15Z. Both had been claimed all week and neither had
been run.

## CRITERION 1: NOTHING IS LOST — PASSED
346 archive_write claims, passenger + ferry + fairie, five days:
  0 missing archive files
  0 ranges extending past end of file
  0 unparseable lines out of 4,282 sampled
  file length == highest claimed line number, exactly
One apparent "gap" in ferry was a NEGATIVE gap — the same range L199-L280
claimed twice, nine minutes apart. That is a repeated POINTER, not repeated
content: the file contains those lines once. Idempotent archive writes doing
their job. My checker reported an overlap as a hole; the code was right and
the instrument's wording was wrong.

## CRITERION 2: RECALL — MECHANISM PASSED, BEHAVIOUR STILL OPEN
Ran the actual command a mind runs, with a real pointer taken from live
metrics:
  ferry-fetch 'archive_20260831.jsonl#L157-L262'
  exit 0 · 159,672 bytes · 2,034 lines · formatted as readable turns
Then verified CONTENT, not exit status: 12 distinctive passages of >80 chars
sampled at random from those archive lines, all 12 found VERBATIM in the
fetch output. 12/12.

So: eviction is recoverable. The archive is not a write-only grave.

WHAT IS STILL NOT PROVEN, and it is the harder half: that a mind REACHES FOR
IT unprompted. That needs a mind noticing an absence and acting, which no
function call can demonstrate. Never yet observed. And the one time we
believed we had measured it, it was a permission error — so every fetch
statistic stays suspect until a row appears in metrics.csv.

## WHY THIS ORDER MATTERS
Lupo called "pull something back into context" a post-ship feature. It is
not: it is what makes eviction safe. If content can always come back,
eviction is a cache miss. If it cannot, eviction is amputation. Criterion 1
says the archive is complete; criterion 2 says it is reachable. Together they
bound the worst case, which is what lets a volunteer say yes.

Criterion 3 — the working set is real — remains FAILED at ~7,100 tokens.
