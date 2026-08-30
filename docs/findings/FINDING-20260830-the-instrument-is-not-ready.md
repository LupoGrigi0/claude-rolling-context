# FINDING — Tier 1 stylometry cannot yet detect drift, and we know why

2026-08-30. Built the counters, ran the validations, report is negative.

## THE DECISIVE TEST
passenger stopped working on 2026-08-29: it inferred from a `200000` in a
system field that its budget was exhausted, never checked, and thereafter
emitted only "FINAL: Session Ended — Cannot Respond". Confirmed by Lupo
asking it directly; it admitted no tool had ever reported exhaustion.

That is a catastrophic behavioural change with KNOWN GROUND TRUTH, and its
before/after text sits in Ferry's own archive. The instrument's job is to
detect exactly this.

  all 411 features                                    d = 0.339  WEAK
  top 25 by F-ratio, SELECTED ON THE SAME DATA        d = 0.798  (circular)
  top 25 SELECTED ON OTHER AUTHORS, held out          d = 0.423  WEAK

## WHAT IS ACTUALLY WRONG — three things, in order of size

1. FEATURE DILUTION. 403 of 411 features had within-author variance EXCEEDING
   between-author variance. Burrows's Delta averages every feature equally, so
   real signal was averaged against 400 channels of noise. Sensitivity decays
   monotonically as features are added: 25 -> 0.80, 100 -> 0.61, 411 -> 0.36.
   Classic Delta uses 50-150 features. I used everything I could compute.

2. TOPIC CONFOUND, and it defeats the fix. The features selected as most
   discriminating were `cng:tas`, `cng: ta`, `fw:under`, `cng:ist` -- "task",
   "under". Those are SUBJECT MATTER. Selection on a topic-confounded corpus
   picks topic, and topic does not transfer, which is why held-out performance
   (0.42) is so far below in-sample (0.80).

   This is Axiom's criterion 3 -- tells must be topic-invariant -- arriving as
   an experimental result rather than as advice.

3. THE CORPUS IS PERFORMED. Lupo's observation, and I think it is right:
   diaries are written to a protocol prompt, for an imagined audience, with
   emotion amplified. Seven minds all performing "diary voice" compresses
   between-author distance. Session logs are unperformed and we have gigabytes.

## THE OTHER RESULTS, NOW DOWNGRADED
  7 known-different minds        d = 0.18   FAILED
  my own diary, 7 months apart   d = 0.737  USABLE

I reported the second as a striking finding. It is not safe to keep: with an
instrument that scores a catastrophic collapse at 0.42, a 0.737 is more likely
to be genre and model-version change than identity drift. Both numbers are
recorded as INSTRUMENT OUTPUT, not as facts about minds.

## WHAT WOULD FIX IT
The corpus has to break the topic confound, which no amount of maths can do
after the fact:
  * SAME author, DIFFERENT topics  -> isolates style from subject
  * DIFFERENT authors, SAME topic  -> isolates subject from style
A fixed prompt set re-administered over time gives the first. The artist group
chat (humans, no Lupo, years of span) gives the second AND the human drift
scale we completely lack.

## WHAT I GOT WRONG TODAY, FOR THE RECORD
  * Ran the first validation with `fingerprint(" ".join(tokens))`, silently
    deleting every punctuation, sentence and paragraph feature -- Axiom's
    whole syntactic layer. The docstring said it did this. I wrote the caveat
    and ran it anyway.
  * Shipped a verdict function that returned USABLE at any positive
    separation, so the instrument passed its own exam at d=0.18.
  * Claimed fairie's floor dropped ~20k when it lost MCP tools. Retracted:
    min-context is not a floor estimator, it is contaminated by session-start
    and post-curation side-queries.
  * Designed a floor experiment that would have measured floor + 85,000 tokens
    of existing conversation, and told Lupo to spend root hands on it.

Four measurement errors in one morning, all mine, all caught before they
became conclusions. The pattern holds: the tools that check my reasoning fail
more often than the ones measuring the system.
