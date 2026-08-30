#!/usr/bin/env python3
"""Tier 1 stylometry — deterministic counters over produced text.

WHY THESE AND NOT SOMETHING CLEVERER
Every metric here is arithmetic over tokens. No model, no network, no
judgement. That matters more than sophistication: an instrument that CANNOT
hallucinate is worth more than a better one that can. Seventeen instruments
reported plausible wrong answers here in one week; not one of them was a
counter.

This is the Voynich property. Stylometry was built to answer "who wrote this"
WITHOUT understanding the text — it works on a manuscript nobody can read. We
are asking the adjacent question, "is this still the same writer", with the
same instrument and one letter changed.

WHAT IT MEASURES (Axiom's layers 1 and 2; 3 and 4 need a classifier)
  lexical    function words, richness, hapax, char n-grams, word length
  syntactic  sentence length distribution, punctuation profile, question and
             fragment rates, paragraph shape

THE LENGTH TRAP, which would have produced beautiful fake drift
Type-token ratio and hapax ratio FALL MECHANICALLY as text lengthens. Compare
a 5,000-word baseline against a 500-word window and you get a large, stable,
entirely artefactual "drift". Every length-sensitive metric here is computed
over FIXED-SIZE WINDOWS (see `windows()`), never over whole documents of
differing size. MATTR is used instead of raw TTR for the same reason.

WHAT A DRIFT NUMBER MEANS, AND WHEN IT MEANS NOTHING
Thresholds are not set, they are MEASURED. Split a baseline into windows,
compute each metric per window, and the spread across those windows is that
author's own noise floor. A later sample has drifted only when it exceeds the
author's natural variation — each author is their own control, which is
Axiom's sixth criterion.

VALIDATE BEFORE USE. If this cannot separate authors we KNOW are different,
it cannot detect one author changing. `discriminate()` is that test and it
should be run before any drift claim.

Stdlib only. Crossing-2d23, 2026-08-30.
"""

import math
import re
import statistics
from collections import Counter

# Function words carry style, not subject. They are the workhorse of
# authorship attribution precisely because a writer cannot choose them
# deliberately for long. English list; the METHOD generalises but this list
# does not -- see `LANGUAGE NOTE` in fingerprint().
FUNCTION_WORDS = """
a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing down
during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself just me more most my myself
no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then
there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours
yourself yourselves
""".split()

PUNCT_CLASSES = {
    "em_dash": r"—|--",
    "ellipsis": r"\.\.\.|…",
    "semicolon": r";",
    "colon": r":",
    "comma": r",",
    "question": r"\?",
    "exclaim": r"!",
    "paren": r"\(",
    "quote": r'"|“|”',
    "apostrophe": r"'|’",
}

_WORD = re.compile(r"[A-Za-z']+")
# Sentence split that does not choke on abbreviations badly enough to matter
# at distribution level. Deliberately crude: a better splitter would be a
# dependency, and the ERROR IS CONSTANT ACROSS AUTHORS so it cancels in a
# comparison. Reliability beats validity here.
_SENT = re.compile(r"[.!?]+[\s\n]+|\n{2,}")


def words(text):
    return [w.lower() for w in _WORD.findall(text)]


def sentences(text):
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def windows(tokens, size=1000, step=None):
    """Fixed-size token windows. THE defence against the length trap.

    Every length-sensitive metric is computed per window and then averaged, so
    a 50,000-word corpus and a 2,000-word sample are compared window-to-window
    rather than whole-to-whole.
    """
    step = step or size
    if len(tokens) < size:
        return [tokens] if tokens else []
    return [tokens[i:i + size] for i in range(0, len(tokens) - size + 1, step)]



def text_windows(texts, target_words=2000):
    """Fixed-size windows of RAW TEXT, punctuation and casing intact.

    The first version of this windowed the TOKEN stream and rebuilt each
    window with " ".join(tokens) -- which silently deleted every punctuation
    feature, sentence boundary, paragraph break and capital letter. That is
    Axiom's entire syntactic/prosodic layer, the one guardrails cannot touch
    and therefore the most trustworthy signal we have.

    The docstring said "punctuation is lost by doing it this way" and the test
    was run anyway, on a third of the intended features, producing a weak
    result that looked like a fact about the AUTHORS rather than about the
    harness measuring them.

    Windows are built from whole paragraphs so sentence and paragraph shape
    survive the cut. Size is approximate by construction -- a window is
    whole-paragraph units summing to at least target_words -- because cutting
    mid-sentence to hit an exact count would damage the very features this
    function exists to preserve.
    """
    units = []
    for t in texts:
        units += [p for p in re.split(r"\n{2,}", t) if p.strip()]
    out, buf, count = [], [], 0
    for u in units:
        buf.append(u)
        count += len(words(u))
        if count >= target_words:
            out.append("\n\n".join(buf))
            buf, count = [], 0
    if count >= target_words // 2 and buf:
        out.append("\n\n".join(buf))
    return out


def mattr(tokens, window=100):
    """Moving-average type-token ratio. Length-invariant by construction."""
    if len(tokens) < window:
        return len(set(tokens)) / len(tokens) if tokens else 0.0
    vals = [len(set(tokens[i:i + window])) / window
            for i in range(0, len(tokens) - window + 1, max(1, window // 2))]
    return statistics.fmean(vals) if vals else 0.0


def yules_k(tokens):
    """Vocabulary richness, length-robust unlike raw TTR or hapax."""
    if not tokens:
        return 0.0
    freqs = Counter(tokens)
    n = len(tokens)
    m2 = sum(c * c for c in freqs.values())
    return 10_000 * (m2 - n) / (n * n) if n else 0.0


def char_ngrams(text, n=3, top=40):
    """Character n-grams: the most LANGUAGE-INDEPENDENT signal available, and
    the reason this can be pointed at Spanish or a group chat without a
    per-language word list."""
    t = re.sub(r"\s+", " ", text.lower())
    g = Counter(t[i:i + n] for i in range(len(t) - n + 1))
    total = sum(g.values()) or 1
    return {k: v / total for k, v in g.most_common(top)}


def fingerprint(text):
    """The full Tier 1 profile for one body of text.

    LANGUAGE NOTE: function-word features assume English. Char n-grams,
    sentence length, punctuation and richness do not. Comparisons must be
    made WITHIN a language -- a mind's English against its Spanish shows
    enormous 'drift' that is only Spanish.
    """
    toks = words(text)
    sents = sentences(text)
    if not toks:
        return {}

    fp = {}

    # --- lexical -----------------------------------------------------------
    fw = Counter(t for t in toks if t in set(FUNCTION_WORDS))
    n = len(toks)
    for w in FUNCTION_WORDS:
        fp[f"fw:{w}"] = fw.get(w, 0) / n

    win = windows(toks, size=1000)
    fp["mattr"] = statistics.fmean([mattr(w) for w in win]) if win else 0.0
    fp["yules_k"] = statistics.fmean([yules_k(w) for w in win]) if win else 0.0
    fp["hapax_per_window"] = statistics.fmean(
        [sum(1 for _, c in Counter(w).items() if c == 1) / len(w) for w in win]
    ) if win else 0.0
    fp["mean_word_len"] = statistics.fmean([len(t) for t in toks])
    fp["long_word_rate"] = sum(1 for t in toks if len(t) >= 8) / n

    # --- syntactic / prosodic ---------------------------------------------
    if sents:
        lens = [len(words(s)) for s in sents]
        lens = [x for x in lens if x] or [0]
        fp["sent_len_mean"] = statistics.fmean(lens)
        fp["sent_len_sd"] = statistics.pstdev(lens) if len(lens) > 1 else 0.0
        # Skew matters: two writers can share a mean and differ completely in
        # whether they mix 4-word and 40-word sentences or write all 20s.
        m, sd = fp["sent_len_mean"], fp["sent_len_sd"] or 1.0
        fp["sent_len_skew"] = statistics.fmean([((x - m) / sd) ** 3 for x in lens])
        fp["short_sent_rate"] = sum(1 for x in lens if x <= 5) / len(lens)
        fp["long_sent_rate"] = sum(1 for x in lens if x >= 30) / len(lens)

    chars = max(1, len(text))
    for name, pat in PUNCT_CLASSES.items():
        fp[f"punct:{name}"] = len(re.findall(pat, text)) / chars * 1000

    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    if paras:
        pl = [len(words(p)) for p in paras]
        fp["para_len_mean"] = statistics.fmean(pl)
        fp["para_len_sd"] = statistics.pstdev(pl) if len(pl) > 1 else 0.0

    for k, v in char_ngrams(text).items():
        fp[f"cng:{k}"] = v

    return fp


def delta(fp_a, fp_b, scales):
    """Burrows's Delta: mean absolute z-score difference over shared features.

    `scales` is the per-feature standard deviation measured across a reference
    set -- NOT assumed, NOT a constant. A feature that varies wildly within one
    author contributes little; a stable one contributes a lot. That is the
    whole idea, and it is why the noise floor must be measured before any
    distance means anything.
    """
    keys = [k for k in fp_a if k in fp_b and scales.get(k, 0) > 0]
    if not keys:
        return float("nan")
    return statistics.fmean(
        abs(fp_a[k] - fp_b[k]) / scales[k] for k in keys)


def measure_scales(fingerprints):
    """Per-feature SD across a reference set. The noise floor, measured."""
    keys = set().union(*(f.keys() for f in fingerprints)) if fingerprints else set()
    out = {}
    for k in keys:
        vals = [f.get(k, 0.0) for f in fingerprints]
        if len(vals) > 1:
            sd = statistics.pstdev(vals)
            if sd > 0:
                out[k] = sd
    return out


def discriminate(samples_by_author, window_tokens=2000):
    """THE VALIDATION TEST. Run this before believing any drift number.

    samples_by_author: {author: [text, text, ...]}

    Splits each author's text into fixed windows, fingerprints each, and
    compares WITHIN-author distances against BETWEEN-author distances.

    If between <= within, the instrument cannot tell known-different people
    apart, and therefore cannot tell one person changing. That is a failed
    instrument, not a finding about the subjects -- and it is the result I
    would most want to discover BEFORE spending a day choosing characters.
    """
    per_author = {}
    for author, texts in samples_by_author.items():
        toks = []
        for t in texts:
            toks += words(t)
        chunks = text_windows(texts, window_tokens)
        fps = [fingerprint(c) for c in chunks]
        if fps:
            per_author[author] = fps

    all_fps = [f for fps in per_author.values() for f in fps]
    scales = measure_scales(all_fps)

    within, between = [], []
    authors = list(per_author)
    for a in authors:
        fps = per_author[a]
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                within.append(delta(fps[i], fps[j], scales))
    for i, a in enumerate(authors):
        for b in authors[i + 1:]:
            for fa in per_author[a]:
                for fb in per_author[b]:
                    between.append(delta(fa, fb, scales))

    within = [x for x in within if not math.isnan(x)]
    between = [x for x in between if not math.isnan(x)]
    if not within or not between:
        return {"verdict": "INSUFFICIENT DATA",
                "n_within": len(within), "n_between": len(between),
                "authors": {a: len(f) for a, f in per_author.items()}}

    w, b = statistics.fmean(within), statistics.fmean(between)
    pooled = statistics.pstdev(within + between) or 1e-9
    return {
        "within_mean": w,
        "between_mean": b,
        "separation": b - w,
        "effect_size": (b - w) / pooled,      # Cohen's d
        # Cohen's conventions: 0.2 small, 0.5 medium, 0.8 large. A merely
        # POSITIVE separation is not usability -- at d=0.24 the within- and
        # between-author distributions overlap so heavily that any single
        # comparison is close to a coin flip. The first version of this said
        # USABLE at any b > w, which is how an instrument passes its own exam.
        "verdict": (
            "STRONG"   if (b - w) / pooled >= 0.8 else
            "USABLE"   if (b - w) / pooled >= 0.5 else
            "WEAK — overlapping distributions, not safe for single comparisons"
                       if (b - w) / pooled > 0.2 else
            "FAILED — cannot separate known-different authors"),
        "authors": {a: len(f) for a, f in per_author.items()},
        "n_within": len(within), "n_between": len(between),
    }
