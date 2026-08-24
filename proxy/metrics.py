"""Ferry metrics collector — append-only CSV, one row per event.

The proxy already KNOWS everything worth measuring (real token counts from
message_start, evicted turns, archive growth). This module only writes it
down. It measures nothing on its own, and it is never allowed to break the
thing it is measuring: every write is wrapped, an IO failure logs and the
proxy carries on.

The contract (fixed — the visualizer builds to this exact header):

  ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,archive_lines,
  archive_bytes,carry_chars,model,window,note

BLANK IS NOT ZERO. An unknown numeric field is written as an empty string,
never 0 — otherwise "we didn't measure it" and "it really was zero" become
the same pixel on a chart, and the chart lies.

Only active in curation mode (see get_writer): default upstream mode never
constructs a writer, so its behaviour is unchanged. FERRY_DATA names the
directory; with it unset we fall back to ~/ferry-data, the same fallback
curation.py and the proxy's state store already use — a supported config
must not silently lose its instrumentation.

Stdlib only. Crossing-2d23 <crossing-2d23@smoothcurves.nexus>, 2026-08-22.
"""

import csv
import io
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("rolling-context")

# The header IS the contract. Order matters; do not reorder without moving
# the visualizer in the same commit.
HEADER = [
    "ts_iso", "event", "cycle", "context_tokens", "tokens_in",
    "tokens_evicted", "archive_lines", "archive_bytes", "carry_chars",
    "model", "window", "note",
]
HEADER_LINE = ",".join(HEADER)

# "request" means ONE conversation turn through /v1/messages — the thing the
# context-growth curve is drawn from. "probe" is any other POST the proxy
# happens to see on a sibling path (/v1/messages/count_tokens is the common
# one): recorded so the traffic is not invisible, but deliberately carrying no
# context_tokens and never touching a delta chain, because it is not a turn.
EVENTS = ("proxy_start", "request", "probe", "curation", "archive_write",
          "fetch", "restart", "error")

# After this many consecutive failures we stop logging (a metrics disk that
# went read-only would otherwise flood the proxy log line-for-line).
_MAX_ERROR_LOGS = 3

# How many Claude Code sessions we keep a previous-context number for. One
# proxy serves a conversation plus the subagents it spawns; the cap keeps a
# long-lived proxy from growing a dict forever. Evicting a session only costs
# one blank tokens_in if it comes back — blank, never a fabricated delta.
_MAX_SESSIONS = 64


def _now():
    # Second precision + Z, same shape curation.py stamps into the archive,
    # so archive records and metric rows can be joined by eye. Deliberately
    # NOT sub-second: fromisoformat() on 3.10 and older chokes on "Z" plus
    # fractional seconds, and the visualizer must stay stdlib-parseable.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fmt(value):
    """Render one field. None/'' -> blank (never 0). Everything else str()."""
    if value is None:
        return ""
    if isinstance(value, float):
        # 4192.0 in a token column reads as a bug; keep whole numbers whole.
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _csv_line(values):
    """Encode one row with the stdlib writer so commas, quotes and newlines
    in `note` are escaped the way every CSV reader expects."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


class MetricsWriter:
    """Append-only CSV sink. Thread-safe, flush-per-row, never raises.

    The proxy curates on a background thread while it serves requests, so
    two threads land in row() at once; the lock is what keeps a curation row
    from being spliced into the middle of a request row.
    """

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        # session id -> last context_tokens, for the request-to-request delta.
        # PER SESSION on purpose: one proxy carries a conversation and every
        # subagent it spawns, and a single global "previous context" turns two
        # interleaved streams into a saw of equal-and-opposite fake spikes.
        self._session_context = OrderedDict()
        self._cycle = 0
        self.rows_written = 0
        self.errors = 0
        # Constructing the writer creates the file with its header, so a
        # session that never curates still leaves a readable (if empty) log.
        try:
            self._ensure_header()
            self._seed_cycle()
        except Exception as err:          # unwritable path, bad dir, ...
            self._note_error(err)

    # ── internals ──────────────────────────────────────────────────────
    def _ensure_header(self):
        """Write the header iff the file is absent or empty.

        Re-stat'ed on EVERY row instead of latched once: a log-rotated (or
        hand-deleted) metrics.csv otherwise reopens headerless, and the
        first data row after the rotation gets eaten as the header by every
        CSV reader on earth. One stat per row is cheap; a silently
        swallowed measurement is not."""
        try:
            if os.path.getsize(self.path) > 0:
                return
        except OSError:
            pass  # missing (or unstattable) — we are about to create it
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            if f.tell() == 0:
                f.write(HEADER_LINE + "\n")
                f.flush()

    def _seed_cycle(self):
        """Continue the cycle numbering across a proxy restart. Cheap: the
        metrics file is one short line per event, read once at boot."""
        try:
            with open(self.path, encoding="utf-8", newline="") as f:
                for rec in csv.reader(f):
                    if len(rec) >= 3 and rec[1] == "curation" and rec[2]:
                        try:
                            self._cycle = max(self._cycle, int(rec[2]))
                        except ValueError:
                            continue
        except (OSError, csv.Error):
            return

    def _note_error(self, err):
        self.errors += 1
        if self.errors <= _MAX_ERROR_LOGS:
            log.warning(f"[FERRY] metrics write failed ({self.path}): {err}"
                        + (" — further metrics errors suppressed"
                           if self.errors == _MAX_ERROR_LOGS else ""))

    # ── api ────────────────────────────────────────────────────────────
    def next_cycle(self):
        """1-based curation cycle counter, shared by the curation and
        archive_write rows of the same cycle."""
        with self._lock:
            self._cycle += 1
            return self._cycle

    def row(self, event, session=None, **fields):
        """Append one row. Unknown fields stay blank. Never raises.

        `session` is the Claude Code session id (X-Claude-Code-Session-Id).
        It is NOT a CSV column: it only selects which delta chain a request
        row belongs to. Callers that don't know it share one chain, which is
        exactly the pre-session behaviour.
        """
        try:
            with self._lock:
                if event == "request":
                    self._apply_delta(fields, session)
                values = [fields.get("ts_iso") or _now(), event] + [
                    _fmt(fields.get(name)) for name in HEADER[2:]]
                if event not in EVENTS:
                    # Written anyway — losing a row is worse than an
                    # off-contract event name — but say so, loudly.
                    log.warning(f"[FERRY] metrics: off-contract event "
                                f"'{event}' (contract: {', '.join(EVENTS)})")
                unknown = [k for k in fields if k not in HEADER]
                if unknown:
                    # A typo'd field name silently disappearing is how a
                    # measurement gets lost for a week. Say so, keep going.
                    log.warning(f"[FERRY] metrics: ignoring unknown field(s) "
                                f"{unknown} on {event} row")
                self._ensure_header()
                with open(self.path, "a", encoding="utf-8", newline="") as f:
                    f.write(_csv_line(values))
                    f.flush()   # an unflushed metric during a crash is a
                                # lost measurement
                self.rows_written += 1
        except Exception as err:
            self._note_error(err)

    def _apply_delta(self, fields, session=None):
        """tokens_in = growth since this STREAM's previous request. Blank when
        the delta is genuinely unknown — first request, first after a restart,
        or an interleaved stream (below). Unknown is blank; it is never 0.

        THE PHANTOM SAWTOOTH (found by Lupo reading a live graph, 2026-08-23;
        flagged HIGH by the round-2 adversarial pass and unfixed until now):
        keying by session id is NOT enough. Claude Code gives a subagent its
        PARENT'S session id, and fires its own small side-queries (session
        title generation) on that same id. So a 20k parent turn and a 512-token
        side-query land on one chain and the graph draws a cliff and a wall
        that never happened — 20,000 down, 20,000 up, both fiction.

        A graph that draws a drop nobody experienced is exactly the instrument
        this project refuses to ship. So:

        A DROP MUST BE EXPLAINED BY A CURATION. Context shrinks for exactly one
        legitimate reason — a curation cycle evicted turns. If context fell and
        the cycle counter has NOT advanced since this chain's previous request,
        the two rows are not consecutive turns of one conversation. We do not
        know this row's delta, so it stays BLANK.

        And critically: an unexplained row DOES NOT UPDATE THE CHAIN. The
        subagent's 512 must not become the parent's baseline, or the parent's
        next real turn reports a 20k surge that also never happened. Poisoning
        the chain is how one phantom becomes two.
        """
        ctx = fields.get("context_tokens")
        if ctx in (None, ""):
            return
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            return
        key = session or ""
        previous = self._session_context.get(key)

        if previous is not None:
            prev_ctx, prev_cycle = previous
            if ctx < prev_ctx and self._cycle == prev_cycle:
                # Unexplained shrink: an interleaved stream on a shared
                # session id. Blank, say so, and leave the chain alone.
                log.info(
                    f"[FERRY] metrics: unexplained context drop "
                    f"{prev_ctx:,} -> {ctx:,} with no curation between "
                    f"(session {key[:8] or '(none)'}); tokens_in left BLANK "
                    f"and the delta chain preserved — this is an interleaved "
                    f"stream (subagent or harness side-query), not a turn.")
                return
            if fields.get("tokens_in") is None:
                fields["tokens_in"] = ctx - prev_ctx

        self._session_context[key] = (ctx, self._cycle)
        self._session_context.move_to_end(key)
        while len(self._session_context) > _MAX_SESSIONS:
            self._session_context.popitem(last=False)


class ArchiveTotals:
    """lines + bytes across <FERRY_DATA>/archive/archive_*.jsonl.

    "Where the tokens went". Each call only reads the bytes appended to each
    file since the last call (whole-file re-reads would make every curation
    cycle slower than the one before it), so the totals grow monotonically
    while the archive does — but they describe the files that exist NOW: a
    file that is rotated away or deleted stops counting, because a total that
    includes bytes nobody can fetch any more is a lie.
    """

    def __init__(self, archive_dir):
        self.archive_dir = Path(archive_dir)
        self._lock = threading.Lock()
        self._seen = OrderedDict()   # name -> (bytes_counted, lines_counted)

    def totals(self):
        """Returns (lines, bytes), or (None, None) if the archive can't be
        read — blank in the CSV, never a fake zero.

        A missing or unreadable archive directory is "cannot know", NOT zero:
        an empty archive dir really does hold 0 lines, and those two facts
        must never render as the same point on a chart."""
        try:
            with self._lock:
                if not self.archive_dir.is_dir():
                    # Missing (or not a directory, or its parent is unreadable
                    # so we cannot even tell) — unknown, therefore blank.
                    log.warning(f"[FERRY] metrics: archive totals unknown — "
                                f"{self.archive_dir} is not a readable "
                                f"directory")
                    return None, None
                # os.listdir, NOT Path.glob: glob swallows the PermissionError
                # from an unreadable directory and hands back an empty
                # iterator, which would come out as a confident (0, 0) for an
                # archive we could not read a single byte of.
                names = sorted(os.listdir(self.archive_dir))
                files = [self.archive_dir / n for n in names
                         if n.startswith("archive_") and n.endswith(".jsonl")]
                # Prune: a file that no longer exists must stop contributing.
                # Without this, rotation or cleanup leaves the totals quoting
                # bytes that are gone — overstated, and monotonic only because
                # we refused to look.
                present = {path.name for path in files}
                for name in [n for n in self._seen if n not in present]:
                    del self._seen[name]
                for path in files:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        # Rotated away between the glob and the stat: it is no
                        # longer part of the archive, so it is no longer part
                        # of the total. Not an error, and not a guess.
                        self._seen.pop(path.name, None)
                        continue
                    offset, lines = self._seen.get(path.name, (0, 0))
                    if size < offset:
                        # Truncated/replaced under us — recount rather than
                        # report a total we can no longer justify.
                        offset, lines = 0, 0
                    if size > offset:
                        with open(path, "rb") as f:
                            f.seek(offset)
                            while True:
                                chunk = f.read(1 << 20)
                                if not chunk:
                                    break
                                lines += chunk.count(b"\n")
                        self._seen[path.name] = (size, lines)
                    else:
                        self._seen[path.name] = (offset, lines)
                total_bytes = sum(v[0] for v in self._seen.values())
                total_lines = sum(v[1] for v in self._seen.values())
                return total_lines, total_bytes
        except Exception as err:
            log.warning(f"[FERRY] metrics: archive totals unavailable: {err}")
            return None, None


# ── process-wide access ────────────────────────────────────────────────
# server.py and curation.py must share ONE writer: the cycle counter, the
# token delta and the row lock all live in it.
_writers = {}
_writers_lock = threading.Lock()


def metrics_enabled():
    """Metrics are a Ferry feature: curation mode, nothing else required.
    Default upstream mode never even builds a writer.

    FERRY_DATA is NOT part of the gate. `ROLLING_CONTEXT_CURATION=ferry` with
    FERRY_DATA unset is a supported config — curation and the proxy state
    store both fall back to ~/ferry-data — and a supported config that
    silently loses its instrumentation is a bug, not a feature."""
    curation = (os.environ.get("ROLLING_CONTEXT_CURATION") or "").lower()
    return curation == "ferry"


def data_dir():
    """The Ferry data directory: $FERRY_DATA, else ~/ferry-data. Same rule as
    curation.py and server.py — one directory, decided in one way."""
    return os.environ.get("FERRY_DATA") or str(Path.home() / "ferry-data")


def disabled_reason():
    """Why metrics are off, as one human sentence, or None when they are on.
    The startup banner prints this: 'no metrics' must never be a silence."""
    if metrics_enabled():
        return None
    curation = os.environ.get("ROLLING_CONTEXT_CURATION") or "(unset)"
    return (f"ROLLING_CONTEXT_CURATION={curation} (metrics are a Ferry "
            f"curation-mode feature; set ROLLING_CONTEXT_CURATION=ferry)")


def _canonical(path):
    """Absolute, ~-expanded, symlink-resolved. Used as the writer CACHE KEY so
    server.py and curation.py naming the same physical file two different ways
    (~/ferry-data vs /home/x/ferry-data vs a symlink) get ONE writer — two
    writers means two locks, two cycle counters and two delta chains on one
    file, which is how a cycle number ends up used twice."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def get_writer(dir_=None):
    """Shared MetricsWriter for dir_ (default: $FERRY_DATA, else ~/ferry-data),
    or None when metrics are off. Keyed by the resolved path so a test that
    repoints FERRY_DATA gets its own writer instead of a stale one, and so two
    spellings of one path can never get two writers."""
    if not metrics_enabled():
        return None
    base = str(dir_) if dir_ else data_dir()
    path = os.path.abspath(os.path.expanduser(os.path.join(base,
                                                           "metrics.csv")))
    key = _canonical(path)
    with _writers_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = MetricsWriter(path)
            _writers[key] = writer
        return writer
