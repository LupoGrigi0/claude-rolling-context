"""Ferry curation producer — curation, not summarization.

Replaces the LLM text-production path of the rolling compressor
(activated by ROLLING_CONTEXT_CURATION=ferry). Instead of asking a model
to summarize evicted turns, it:

  1. ARCHIVES the evicted turns verbatim (append-only jsonl; media blobs
     extracted to a content-addressed blob store — never inline, §15.1a
     of the Ferry design).
  2. Emits a deterministic POINTER INDEX: one slug per evicted turn,
     carrying a verbatim gist (a contiguous prefix span — legal selection
     under the Ferry invariant) plus the exact archive pointer.
  3. Renders the mind's CARRY (append-only block log, ferry carrystore)
     at the head — identity verbatim, never summarized.

No LLM call. Pure, offline-testable, and every retained byte is
reconstructable from the archive (the constitution's invariant).

Ferry design + constitution: github.com/LupoGrigi0/ferry DESIGN-REV2.md
Crossing-2d23 <crossing-2d23@smoothcurves.nexus>, 2026-08-11. Stdlib only.
"""

import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("rolling-context")

# Metrics are an optional observer of curation, never a dependency of it:
# if the module is missing, crossings still happen, just unmeasured.
try:
    from metrics import ArchiveTotals, get_writer as _metrics_writer
except ImportError:  # pragma: no cover — defensive
    ArchiveTotals = None

    def _metrics_writer(*_a, **_kw):
        return None

GIST_CHARS = 80
CARRY_HEADER = "=== CARRY (verbatim, append-only — never summarized) ==="
INDEX_HEADER = "=== ARCHIVED TURNS (verbatim at pointers; fetch to recall in full) ==="
SLUG_PREFIX = "- ["


def _ferry_core_on_path():
    """Make ferry core (carrystore) importable when configured."""
    core = os.environ.get("FERRY_CORE", "")
    if core and core not in sys.path:
        sys.path.insert(0, core)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FerryCurationProducer:
    """Turns-in -> curated-payload-text-out. Deterministic."""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get(
            "FERRY_DATA", str(Path.home() / "ferry-data")))
        self.archive_dir = self.data_dir / "archive"
        self.blob_dir = self.archive_dir / "blobs"
        self.carry_path = self.data_dir / "carry.jsonl"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        # Incremental archive line/byte counter (only reads what was
        # appended since the last cycle). Built lazily — off-metrics runs
        # never touch it.
        self._archive_totals = None

    # ── blob store (§15.1a: blobs are NEVER inline) ────────────────────
    def _store_blob(self, b64_data):
        """base64 (API transit format) -> raw binary content-addressed
        file. Returns the sha256 hex that IS the filename."""
        raw = base64.b64decode(b64_data)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.blob_dir / f"sha256-{digest}"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.rename(path)
        return digest

    def _externalize_media(self, content):
        """Return content with any inline media replaced by blob refs."""
        if not isinstance(content, list):
            return content
        out = []
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "image"
                    and isinstance(block.get("source"), dict)
                    and block["source"].get("type") == "base64"):
                digest = self._store_blob(block["source"].get("data", ""))
                out.append({
                    "type": "ferry_blob",
                    "media_type": block["source"].get("media_type", ""),
                    "sha256": digest,
                })
            else:
                out.append(block)
        return out

    # ── archive (verbatim, append-only) ────────────────────────────────
    def _archive_turns(self, turns):
        """Append evicted turns verbatim (media externalized). Returns
        (filename, first_line, last_line, batch_chars) — lines 1-based,
        inclusive; batch_chars is what we actually wrote for this batch
        (the metrics collector's chars/4 token estimate, measured rather
        than re-derived: media is already externalized here, so a 4 MB
        screenshot doesn't masquerade as a million evicted tokens)."""
        fname = f"archive_{time.strftime('%Y%m%d', time.gmtime())}.jsonl"
        path = self.archive_dir / fname
        # first line number of this batch = existing line count + 1
        first = 1
        if path.exists():
            with open(path, encoding="utf-8") as f:
                first = sum(1 for _ in f) + 1
        batch_chars = 0
        with open(path, "a", encoding="utf-8") as f:
            for t in turns:
                record = {
                    "role": t.get("role", "unknown"),
                    "content": self._externalize_media(t.get("content", "")),
                    "archived_ts": _now(),
                }
                line = json.dumps(record, ensure_ascii=False) + "\n"
                batch_chars += len(line)
                f.write(line)
        return fname, first, first + len(turns) - 1, batch_chars

    # ── slugs (deterministic scaffolding + verbatim gist) ──────────────
    @staticmethod
    def _gist(content):
        """First GIST_CHARS of the first text in the turn — a contiguous
        verbatim prefix (legal selection), never a paraphrase."""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = ""
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and block.get("text"):
                        text = block["text"]
                        break
                    if block.get("type") == "tool_use":
                        text = f"[tool_use: {block.get('name', '?')}]"
                        break
                    if block.get("type") == "tool_result":
                        c = block.get("content", "")
                        if isinstance(c, list):
                            c = next((b.get("text", "") for b in c
                                      if isinstance(b, dict)
                                      and b.get("type") == "text"), "")
                        text = f"[tool_result] {c if isinstance(c, str) else ''}"
                        break
                    if block.get("type") in ("image", "ferry_blob"):
                        text = "[image]"
                        break
        else:
            text = ""
        # Skip harness boilerplate: Claude Code prepends <system-reminder>
        # blocks to user turns, so a naive first-80-chars gist captures
        # boilerplate instead of the human's words. (Found live: the
        # planted beacon code sat past a system-reminder prefix — first
        # ferry crossing, 2026-08-11.)
        while text.lstrip().startswith("<system-reminder>"):
            end = text.find("</system-reminder>")
            if end == -1:
                break
            text = text[end + len("</system-reminder>"):]
        gist = " ".join(text.split())  # collapse newlines for a one-line slug
        return gist[:GIST_CHARS]

    def _slugs(self, turns, fname, first_line):
        lines = []
        for i, t in enumerate(turns):
            gist = self._gist(t.get("content", ""))
            lines.append(
                f"{SLUG_PREFIX}{t.get('role', '?')}] {gist} "
                f"→ {fname}#L{first_line + i}")
        return lines

    # ── carry render (identity verbatim, from the block log) ───────────
    def _render_carry(self):
        if not self.carry_path.exists():
            return ""
        _ferry_core_on_path()
        try:
            from carrystore import CarryStore
        except ImportError:
            log.warning("FERRY_CORE not importable; carry not rendered")
            return ""
        try:
            blocks = CarryStore(self.carry_path).materialize()
        except Exception as e:
            log.warning(f"carry render failed ({e}); continuing without")
            return ""
        parts = []
        for b in blocks:
            if b.get("kind") == "scaffolding":
                parts.append(b.get("payload", ""))
            else:
                parts.append(b.get("payload", ""))
        return "\n\n".join(p for p in parts if p)

    # ── the producer ───────────────────────────────────────────────────
    def produce(self, messages, start_idx, keep_from_idx, existing_payload="",
                real_tokens=None):
        """Evict messages[start_idx:keep_from_idx]; return the curated
        payload TEXT (caller wraps it in the marker envelope). Prior
        pointer-index lines from existing_payload are carried forward
        append-only; the carry section is re-rendered fresh (the carry
        log is its own source of truth).

        real_tokens is the REAL upstream input-token count that tripped the
        curation trigger, for the metrics row only — it says WHERE on the
        token curve this cliff happened, which the producer cannot know from
        the messages alone. Optional on purpose: the producer stays a pure,
        independently testable messages-in/text-out function, and a caller
        that does not know the number passes nothing, which records blank
        (never 0, and never an estimate dressed up as a measurement)."""
        evicted = messages[start_idx:keep_from_idx]
        if not evicted:
            return existing_payload or ""

        fname, first, last, batch_chars = self._archive_turns(evicted)
        new_slugs = self._slugs(evicted, fname, first)

        prior_slugs = [ln for ln in (existing_payload or "").splitlines()
                       if ln.startswith(SLUG_PREFIX)]

        sections = []
        carry = self._render_carry()
        if carry:
            sections.append(f"{CARRY_HEADER}\n{carry}")
        sections.append(INDEX_HEADER + "\n"
                        + "\n".join(prior_slugs + new_slugs))
        payload = "\n\n".join(sections)
        log.info(
            f"Ferry curation: archived {len(evicted)} turns -> {fname}"
            f"#L{first}-L{last}; index {len(prior_slugs)}+{len(new_slugs)} "
            f"slugs; payload {len(payload):,} chars (no LLM call)")
        self._record_metrics(evicted, batch_chars, carry, fname, first, last,
                             real_tokens=real_tokens)
        return payload

    # ── metrics (write-down only; measures nothing new) ────────────────
    def _record_metrics(self, evicted, batch_chars, carry, fname, first, last,
                        real_tokens=None):
        """One curation row + one archive_write row per cycle. Wrapped whole:
        a metrics problem must never cost a crossing."""
        try:
            writer = _metrics_writer(self.data_dir)
            if writer is None:
                return
            cycle = writer.next_cycle()
            # context_tokens on a curation row = the real token count the
            # request was carrying when the trigger fired, so the cliff lands
            # on the same curve as the request rows. Unknown stays None ->
            # blank; 0 would claim we curated an empty context. It never
            # touches the request delta chain (that is request-only).
            try:
                at_tokens = int(real_tokens) if real_tokens else None
            except (TypeError, ValueError):
                at_tokens = None
            if at_tokens is not None and at_tokens <= 0:
                at_tokens = None
            writer.row("curation", cycle=cycle,
                       context_tokens=at_tokens,
                       tokens_evicted=batch_chars // 4,
                       carry_chars=len(carry or ""),
                       note=f"estimate:chars/4, turns={len(evicted)}")
            if self._archive_totals is None and ArchiveTotals is not None:
                self._archive_totals = ArchiveTotals(self.archive_dir)
            lines, size = (self._archive_totals.totals()
                           if self._archive_totals else (None, None))
            writer.row("archive_write", cycle=cycle, archive_lines=lines,
                       archive_bytes=size, note=f"{fname}#L{first}-L{last}")
        except Exception as e:
            log.warning(f"[FERRY] metrics: curation rows not written ({e})")


# Framing text used by the compressor's envelope in curation mode —
# honest about what the payload IS (pointers + verbatim, not a summary).
CURATION_FRAMING_USER = (
    "The above is your carried context: verbatim identity blocks and a "
    "pointer index to archived turns (stored verbatim, fetchable). "
    "Nothing was summarized; earlier turns were moved, not lost. "
    "To recall any archived turn in full, run: "
    "ferry-fetch '<pointer>' (e.g. ferry-fetch 'archive_20260811.jsonl#L3') "
    "via your Bash tool. Continue from where we left off."
)
CURATION_FRAMING_ACK = (
    "I have my carried context — identity verbatim, and the pointer index "
    "to the archived turns, which I can recall in full with ferry-fetch "
    "when needed. Continuing."
)
