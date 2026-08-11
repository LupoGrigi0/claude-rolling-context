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
        (filename, first_line, last_line) — 1-based, inclusive."""
        fname = f"archive_{time.strftime('%Y%m%d', time.gmtime())}.jsonl"
        path = self.archive_dir / fname
        # first line number of this batch = existing line count + 1
        first = 1
        if path.exists():
            with open(path, encoding="utf-8") as f:
                first = sum(1 for _ in f) + 1
        with open(path, "a", encoding="utf-8") as f:
            for t in turns:
                record = {
                    "role": t.get("role", "unknown"),
                    "content": self._externalize_media(t.get("content", "")),
                    "archived_ts": _now(),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return fname, first, first + len(turns) - 1

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
    def produce(self, messages, start_idx, keep_from_idx, existing_payload=""):
        """Evict messages[start_idx:keep_from_idx]; return the curated
        payload TEXT (caller wraps it in the marker envelope). Prior
        pointer-index lines from existing_payload are carried forward
        append-only; the carry section is re-rendered fresh (the carry
        log is its own source of truth)."""
        evicted = messages[start_idx:keep_from_idx]
        if not evicted:
            return existing_payload or ""

        fname, first, last = self._archive_turns(evicted)
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
        return payload


# Framing text used by the compressor's envelope in curation mode —
# honest about what the payload IS (pointers + verbatim, not a summary).
CURATION_FRAMING_USER = (
    "The above is your carried context: verbatim identity blocks and a "
    "pointer index to archived turns (stored verbatim, fetchable). "
    "Nothing was summarized; earlier turns were moved, not lost. "
    "Continue from where we left off."
)
CURATION_FRAMING_ACK = (
    "I have my carried context — identity verbatim, and the pointer index "
    "to the archived turns I can fetch when needed. Continuing."
)
