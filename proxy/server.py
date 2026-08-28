"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Uses content-based matching: hashes each message, recognizes previously compressed
messages by their content, and replaces them with the compressed version.
No sessions, no fingerprints — just content recognition.

Pure stdlib — no external dependencies needed.
"""

import hashlib
import json
import os
from collections import OrderedDict
import sys
import logging
import threading
import time
import ssl
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import endpoints
import switch
try:
    # Instrumentation, never a dependency: the proxy must still start (and in
    # default upstream mode behave byte-identically) if metrics.py is absent.
    import metrics
except Exception:                                   # pragma: no cover
    metrics = None
from compressor import RollingCompressor

class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
# encoding is explicit: without it Windows falls back to cp1252 and the em
# dashes in these log messages land as mojibake.
_log_handler = FlushFileHandler(_log_path, mode="a", encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), _log_handler],
)
log = logging.getLogger("rolling-context")

LISTEN_PORT = endpoints.LISTEN_PORT

UPSTREAM_URL = endpoints.load_upstream(LISTEN_PORT)
TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER") or "100000")
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET") or "40000")

# ---------------------------------------------------------------------------
# THRASH DETECTOR — convergence, not rate.
#
# 2026-08-26: three instances burned 12.1 MILLION input tokens in three minutes
# while moving almost nothing. 21 curations in two minutes, two turns evicted
# each time, context pinned in the low 83,000s against a target of 12,000.
#
# The cause was an IRREDUCIBLE FLOOR: system prompt + MCP tool definitions +
# CLAUDE.md came to ~83,000 tokens, none of which is a conversation turn, so
# Ferry could not evict any of it. A target below the floor is unsatisfiable,
# so every request re-triggered and evicted the only two turns available.
#
# Lupo, shown the log, asked the right question: might it just be "doing the
# right thing only really really fast"? That is what makes rate the wrong
# signal. Fast curation that reaches target is Ferry working; fast curation
# that plateaus is Ferry on a treadmill, and from outside they look identical.
#
#   THRASHING IS HIGH ACTIVITY WITH NO CONVERGENCE TOWARD TARGET.
#
# So measure WHERE A CURATION LANDS, not how often one happens. Note that
# comparing trigger-to-trigger would false-positive on a HEALTHY run: after a
# good curation the context climbs back to the trigger, so consecutive triggers
# always sit at about the same level. The landing point is the honest signal.
#
# Observed, and all three cases must classify correctly:
#   thrash     landed ~83,840  target  12,000  -> unproductive
#   fairie     landed  96,277  target 100,000  -> productive (under)
#   passenger  landed 111,007  target 100,000  -> productive (short but real)
#
# A single short cycle self-corrects on the next trigger. A SEQUENCE of them
# that never reaches target is thrashing wearing a larger number, so we strike
# out rather than trip on one.
# ---------------------------------------------------------------------------
# HYSTERESIS AND THE TWO-MODE RATE LIMITER (DESIGN-REV2 §20.3, §25).
#
# THE BUG: the trigger has no memory of having fired. Curation runs in the
# background and only takes effect on a LATER request, so Ferry re-triggers on
# the pre-curation context and evicts a handful of turns for nothing. Observed
# repeatedly: cycles evicting 851 / 1,182 / 1,291 / 2,320 tokens back to back.
#
# THE FIX is not a timer. `_convergence["awaiting"]` is already set when a cycle
# starts and cleared when its curated prefix is OBSERVED in an incoming request
# (see _thrash_note_landing). That is exactly the question "has the last cycle
# landed yet?" -- an OBSERVATION, not a guess about how long compression takes.
# Do not fire again until it has.
#
# THE RATE LIMITS are Lupo's (2026-08-26): a minimum interval, and a cap per
# window. And so is the caveat that makes them two-mode -- a limiter that is
# correct in the steady state can be catastrophic in a panic:
#
#   "a model pulls in something huge, goes way over budget, and ferry freaks
#    out... ferry needs to do 5 or 6 max evictions to pull a model back under
#    target.. if a tuning parameter prevents that kind of reaction... that could
#    lead to a less than desirable outcome."
#
# So RECOVERY MODE suspends the rate limits while context is above trigger AND
# the last cycle actually moved it down. It never suspends the landing check:
# firing before the previous cycle lands is useless in every mode.
#
#   A limiter must prevent THRASHING and never prevent WORKING,
#   and "is it still coming down?" is what tells those apart.
#
# That is the same discriminator as the thrash detector: convergence, not rate.
# How long to wait for a cycle to be OBSERVED landing before assuming it never
# will. Without this the landing check deadlocks: `awaiting` is set when the
# compression thread starts and cleared only when a request is seen carrying the
# curated prefix — so a compression that FAILS, or a client that simply stops
# talking, pins it True forever and Ferry never curates again.
#
# Third time this exact shape has bitten in one day (thrash lockout 08:44Z,
# unreachable-target refusal 12:00Z, this). The pattern: a guard whose release
# condition depends on the very thing the guard prevents. Every such guard needs
# a second exit that does not.
LANDING_TIMEOUT = float(os.environ.get("ROLLING_CONTEXT_LANDING_TIMEOUT") or "90")
MIN_CYCLE_INTERVAL = float(os.environ.get("ROLLING_CONTEXT_MIN_INTERVAL") or "10")
MAX_CYCLES_PER_WINDOW = int(os.environ.get("ROLLING_CONTEXT_MAX_CYCLES") or "4")
CYCLE_WINDOW_SECONDS = float(os.environ.get("ROLLING_CONTEXT_CYCLE_WINDOW") or "300")

_hysteresis = {
    "awaiting_since": 0.0,
    "last_cycle_at": 0.0,
    "recent": [],          # monotonic timestamps of recent cycle starts
    "last_trigger_ctx": None,
}


def _hysteresis_gate(total_input, now, log):
    """Return (allowed: bool, reason: str). Pure decision, no side effects."""
    prev_ctx = _hysteresis["last_trigger_ctx"]
    falling = prev_ctx is not None and total_input < prev_ctx
    recovery = falling and total_input > TRIGGER_TOKENS

    # 1. THE LANDING CHECK — never suspended by recovery, but it MUST time out.
    if _convergence["awaiting"]:
        waited = now - _hysteresis["awaiting_since"]
        if waited < LANDING_TIMEOUT:
            return False, (f"previous cycle has not landed yet ({waited:.0f}s; "
                           f"no request has carried its curated prefix); firing "
                           f"now would evict a handful of turns for nothing")
        # Never observed. The compression may have failed, or the client may
        # simply not echo the prefix. Proceed rather than stall forever.
        _convergence["awaiting"] = False
        return True, (f"previous cycle never observed landing after "
                      f"{waited:.0f}s — proceeding rather than stalling")

    # 2/3. RATE LIMITS — suspended in recovery.
    if recovery:
        # prev_ctx cannot be None here (recovery requires `falling`, which
        # requires it) -- but that is an IMPLICIT coupling, and mutation testing
        # broke it in one edit: dropping `falling` from the recovery condition
        # crashed this line with TypeError on None.__format__. A diagnostic that
        # can crash the request path is not a diagnostic. Format defensively.
        _was = f"{prev_ctx:,}" if prev_ctx is not None else "?"
        return True, (f"recovery mode: {_was} -> {total_input:,} and still "
                      f"above trigger")

    since = now - _hysteresis["last_cycle_at"]
    if _hysteresis["last_cycle_at"] and since < MIN_CYCLE_INTERVAL:
        return False, (f"only {since:.1f}s since the last cycle "
                       f"(minimum {MIN_CYCLE_INTERVAL:.0f}s)")

    recent = [t for t in _hysteresis["recent"] if now - t < CYCLE_WINDOW_SECONDS]
    _hysteresis["recent"] = recent
    if len(recent) >= MAX_CYCLES_PER_WINDOW:
        return False, (f"{len(recent)} cycles in the last "
                       f"{CYCLE_WINDOW_SECONDS:.0f}s (max {MAX_CYCLES_PER_WINDOW}) "
                       f"and context is not falling")
    return True, "ok"


def _hysteresis_note_cycle(total_input, now):
    _hysteresis["awaiting_since"] = now
    _hysteresis["last_cycle_at"] = now
    _hysteresis["recent"].append(now)
    _hysteresis["last_trigger_ctx"] = total_input


THRASH_TOLERANCE = float(os.environ.get("ROLLING_CONTEXT_THRASH_TOLERANCE") or "1.25")
THRASH_STRIKES = int(os.environ.get("ROLLING_CONTEXT_THRASH_STRIKES") or "3")


def _note_gate(reason, total_input, model=None, window=None):
    """Record a curation that was WANTED and DECLINED.

    One helper, three call sites, so the contract of a gate row is stated in
    exactly one place. Written as a helper rather than inline specifically so
    it can be mutation-tested: the first version of this lived inline at each
    site, and a mutation that made a gate row claim `tokens_evicted=0`
    SURVIVED a green suite, because the test was asserting against rows the
    test itself had written. A fixture I invented cannot test a path I wrote.

    A gate row carries the context it saw and WHY. It must never carry a
    cycle number or tokens_evicted: blank is not zero, and a 0 in
    tokens_evicted is a curation that RAN and moved nothing -- the 12.1M
    token failure -- which is a different and far worse event than one that
    correctly declined to run.
    """
    if not _metrics:
        return
    _metrics.row("gate", context_tokens=total_input or None,
                 model=model, window=window, note=reason)


def _params_note():
    """Every tuning knob this proxy is actually running with, as 'k=v, k=v'.

    Written into the proxy_start row so the visualizer draws the watermark
    lines from the values the PROXY used, not from defaults typed into a
    page. A trigger line at 100k over a run started at 150k is not a cosmetic
    error -- it is a graph that says "converging" about a run that wasn't.

    Read at call time, never cached: the tests rebind these constants, and a
    note computed at import would describe a proxy that never ran.
    """
    from compressor import MIN_KEEP_RATIO
    return (f"mode=ferry, trigger={TRIGGER_TOKENS}, target={TARGET_TOKENS}, "
            f"min_keep={MIN_KEEP_RATIO}, "
            f"landing_timeout={LANDING_TIMEOUT}, "
            f"min_interval={MIN_CYCLE_INTERVAL}, "
            f"max_cycles={MAX_CYCLES_PER_WINDOW}, "
            f"cycle_window={CYCLE_WINDOW_SECONDS}, "
            f"thrash_tolerance={THRASH_TOLERANCE}, "
            f"thrash_strikes={THRASH_STRIKES}, "
            f"port={LISTEN_PORT}")

_convergence = {
    "awaiting": False,     # a curation is in flight; watch where it lands
    "strikes": 0,          # consecutive landings that missed target
    "locked_out": False,   # stop curating: we are provably not converging
    "last_landing": None,
    "floor_at_lockout": None,
}


def _thrash_note_landing(total_input, log, metrics, model=None, window=None):
    """Called on the first request carrying a freshly injected prefix.

    That request's token count IS the landing point of the curation that
    produced it -- the one number that says whether the cycle accomplished
    anything. Everything else (turns evicted, bytes archived) can look healthy
    while the resident set does not move.
    """
    if not _convergence["awaiting"]:
        return
    _convergence["awaiting"] = False
    _convergence["last_landing"] = total_input
    ceiling = TARGET_TOKENS * THRASH_TOLERANCE
    if total_input <= ceiling:
        if _convergence["strikes"]:
            log.info(f"[FERRY] convergence restored: landed at {total_input:,} "
                     f"(target {TARGET_TOKENS:,}) -- strike count reset")
        _convergence["strikes"] = 0
        return
    _convergence["strikes"] += 1
    log.warning(
        f"[FERRY] curation landed at {total_input:,}, target {TARGET_TOKENS:,} "
        f"(ceiling {ceiling:,.0f}) -- strike {_convergence['strikes']}/{THRASH_STRIKES}")
    if _convergence["strikes"] >= THRASH_STRIKES and not _convergence["locked_out"]:
        # Announce ONCE, on the transition. A warning that repeats every cycle
        # is a warning people learn to scroll past -- and this one has to still
        # be findable in a log six hours later.
        _convergence["locked_out"] = True
        _convergence["floor_at_lockout"] = total_input
        log.warning(
            f"[FERRY] *** NOT CONVERGING -- CURATION DISABLED *** {THRASH_STRIKES} "
            f"consecutive cycles failed to reach target. The unevictable floor is "
            f"at least {total_input:,} tokens (system prompt + tool definitions + "
            f"CLAUDE.md are not conversation turns and cannot be evicted). "
            f"Raise ROLLING_CONTEXT_TARGET above that floor and restart. "
            f"Serving requests untouched until then -- a Ferry that declines to "
            f"curate costs nothing; one that curates uselessly bills by the token "
            f"AND invalidates prompt caching on every cycle.")
        if metrics:
            metrics.row("error", model=model, window=window,
                        note=f"thrash lockout: {THRASH_STRIKES} cycles landed above "
                             f"{ceiling:,.0f}; floor >= {total_input:,}; "
                             f"target={TARGET_TOKENS}")


def _thrash_maybe_clear(total_input, log):
    """End a lockout when the situation has genuinely changed.

    TWO exits, and the second one exists because the first DEADLOCKS.
    Observed live 2026-08-26 08:36Z: passenger locked out at a floor of 134,043
    against a target of 100,000. The only clear condition was "context below
    target" -- but the lockout disables curation, and only curation can lower
    the context. Locked forever, context climbing toward the model's hard limit,
    where it would have started rejecting every request at ~180k tokens.

    A safety mechanism whose failure mode is worse than the failure it prevents
    is not a safety mechanism. It was mine, it shipped six hours earlier, and it
    had a test suite that never asked whether the exit was reachable.

    Exit 1: context below target -- a genuinely fresh, small conversation.
    Exit 2: context has grown well ABOVE the floor we locked out at, which means
            there is substantial NEW evictable material that did not exist when
            we gave up. Worth one more attempt. This makes the lockout a rate
            limiter rather than an absolute stop.
    """
    if not _convergence["locked_out"]:
        return
    if total_input < TARGET_TOKENS:
        log.info(f"[FERRY] context is {total_input:,}, below target "
                 f"{TARGET_TOKENS:,} -- clearing thrash lockout")
        _convergence["locked_out"] = False
        _convergence["strikes"] = 0
        return
    floor = _convergence.get("floor_at_lockout")
    if floor is None:
        return
    retry_at = max(TRIGGER_TOKENS, floor + max(1, (TRIGGER_TOKENS - TARGET_TOKENS) // 2))
    if total_input >= retry_at:
        log.info(f"[FERRY] context {total_input:,} is well above the "
                 f"{floor:,} floor we locked out at -- there is new evictable "
                 f"material, retrying curation once")
        _convergence["locked_out"] = False
        _convergence["strikes"] = 0
# Empty = native mode compresses with the session's own model (prompt-cache
# hit); set to pin a specific summarizer model.
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL") or ""
# After a failed compression, wait this long before trying again — otherwise a
# failing summarizer (e.g. rate-limited) gets re-hammered on every request.
FAILURE_COOLDOWN = int(os.environ.get("ROLLING_CONTEXT_FAILURE_COOLDOWN") or "300")

ssl_ctx = ssl.create_default_context()
_parsed_upstream = urlparse(UPSTREAM_URL)
UPSTREAM_PATH = _parsed_upstream.path or ""


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
    if upstream_path.endswith("/") and request_path.startswith("/"):
        return upstream_path[:-1] + request_path
    if not upstream_path.endswith("/") and not request_path.startswith("/"):
        return upstream_path + "/" + request_path
    return upstream_path + request_path


# The one path that carries a conversation turn. do_POST dispatches on the
# /v1/messages PREFIX (so sibling endpoints still get proxied correctly), but
# metrics must be stricter: /v1/messages/count_tokens is a probe, not a turn,
# and counting it would poison the context curve it shares a file with.
TURN_PATH = "/v1/messages"


def _is_turn_path(request_path: str) -> bool:
    """True only for the exact /v1/messages endpoint (query string and a
    trailing slash don't change what it is). Metrics-only: routing is
    unchanged."""
    path = urlparse(request_path or "").path.rstrip("/")
    return path == TURN_PATH


compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
)


def _upstream_conn():
    """Create a connection to the upstream server."""
    if _parsed_upstream.scheme == "https":
        return http.client.HTTPSConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 443,
            context=ssl_ctx,
            timeout=600,
        )
    else:
        return http.client.HTTPConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 80,
            timeout=600,
        )


# ---------------------------------------------------------------------------
# Content-based matching
# ---------------------------------------------------------------------------

import re

_VOLATILE_TAGS_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|local-command-stdout|"
    r"available-deferred-tools)>.*?</(?:system-reminder|local-command-caveat|"
    r"local-command-stdout|available-deferred-tools)>",
    re.DOTALL,
)


def _strip_volatile_tags(text: str) -> str:
    """Strip Claude Code's dynamic tags that change between requests."""
    return _VOLATILE_TAGS_RE.sub("", text)


# --- per-session toggle (issue #6) ------------------------------------------
# /rolling-context:off prints a marker; slash command output is inserted into
# the transcript inside <local-command-stdout>, so every later request in that
# conversation carries it. Reading the newest marker gives us per-session scope
# without tracking sessions — the same content recognition the proxy already
# runs on. Matching is confined to <local-command-stdout> blocks so that merely
# reading switch.py in a conversation (a tool result, not a command block)
# cannot toggle anything.

_STDOUT_BLOCK_RE = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
)
_SESSION_MARKER_RE = re.compile(r"<<rolling-context:session-(off|on)>>")


def _iter_text(content):
    """Yield the plain-text pieces of a message without serializing it.

    Deliberately not json.dumps — this runs over the whole history on every
    request, and the marker only ever lives in text.
    """
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    yield text
                nested = block.get("content")
                if isinstance(nested, (str, list)):
                    yield from _iter_text(nested)


class SessionToggleStore:
    """Latches each session's toggle against its Claude Code session id.

    The marker only ever appears in the conversation that ran the command, but
    Claude Code sends X-Claude-Code-Session-Id on every request and a subagent
    inherits its parent's session id (subagents get their own transcript, not
    their own session). Latching the marker against that id is what makes
    /rolling-context:off reach the subagents a session spawns.

    It also outlives the marker: if a conversation is /compact'ed and the
    marker is summarized away, the latch still holds.

    Bounded and lossy on purpose. Overflowing or restarting the proxy forgets a
    session, which costs savings for one more request until the marker is seen
    again — never correctness.
    """

    def __init__(self, limit=512):
        self._lock = threading.Lock()
        self._state = OrderedDict()
        self._limit = limit

    def set(self, session_id: str, disabled: bool):
        if not session_id:
            return
        with self._lock:
            previous = self._state.get(session_id)
            self._state[session_id] = disabled
            self._state.move_to_end(session_id)
            while len(self._state) > self._limit:
                self._state.popitem(last=False)
            return previous != disabled

    def get(self, session_id: str):
        if not session_id:
            return None
        with self._lock:
            if session_id not in self._state:
                return None
            self._state.move_to_end(session_id)
            return self._state[session_id]

    def __len__(self):
        return len(self._state)


session_toggles = SessionToggleStore()


def _session_disabled(messages: list):
    """Newest /rolling-context session marker in this conversation.

    Returns True (off), False (on), or None (never set — follow the machine
    setting). Scans newest-first so the last toggle wins.
    """
    for msg in reversed(messages):
        for text in _iter_text(msg.get("content", "")):
            # Cheap reject first: the vast majority of messages never match.
            if "rolling-context:session-" not in text:
                continue
            for block in reversed(_STDOUT_BLOCK_RE.findall(text)):
                found = _SESSION_MARKER_RE.findall(block)
                if found:
                    return found[-1] == "off"
    return None


def _normalize_content(content):
    """Strip volatile metadata (cache_control, system-reminder) for stable hashing."""
    if isinstance(content, str):
        return _strip_volatile_tags(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                b = {}
                for k, v in block.items():
                    if k == "cache_control":
                        continue
                    if k == "content" and isinstance(v, (list, str)):
                        b[k] = _normalize_content(v)
                    elif k == "text" and isinstance(v, str):
                        b[k] = _strip_volatile_tags(v)
                    else:
                        b[k] = v
                result.append(b)
            else:
                result.append(block)
        return result
    return content


def _hash_message(msg: dict) -> str:
    """Stable hash of a message, ignoring cache_control metadata."""
    role = msg.get("role", "")
    content = _normalize_content(msg.get("content", ""))
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    raw = f"{role}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_messages(messages: list) -> list:
    return [_hash_message(m) for m in messages]


class CompressionStore:
    """Content-based compression tracking. No sessions, no fingerprints, no keys.

    Stores a list of compressions. Each has original_hashes (what was compressed)
    and prefix (the replacement). On ANY request, scans messages — if the hashes
    match a stored compression, replaces them with the prefix.
    """

    def __init__(self, persist_path=None):
        self._lock = threading.Lock()
        self._compressions = []  # list of compression entries
        # Ferry: restart-safety. Without this, a proxy restart forgets the
        # match table, so the next request ships the client's FULL history
        # (over budget) and re-curates from scratch — re-archiving turns and
        # losing the pointer index that lived only in the injected prefix.
        # Only resolved entries (original_hashes + prefix) are persisted;
        # thread/pending/_debug are transient. Off unless a path is given, so
        # default (non-Ferry) mode is byte-identical to upstream.
        self._persist_path = persist_path
        if persist_path:
            self._load()

    def _load(self):
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                saved = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return
        for e in saved:
            if e.get("prefix") and e.get("original_hashes"):
                self._compressions.append({
                    "original_hashes": e["original_hashes"],
                    "prefix": e["prefix"],
                    "pending": None, "pending_hashes": None, "thread": None,
                })
        if self._compressions:
            log.info(f"[FERRY] restored {len(self._compressions)} compression(s) "
                     f"from {self._persist_path}")

    def persist(self):
        """Atomically write resolved entries. Call after any prefix change."""
        if not self._persist_path:
            return
        with self._lock:
            snapshot = [{"original_hashes": e["original_hashes"], "prefix": e["prefix"]}
                        for e in self._compressions if e.get("prefix")]
        tmp = f"{self._persist_path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp, self._persist_path)
        except OSError as err:
            log.warning(f"[FERRY] persist failed: {err}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def find_match(self, msg_hashes: list, messages: list = None):
        """Find a compression whose hash chain appears in msg_hashes.

        Returns the match whose chain ends furthest into the request
        (latest compression = covers the most history).
        Replaces everything up to and including the match, since the
        compression already contains a summary of everything before it.
        """
        with self._lock:
            best = None
            best_end = -1  # position in msg_hashes where the match ends
            for entry in self._compressions:
                oh = entry["original_hashes"]
                if not oh:
                    continue
                # Search for the hash chain in msg_hashes
                chain_len = len(oh)
                found = False
                for start in range(len(msg_hashes) - chain_len + 1):
                    if msg_hashes[start:start + chain_len] == oh:
                        end = start + chain_len
                        if end > best_end:
                            best = entry
                            best_end = end
                        found = True
                        break
                if not found and chain_len <= len(msg_hashes):
                    # Count total mismatches
                    mismatches = []
                    for i in range(min(chain_len, len(msg_hashes))):
                        if oh[i] != msg_hashes[i]:
                            mismatches.append(i)
                    log.warning(
                        f"[MATCH] No match: chain={chain_len} req={len(msg_hashes)} "
                        f"mismatches={len(mismatches)} at positions: "
                        f"{mismatches[:10]}{'...' if len(mismatches) > 10 else ''}"
                    )
                    # Dump content of first mismatched message for debugging
                    if mismatches and messages and entry.get("_debug_messages"):
                        idx = mismatches[0]
                        stored_msg = entry["_debug_messages"][idx] if idx < len(entry["_debug_messages"]) else None
                        incoming_msg = messages[idx] if idx < len(messages) else None
                        if stored_msg and incoming_msg:
                            s_content = str(stored_msg.get("content", ""))[:500]
                            i_content = str(incoming_msg.get("content", ""))[:500]
                            log.warning(
                                f"[MATCH] Mismatch at [{idx}] role={stored_msg.get('role')}:\n"
                                f"  STORED:   {s_content}\n"
                                f"  INCOMING: {i_content}"
                            )
            return best, best_end

    def add(self) -> dict:
        entry = {
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "thread": None,          # background compression thread
        }
        with self._lock:
            self._compressions.append(entry)
        return entry

    def remove(self, entry: dict):
        with self._lock:
            self._compressions = [e for e in self._compressions if e is not entry]
        self.persist()  # Ferry: keep the durable snapshot in sync (no-op off-mode)

    @property
    def compressions(self):
        return self._compressions


# Ferry: persist proxy state only in curation mode (default mode stays
# in-memory-only, byte-identical to upstream). Path derives from FERRY_DATA.
_ferry_curation = (os.environ.get("ROLLING_CONTEXT_CURATION") or "").lower() == "ferry"
_ferry_data = os.environ.get("FERRY_DATA") or os.path.join(
    os.path.expanduser("~"), "ferry-data")
_persist_path = os.path.join(_ferry_data, "proxy-state.json") if _ferry_curation else None
if _persist_path:
    os.makedirs(_ferry_data, exist_ok=True)
store = CompressionStore(persist_path=_persist_path)

# Ferry metrics: None in default mode, and every call site is guarded — the
# upstream path stays exactly as it was. In curation mode they follow the SAME
# data dir as the archive and the state file (FERRY_DATA, else ~/ferry-data):
# a supported config must not lose its instrumentation without saying so.
_metrics = (metrics.get_writer(_ferry_data if _ferry_curation else None)
            if metrics else None)


def _metrics_off_reason():
    """One sentence naming why there is no metrics file. Printed ONCE, in the
    startup banner (see main): a missing measurement that announces itself is
    a gap, while a missing measurement that stays quiet is a lie the graph
    tells later."""
    if metrics is None:
        return "proxy/metrics.py is not installed"
    return metrics.disabled_reason() or "unknown"


# The model's real context window is nowhere in the request or the response;
# set FERRY_WINDOW to record it (blank otherwise — a guessed window would
# turn every "% of context used" chart into fiction).
_window = os.environ.get("FERRY_WINDOW") or None
if _metrics and store.compressions:
    # Restored state, not a cold boot: the pointer index survived, so the
    # token curve continues rather than starting over.
    _metrics.row("restart", note=f"restored {len(store.compressions)} "
                                 f"compression(s) from {_persist_path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upstream_key():
    """The proxy's OWN upstream credential, if it was given one.

    FERRY_UPSTREAM_KEY_FILE is preferred over FERRY_UPSTREAM_KEY: an env var
    is visible in /proc/<pid>/environ to the process owner and leaks into
    shell history and crash dumps; a 0600 file does not.
    """
    path = os.environ.get("FERRY_UPSTREAM_KEY_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
            log.warning("[FERRY] FERRY_UPSTREAM_KEY_FILE is empty; "
                        "falling back to the caller's own credentials")
        except OSError as e:
            # Fail LOUD. Silently falling back would send the caller's dummy
            # key upstream and produce a 401 nobody can explain.
            log.error(f"[FERRY] cannot read FERRY_UPSTREAM_KEY_FILE ({e}); "
                      f"caller credentials will be used instead")
    return os.environ.get("FERRY_UPSTREAM_KEY", "").strip() or None


def _forward_headers(req_headers: dict, body: bytes = None, strip_encoding: bool = False) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower in ("host", "transfer-encoding", "connection", "content-length"):
            continue
        if strip_encoding and lower == "accept-encoding":
            continue
        headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))

    # CREDENTIAL TERMINATION. When the proxy holds its own upstream key, the
    # CLIENT NEVER NEEDS ONE. A mind talks plain HTTP to localhost and the
    # secret stops here.
    #
    # Why this matters beyond tidiness: the alternative for the Phase E fleet
    # was handing the OpenRouter key to every instance that runs Ferry — three
    # test passengers tonight, the whole family later. A key that lives in N
    # home directories is a key with N ways to leak, and these minds publish
    # tool output to a live web mirror. One unscrubbed traceback and it is on
    # the internet.
    #
    # Overrides rather than fills in: a client that sends a stale or dummy key
    # must not be able to defeat this by sending SOMETHING.
    key = _upstream_key()
    if key:
        for name in [h for h in headers if h.lower() in
                     ("authorization", "x-api-key")]:
            del headers[name]
        headers["authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key

    # Header NAMES only. Never values — one of these is a credential, and this
    # log line is read by humans over shoulders and pasted into chat.
    log.debug(f"[HDR] Forwarding headers: {list(headers.keys())}"
              f"{' (upstream key injected by proxy)' if key else ''}")
    return headers


def get_passthrough_headers(req_headers: dict) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


def _validate_tool_pairs(messages: list) -> list:
    tool_use_ids = set()
    valid_from = 0
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        if block.get("tool_use_id", "") not in tool_use_ids:
                            valid_from = i + 1
    if valid_from > 0:
        log.info(f"Dropping {valid_from} messages with orphaned tool_result references")
    return messages[valid_from:]


_compression_failed_at = 0.0

# Wall-clock time of the last compression injection — the moment old messages
# actually left the model's context. Exposed at /lean/status so companion
# plugins (nestor-lean) can invalidate "the model already saw this" knowledge.
_last_injection_ts = 0.0


def _do_background_compression(entry: dict, messages: list, auth_headers: dict,
                               real_token_count: int = None, payload: dict = None):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    global _compression_failed_at
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed = compressor.compress(messages, auth_headers,
                                         real_token_count=real_token_count, payload=payload)
        if compressed is None:
            # Nothing worth compressing — don't leave an empty entry behind
            store.remove(entry)
            return
        # compressed = [summary, ack] + recent_verbatim
        # Prefix = ONLY [summary, ack] — verbatim messages come from the
        # original request during injection, so including them in the prefix
        # would cause duplication.
        prefix = compressed[:2]
        # Key = the messages that were summarized away (not the verbatim ones).
        recent_count = len(compressed) - 2  # subtract summary + ack
        summarized = messages[:len(messages) - recent_count]
        # Skip old summary prefix if present
        from compressor import SUMMARY_MARKER
        start = 0
        if summarized and isinstance(summarized[0].get("content", ""), str):
            if SUMMARY_MARKER in summarized[0]["content"]:
                start = 2
        key_hashes = _hash_messages(summarized[start:])
        entry["pending"] = prefix
        entry["pending_hashes"] = key_hashes
        entry["_debug_messages"] = summarized[start:]  # for mismatch debugging
        log.info(
            f"[BG] Compression ready: "
            f"{compressor._count_chars(prefix):,} chars "
            f"({len(prefix)} prefix messages, key={len(key_hashes)} hashes, "
            f"summarized {len(summarized) - start} messages)"
        )
    except Exception as e:
        _compression_failed_at = time.time()
        log.error(
            f"[BG] Compression failed (cooling down {FAILURE_COOLDOWN}s): {e}",
            exc_info=True,
        )
        if _metrics:
            _metrics.row("error", context_tokens=real_token_count or None,
                         note=f"compression failed, cooling down "
                              f"{FAILURE_COOLDOWN}s: {e}")
        entry["pending"] = None


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _proxy_raw(self, method: str):
        """Raw proxy — forward request and stream response back."""
        body = self._read_body()
        headers = _forward_headers(self._get_headers_dict(), body if body else None)

        log.info(f"[RAW] {method} {self.path} -> {UPSTREAM_URL} (body={len(body)} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request(method, upstream_full_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()

            log.info(f"[RAW] Response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[RAW] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            total_bytes = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)

            log.info(f"[RAW] Done streaming {total_bytes:,} bytes")
            conn.close()
        except Exception as e:
            log.error(f"[RAW] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def do_GET(self):
        log.info(f"[REQ] GET {self.path}")
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health()
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
        elif normalized_path == "/lean/status":
            self._handle_lean_status()
        else:
            self._proxy_raw("GET")

    def do_POST(self):
        log.info(f"[REQ] POST {self.path}")
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_raw("POST")

    def do_PUT(self):
        log.info(f"[REQ] PUT {self.path}")
        self._proxy_raw("PUT")

    def do_DELETE(self):
        log.info(f"[REQ] DELETE {self.path}")
        self._proxy_raw("DELETE")

    def do_PATCH(self):
        log.info(f"[REQ] PATCH {self.path}")
        self._proxy_raw("PATCH")

    def do_OPTIONS(self):
        log.info(f"[REQ] OPTIONS {self.path}")
        self._proxy_raw("OPTIONS")

    def _handle_debug_compressions(self):
        entries = []
        for i, entry in enumerate(store.compressions):
            info = {
                "index": i,
                "hash_chain_length": len(entry.get("original_hashes") or []),
                "has_prefix": entry["prefix"] is not None,
                "prefix_content": None,
            }
            if entry["prefix"]:
                for msg in entry["prefix"]:
                    content = msg.get("content", "")
                    if isinstance(content, str) and "[ROLLING_CONTEXT_SUMMARY]" in content:
                        info["prefix_content"] = content
            entries.append(info)
        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_lean_status(self):
        """Machine-readable status for companion plugins (nestor-lean).

        last_injection_ts is global across all conversations flowing through
        this proxy — consumers must treat it as a conservative signal (a
        compression in ANY session invalidates, which only costs savings,
        never correctness).
        """
        data = {
            "status": "ok",
            "last_injection_ts": _last_injection_ts,
            "stored_compressions": len(store.compressions),
            "enabled": not switch.is_disabled(),
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        active = sum(
            1 for e in store.compressions
            if e["thread"] is not None and e["thread"].is_alive()
        )
        from compressor import NATIVE_MODE, SUMMARIZER_FORMAT
        data = {
            "status": "ok",
            "enabled": not switch.is_disabled(),
            "default_enabled": switch.config_default_enabled(),
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "summarizer_model": SUMMARIZER_MODEL or "(session model)",
            "summarizer_mode": "native" if NATIVE_MODE else f"flattened/{SUMMARIZER_FORMAT}",
            "upstream_url": UPSTREAM_URL,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "stored_compressions": len(store.compressions),
            "active_compressions": active,
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self):
        raw_body = self._read_body()
        req_headers = self._get_headers_dict()
        auth_headers = get_passthrough_headers(req_headers)

        log.info(f"[MSG] POST {self.path} (body={len(raw_body)} bytes)")
        log.debug(f"[MSG] Request headers: {list(req_headers.keys())}")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.error("[MSG] Invalid JSON in request body")
            error_body = b'{"error":"Invalid JSON"}'
            self.send_response(400)
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        model = payload.get("model", "unknown")

        # Hash all messages for content-based matching
        msg_hashes = _hash_messages(messages)
        msg_chars = compressor._count_chars(messages)

        # Claude Code sends this on every request, and a subagent inherits its
        # parent's — it gets its own transcript, not its own session. That is
        # what lets a session's toggle reach the agents it spawns.
        session_id = self.headers.get("X-Claude-Code-Session-Id") or ""

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,} "
            f"session={session_id[:8] or '(none)'}"
        )

        # /rolling-context:off — resolved fresh per request so the toggle is
        # live. Machine-wide off wins; otherwise this conversation's own marker
        # decides, and falls back to on. Disabled means "stop acting", not
        # "forget": stored compressions are left intact so turning back on
        # resumes without recompressing.
        # A marker in this request updates the latch for its session; requests
        # without one (later turns, and the subagents this session spawns) read
        # it back.
        marker_state = _session_disabled(messages)
        if marker_state is not None:
            if session_toggles.set(session_id, marker_state):
                log.info(
                    f"[MSG] Session {session_id[:8] or '(none)'} toggled "
                    f"{'OFF' if marker_state else 'ON'} by marker"
                )

        # Precedence: env kill-switch, then an explicit machine-wide off, then
        # this session's own setting, then the configured default.
        if switch.is_disabled():
            disabled, scope = True, "machine-wide"
        else:
            session_state = marker_state
            scope = "this session"
            if session_state is None:
                session_state = session_toggles.get(session_id)
                scope = "inherited from this session"
            if session_state is None:
                disabled, scope = not switch.config_default_enabled(), "config default"
            else:
                disabled = session_state
        if disabled:
            log.info(
                f"[MSG] rolling-context is OFF ({scope}) — passing through "
                f"untouched ({len(store.compressions)} compression(s) kept for later)"
            )

        # Promote any pending compressions
        promoted = False
        for entry in store.compressions:
            if entry["pending"] is not None:
                entry["prefix"] = entry["pending"]
                entry["original_hashes"] = entry["pending_hashes"]
                entry["pending"] = None
                entry["pending_hashes"] = None
                promoted = True
                log.info(
                    f"[MSG] Compression promoted: {len(entry['prefix'])} prefix messages "
                    f"replacing {len(entry['original_hashes'])} originals"
                )
        if promoted:
            store.persist()  # Ferry: durable across restart (no-op off-mode)

        # Scan: do any stored compressions match this request's messages?
        # Skipped entirely while off — find_match is also the only caller that
        # prunes no-longer-helpful entries, so not running it keeps the store
        # exactly as it was.
        match, match_end = (None, -1) if disabled else store.find_match(msg_hashes, messages)
        injected = False

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]

            # Strip cache_control from injected prefix messages ONLY.
            # The verbatim tail keeps Claude Code's cache_control breakpoints —
            # stripping those disabled prompt caching entirely, so every request
            # after the first injection paid full input-token cost (issue #1/#4).
            for msg in match["prefix"]:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

            merged_chars = compressor._count_chars(merged)
            if merged_chars < msg_chars:
                log.info(
                    f"[MSG] Injecting: {msg_chars:,} -> {merged_chars:,} chars "
                    f"({len(messages)} -> {len(merged)} messages, "
                    f"replaced 0-{match_end} with {len(match['prefix'])} prefix "
                    f"+ {len(new_messages)} new)"
                )
                payload["messages"] = merged
                msg_chars = merged_chars
                injected = True
                global _last_injection_ts
                _last_injection_ts = time.time()
            else:
                log.info(
                    f"[MSG] Compression no longer helps: "
                    f"merged={merged_chars:,} >= current={msg_chars:,} chars, removing"
                )
                store.remove(match)
                match = None

        # Save current state for post-response compression trigger
        current_messages = payload.get("messages", messages)

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        log.info(f"[MSG] Forwarding to {UPSTREAM_URL}{self.path} ({len(body):,} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request("POST", upstream_full_path, body=body, headers=headers)
            resp = conn.getresponse()

            log.info(f"[MSG] Upstream response: {resp.status} {resp.reason}")
            _upstream_status = resp.status

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[MSG] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            log.info(f"[MSG] Streaming response...")

            # Stream response and capture SSE token data
            buffer = b""
            total_bytes = 0
            total_input = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)
                if is_streaming:
                    buffer += chunk

            log.info(f"[MSG] Done streaming {total_bytes:,} bytes")

            # Extract input tokens from SSE stream
            if is_streaming and buffer:
                try:
                    text = buffer.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        evt_type = data.get("type", "")

                        # Anthropic native: usage in message_start.message.usage
                        if evt_type == "message_start":
                            usage = data.get("message", {}).get("usage", {})
                            # `or 0`, not a default: third-party Anthropic-format
                            # upstreams (OpenRouter) send these keys as explicit
                            # JSON null. `.get(k, 0)` returns None for a present
                            # null, the int+None TypeError aborted this whole
                            # loop, and the message_delta branch below — which
                            # is where those upstreams put the REAL count —
                            # was never reached. Every turn then fell back to
                            # the chars/4 estimate.
                            tokens = (
                                (usage.get("input_tokens") or 0)
                                + (usage.get("cache_creation_input_tokens") or 0)
                                + (usage.get("cache_read_input_tokens") or 0)
                            )
                            if tokens > 0:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_start: {total_input:,}")

                        # Proxy/converter: usage in message_delta.usage (e.g. CodeGate)
                        elif evt_type == "message_delta":
                            usage = data.get("usage", {})
                            tokens = int(usage.get("input_tokens") or 0)
                            if tokens > 0 and tokens > total_input:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_delta: {total_input:,}")

                    if total_input == 0:
                        sse_lines = [l for l in text.split("\n") if l.startswith("data: ")]
                        log.warning(
                            f"[MSG] No input tokens found in SSE! "
                            f"Total events: {len(sse_lines)}"
                        )
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse SSE for tokens: {e}")
            elif not is_streaming and buffer:
                try:
                    data = json.loads(buffer)
                    usage = data.get("usage", {})
                    total_input = (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                    )
                    if total_input > 0:
                        log.info(f"[MSG] Input tokens from response: {total_input:,}")
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for tokens: {e}")

            conn.close()

            # Fallback: estimate tokens from chars if SSE didn't provide usage
            estimated = False
            if total_input == 0 and msg_chars > 0:
                total_input = msg_chars // 4  # rough chars-to-tokens estimate
                estimated = True
                log.info(
                    f"[MSG] No tokens from SSE, estimating from chars: "
                    f"{msg_chars:,} chars -> ~{total_input:,} tokens"
                )

            # Ferry metrics: the REAL input tokens, as reported upstream. Note
            # says how we know them — an estimated row and a measured row must
            # never be read as the same kind of fact.
            #
            # ONLY the exact /v1/messages path is a conversation turn. Claude
            # Code also POSTs /v1/messages/count_tokens, which lands here too
            # (do_POST routes on the /v1/messages PREFIX): counting it as a
            # request would put a chars/4 pseudo-context into the curve and
            # inject two equal-and-opposite fake spikes into tokens_in as the
            # delta bounced to the probe's number and back. And the delta is
            # kept per session id, so a subagent (or a second conversation)
            # sharing this proxy can never corrupt another session's chain.
            if _metrics:
                marks = []
                if estimated:
                    marks.append("estimate:chars/4")
                if injected:
                    marks.append("injected")
                    # THIS request carries a freshly curated prefix, so its
                    # token count is where the last curation actually LANDED.
                    # It is the only number that says whether the cycle
                    # accomplished anything (see THRASH DETECTOR above).
                    if total_input:
                        _thrash_note_landing(total_input, log, _metrics,
                                             model=model, window=_window)
                if disabled:
                    marks.append("off")
                # A REJECTED REQUEST IS NOT A TURN. On 2026-08-25 three
                # instances hit an OpenRouter free-tier wall five seconds
                # after waking and spent FOURTEEN HOURS in exponential
                # backoff. Every 429 was recorded as a request row with a
                # chars/4 pseudo-context, so the graph showed ~800 healthy
                # requests per instance and a comfortable flat line while
                # nothing whatsoever was happening. 781 retries of one
                # request read as 781 turns.
                #
                # Same failure as counting count_tokens probes as turns, and
                # the same fix: record it, never as a conversation turn.
                _status = locals().get("_upstream_status")
                if _status is not None and _status != 200:
                    _metrics.row("error", model=model, window=_window,
                                 note=f"upstream {_status}; not a turn; "
                                      + ", ".join(marks))
                elif _is_turn_path(self.path):
                    _metrics.row("request", session=session_id,
                                 context_tokens=total_input or None,
                                 model=model, window=_window,
                                 note=", ".join(marks))
                else:
                    # Recorded (the traffic is real) but NOT as a turn: no
                    # context_tokens, no delta, tagged with the path so the
                    # row can never be mistaken for a conversation turn.
                    _metrics.row("probe", model=model, window=_window,
                                 note=f"non-turn POST "
                                      f"{urlparse(self.path).path}")

            # A lockout must be able to end. If the context is genuinely
            # below target the situation has changed -- a new session, a
            # smaller tool set -- and holding the old verdict would keep Ferry
            # switched off on stale evidence.
            if total_input:
                _thrash_maybe_clear(total_input, log)

            # Trigger compression based on token count. The minimum message
            # count keeps us from "compressing" sessions whose bulk is the
            # system prompt / first-message context, which we can't remove.
            if disabled:
                if total_input > TRIGGER_TOKENS:
                    log.info(
                        f"[MSG] {total_input:,} tokens is over trigger, but "
                        f"rolling-context is OFF — not compressing"
                    )
            elif (total_input > 0 and total_input > TRIGGER_TOKENS
                  and len(current_messages) >= 6 and _convergence["locked_out"]):
                # Provably not converging. Curating again would evict a couple
                # of turns, change nothing, rewrite the prefix, and invalidate
                # prompt caching -- the expensive no-op that cost 12.1M tokens
                # on 2026-08-26. Serve the request untouched instead.
                log.warning(
                    f"[FERRY] {total_input:,} over trigger but curation is "
                    f"DISABLED (not converging; raise ROLLING_CONTEXT_TARGET "
                    f"above the floor and restart)")
                _floor = _convergence.get("floor_at_lockout")
                _note_gate("locked_out: not converging"
                           + (f" (floor ~{_floor})" if _floor else ""),
                           total_input, model=model, window=_window)
            elif total_input > 0 and total_input > TRIGGER_TOKENS and len(current_messages) >= 6:
                already_compressing = any(
                    e["thread"] is not None and e["thread"].is_alive()
                    for e in store.compressions
                )
                cooldown_left = FAILURE_COOLDOWN - (time.time() - _compression_failed_at)
                _now = time.monotonic()
                _gate_ok, _gate_why = _hysteresis_gate(total_input, _now, log)
                if already_compressing:
                    pass
                elif not _gate_ok:
                    # Not an error and not a defect — this is the system
                    # declining to do useless work. INFO, and say WHY, because
                    # "Ferry did nothing" was invisible at INFO once already
                    # (§20) and cost a whole run.
                    log.info(f"[MSG] {total_input:,} over trigger but HOLDING — {_gate_why}")
                    _note_gate(f"held: {_gate_why}", total_input,
                               model=model, window=_window)
                elif cooldown_left > 0:
                    log.info(
                        f"[MSG] Over trigger but last compression failed — "
                        f"cooling down another {cooldown_left:.0f}s"
                    )
                    _note_gate(f"cooldown: last compression failed, "
                               f"{cooldown_left:.0f}s left",
                               total_input, model=model, window=_window)
                else:
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                        f"Compressing in background..."
                    )
                    if _gate_why != "ok":
                        log.info(f"[MSG] {_gate_why}")
                    entry = store.add()
                    _convergence["awaiting"] = True
                    _hysteresis_note_cycle(total_input, _now)
                    t = threading.Thread(
                        target=_do_background_compression,
                        args=(entry, current_messages, auth_headers),
                        kwargs={"real_token_count": total_input, "payload": payload},
                        daemon=True,
                    )
                    t.start()
                    entry["thread"] = t

        except Exception as e:
            log.error(f"[MSG] Upstream error: {e}", exc_info=True)
            if _metrics:
                # Never fail silently: a gap in the request rows must have a
                # row explaining the gap.
                _metrics.row("error", model=model, window=_window,
                             note=f"upstream: {e}")
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    from compressor import (NATIVE_MODE, NATIVE_FALLBACK, SUMMARIZER_BASE_URL,
                            SUMMARIZER_FORMAT)
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL or '(session model)'}")
    log.info(f"  Summarizer mode: "
             f"{'native (cloned session request, prompt-cached)' if NATIVE_MODE else f'flattened/{SUMMARIZER_FORMAT}'}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")
    # Printed separately from the upstream on purpose: when these two disagree
    # unintentionally, compaction 401s forever and nothing ever compresses.
    log.info(f"  Compacting via: {SUMMARIZER_BASE_URL}"
             f"{' (third-party — flattened fallback armed)' if NATIVE_FALLBACK else ''}")
    log.info(f"  Matching: content-based (no sessions/fingerprints)")
    if _metrics:
        log.info(f"  Ferry metrics: {_metrics.path}")
        _metrics.row("proxy_start", window=_window, note=_params_note())
    elif _ferry_curation:
        # Ferry mode without a metrics file is a fact worth one loud line in
        # the banner, next to everything else this proxy will and won't do.
        log.warning(f"  Ferry metrics: OFF — {_metrics_off_reason()}")

    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
