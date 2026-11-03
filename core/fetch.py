#!/usr/bin/env python3
"""ferry-fetch — pull archived turns back into context by pointer.

The other half of the pointer slug. Curation moves turns out of the
window ("moved, not lost"); this is how a mind reaches them again:

    ferry-fetch 'archive_20260811.jsonl#L1'
    ferry-fetch 'archive_20260811.jsonl#L3-L7'
    ferry-fetch --json 'archive_20260811.jsonl#L1'

Reads $FERRY_DATA/archive/ (default ~/ferry-data/archive/). Prints the
verbatim archived turns with role labels; media appears as blob refs
(sha256 + media type) — reconstitute with ferry-extract (media phase).

Any Claude Code mind can call this through its Bash tool — no MCP
registration, no chassis coupling. A pointer in your carried context plus
this command IS recall.

Exit: 0 found · 1 not found / bad pointer · 2 bad usage.
Stdlib only. Crossing-2d23, 2026-08-11.
"""

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

# The metrics contract, duplicated on purpose: ferry-fetch lives in the Ferry
# core tree and must run with nothing but the stdlib on sys.path — importing
# proxy/metrics.py would make the fork a dependency of the fetch verb. Thirteen
# columns, this exact order; mirrors proxy/metrics.py HEADER. If that header
# ever moves, this line moves with it (tests/test_metrics.py asserts the
# contract line, and core/test_fetch.py asserts this file writes it).
METRICS_HEADER = [
    "ts_iso", "event", "cycle", "context_tokens", "tokens_in",
    "tokens_evicted", "archive_lines", "archive_bytes", "carry_chars",
    "model", "window", "note",
    # seq: proxy-only, appended LAST in metrics.py; a fetch row leaves it
    # blank ("no seq"), never 0. Added when core/ moved into the repo
    # (2026-11-03) and this test could finally run against the real header.
    "seq",
]

POINTER_RE = re.compile(
    r"^(?P<file>archive_[0-9]{8}\.jsonl)#L(?P<first>[0-9]+)(?:-L?(?P<last>[0-9]+))?$")


def parse_pointer(pointer):
    """'archive_YYYYMMDD.jsonl#L3' or '...#L3-L7' -> (file, first, last).
    Returns None on malformed pointers (never guesses)."""
    m = POINTER_RE.match(pointer.strip())
    if not m:
        return None
    first = int(m.group("first"))
    last = int(m.group("last") or first)
    if first < 1 or last < first:
        return None
    return m.group("file"), first, last


def _whoami():
    """Who ran this fetch. The row is worthless for measurement without it.

    2026-08-31: I tested the recall MECHANISM by running ferry-fetch twice
    with FERRY_DATA pointed at a test instance's data directory. Both fetches
    logged correctly -- into that instance's metrics.csv, indistinguishable
    from the instance reaching into its own past. Twenty minutes later I read
    "ferry: 2 fetch rows" and briefly believed I had observed the exact
    behaviour I had been waiting weeks to see.

    A fetch row without an owner cannot separate the experimenter from the
    subject, and the entire point of the row is to answer "did a MIND reach
    back". Never getlogin(): it reads the controlling terminal and there is
    not one. The effective UID is what actually ran the process.

    Falls back to a marker rather than raising -- this is instrumentation,
    and recall must never fail because instrumentation did.
    """
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        try:
            return "uid%d" % os.geteuid()
        except Exception:
            return "unknown"


def _log_fetch(data_dir, pointer, turn_count):
    """Append one `fetch` row to <data_dir>/metrics.csv, or do nothing.

    Recall is the other half of the crossing: without this row the metrics
    file shows turns leaving the window and never shows one coming back, so
    the archive reads as a landfill instead of a ferry.

    Opt-in via FERRY_DATA: an instrumented session sets it (the proxy, the
    producer and the archive all live under it), and a bare `ferry-fetch` on
    a machine that never asked for measurement must not start creating CSVs
    in a home directory.

    Every unknown column is blank, never 0 — a fetch genuinely does not know
    the context size, the cycle or the window. Only the note is populated.

    Wrapped whole and silent: a fetch that cannot log itself is still a
    fetch, and recall must never fail because instrumentation did."""
    if not os.environ.get("FERRY_DATA"):
        return
    try:
        path = Path(data_dir) / "metrics.csv"
        # One short line, opened O_APPEND: the proxy may be writing the same
        # file from another process, and a sub-4K append is atomic.
        fresh = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            if fresh:
                w.writerow(METRICS_HEADER)
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "fetch", "", "", "", "", "", "", "", "", "",
                        f"{pointer}, turns={turn_count}, by={_whoami()}",
                        ""])  # seq: blank (proxy-only), never 0
            f.flush()
    except Exception as e:
        # NEVER FATAL — but never SILENT either.
        #
        # 2026-08-26: this bare swallow destroyed the central finding of a
        # 15-hour run. Three fairies had read-only ACLs on their own
        # metrics.csv (`ls -l` showed -rw-rw-r--, the ACL said
        # `group::r-x  #effective:r--` — the mask is what ls prints). Every
        # fetch they made succeeded and left no trace, and I concluded from
        # zero rows that no mind had ever reached into its archive. I told the
        # human it might reframe the whole project.
        #
        # An audit trail that fails quietly is worse than none: absence of
        # evidence became evidence of absence, and it was load-bearing.
        print(f"ferry-fetch: WARNING — recall SUCCEEDED but could not be "
              f"logged to {data_dir}/metrics.csv ({e.__class__.__name__}: {e}). "
              f"Fetch counts from this instance are UNDERCOUNTS, not zeroes.",
              file=sys.stderr)
        return


def fetch(pointer, data_dir=None):
    """Pointer -> list of archived turn records (verbatim, as stored).
    Raises FileNotFoundError / ValueError with plain messages."""
    parsed = parse_pointer(pointer)
    if not parsed:
        raise ValueError(f"malformed pointer: {pointer!r} "
                         "(expected archive_YYYYMMDD.jsonl#L<n>[-L<m>])")
    fname, first, last = parsed
    data = Path(data_dir or os.environ.get(
        "FERRY_DATA", str(Path.home() / "ferry-data")))
    path = data / "archive" / fname
    if not path.exists():
        raise FileNotFoundError(f"no such archive: {path}")
    turns = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if n > last:
                break
            if n >= first and line.strip():
                turns.append(json.loads(line))
    if len(turns) != last - first + 1:
        raise ValueError(
            f"pointer {pointer} spans lines {first}-{last} but "
            f"{path.name} has only {n} line(s)")
    # Logged only once the turns are in hand, and never before the return:
    # the row records a recall that actually happened.
    _log_fetch(data, pointer, len(turns))
    return turns


def render(turns):
    """Human/model-readable rendering of fetched turns, verbatim text."""
    out = []
    for t in turns:
        out.append(f"--- [{t.get('role', '?')}] "
                   f"(archived {t.get('archived_ts', '?')}) ---")
        content = t.get("content", "")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    out.append(str(block))
                elif block.get("type") == "text":
                    out.append(block.get("text", ""))
                elif block.get("type") == "ferry_blob":
                    out.append(f"[blob: {block.get('media_type', '?')} "
                               f"sha256-{block.get('sha256', '?')} — "
                               "reconstitute with ferry-extract]")
                elif block.get("type") == "tool_use":
                    out.append(f"[tool_use {block.get('name', '?')}: "
                               f"{json.dumps(block.get('input', {}))[:500]}]")
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c
                                     if isinstance(b, dict))
                    out.append(f"[tool_result: {c if isinstance(c, str) else ''}]")
                else:
                    out.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(out)


def main(argv):
    args = [a for a in argv if a != "--json"]
    as_json = "--json" in argv
    if len(args) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        turns = fetch(args[0])
    except (ValueError, FileNotFoundError) as e:
        print(f"ferry-fetch: {e}", file=sys.stderr)
        return 1
    if as_json:
        json.dump(turns, sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(render(turns))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
