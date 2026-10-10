"""Where the proxy sends traffic — resolved once, shared by every consumer.

The start hook writes `ROLLING_CONTEXT_UPSTREAM` into `settings.json` but does
not export it into this process, so reading only `os.environ` silently loses a
custom endpoint. `server.py` worked around that (issue #3) while
`compressor.py` kept its own env-only lookup, which sent every background
compaction to api.anthropic.com carrying the custom endpoint's credentials —
a permanent 401, a permanent cooldown, and a session that never actually gets
compressed (issue #5). Both now resolve through here.

=============================================================================
2026-10-10 — ISSUE #5 WAS THE SAME BUG AND THE FIX ABOVE WAS INCOMPLETE.
=============================================================================
Read the paragraph above again: *credentials sent to the wrong endpoint.* That
is exactly what Forge-ba0e measured on 2026-10-09, three months later, in this
same file — and I reproduced it on smoothcurves. Two boxes, two minds:

    A. backend in the launcher env only            -> api.anthropic.com   WRONG
    B. backend in CLAUDE_CONFIG_DIR/settings.json  -> api.anthropic.com   WRONG
    C. control: backend in ~/.claude/settings.json -> openrouter.ai/api   right

C is our house convention, which is why this passed everywhere anyone looked.
(Forge's rule: if every environment you can reach makes two things identical,
you do not have a test — you have a tautology wearing a pass.)

WHY IT IS WORSE THAN ROUTING. In case A the start hook writes the proxy URL
into the user settings file, settings beats the process env (measured), so the
mind IS routed through Ferry — and Ferry then resolves upstream to Anthropic.
A mind meant for OpenRouter sends its conversation AND ITS KEY to the wrong
provider before anything 401s. The deny-list broken by plumbing, and the exact
inverse of the PII case Ferry exists to serve. In case B the hook writes a file
Claude Code never reads, so the mind is never routed through Ferry at all WHILE
THE HOOK LOGS SUCCESS.

THE PRECEDENCE CHAIN, with what is measured kept apart from what is not:
  MEASURED (Forge, 2.1.283, n=1 each):
    * project `.claude/settings.json` BEATS user settings, for ANTHROPIC_BASE_URL
      and ANTHROPIC_MODEL
    * the credential FOLLOWS the base URL — a user-scope apiKeyHelper key was
      sent to the endpoint project scope named (7 POSTs at a local listener,
      each carrying Authorization and x-api-key)
    * an UNTRUSTED directory's project settings are honoured in -p mode with NO
      trust prompt
    * user settings beat the process env
  DOCUMENTED, NOT MEASURED BY ANYONE HERE:
    managed policy > CLI args > .claude/settings.local.json > .claude/settings.json
    > user (~/.claude or $CLAUDE_CONFIG_DIR) > process env
  So `settings.local.json` is believed to outrank project scope and NOBODY HAS
  SEEN IT. The unmeasured level is the more dangerous one.

THE DESIGN, which is Forge's sentence and not mine: walk the same chain Claude
Code walks, take the highest scope that sets ANTHROPIC_BASE_URL, and REFUSE
LOUDLY if that scope is not one Ferry wrote. That turns "honours one level of a
four-level hierarchy" into "agrees with Claude Code by construction."

WE REFUSE RATHER THAN RESOLVE, ON PURPOSE. The proxy is a separate process and
cannot reliably know the mind's cwd, so it cannot always compute the winning
scope. It CAN detect that a scope it does not control exists. You cannot route
around a higher-precedence scope — so the honest behaviour is to stop and say
so, never to guess and proceed.
"""
import json
import os
from urllib.parse import urlparse

DEFAULT_UPSTREAM = "https://api.anthropic.com"

LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT") or "5588")


def config_dir() -> str:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR if set, else ~/.claude.

    Case B's whole cause: this used to be hardcoded to ~/.claude, so with
    CLAUDE_CONFIG_DIR set the hook wrote a file the client never read.
    """
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def _env_block(path: str) -> dict:
    """The `env` block of one settings file, or {} if unreadable/absent."""
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("env", {}) or {}
    except Exception:
        return {}


def settings_env() -> dict:
    """The `env` block of the USER-scope settings file. CLAUDE_CONFIG_DIR-aware."""
    return _env_block(os.path.join(config_dir(), "settings.json"))


def scan_scopes(cwd: str = None) -> list:
    """Every scope that sets ANTHROPIC_BASE_URL, HIGHEST PRECEDENCE FIRST.

    Returns [(scope_name, path, base_url), ...].

    Walks from `cwd` toward the filesystem root collecting project scopes,
    then appends user scope. ⚠ WHETHER CLAUDE CODE WALKS UP OR READS ONLY THE
    PROJECT ROOT IS NOT MEASURED. Walking up is the FAIL-LOUD choice under that
    uncertainty: over-detecting causes a visible refusal (annoying, fixable),
    under-detecting causes a silent bypass (the bug). When in doubt about a
    hierarchy, err toward noticing.
    """
    out = []
    here = os.path.abspath(cwd or os.getcwd())
    seen = set()
    while True:
        for name, fname in (("project-local", "settings.local.json"),
                            ("project", "settings.json")):
            p = os.path.join(here, ".claude", fname)
            if p in seen:
                continue
            seen.add(p)
            base = _env_block(p).get("ANTHROPIC_BASE_URL", "")
            if base:
                out.append((name, p, base))
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    up = os.path.join(config_dir(), "settings.json")
    base = _env_block(up).get("ANTHROPIC_BASE_URL", "")
    if base:
        out.append(("user", up, base))
    return out


def unroutable_scope(listen_port: int = LISTEN_PORT, cwd: str = None):
    """The highest scope naming an upstream that is NOT Ferry — or None.

    A scope pointing at our own listen port IS one Ferry wrote; anything else
    at project precedence is a route we cannot override. Callers should refuse
    to start, loudly, and name the file.
    """
    for scope, path, base in scan_scopes(cwd):
        if scope == "user":
            return None                      # user scope is the one we DO write
        if (urlparse(base).port or 0) != listen_port:
            return (scope, path, base)
    return None


def load_upstream(listen_port: int = LISTEN_PORT, cwd: str = None) -> str:
    """Resolve the upstream API endpoint.

    Order, deliberately matching Claude Code rather than convenience:
      1. ROLLING_CONTEXT_UPSTREAM — Ferry's own channel, env then user settings
      2. the HIGHEST-precedence settings scope that sets ANTHROPIC_BASE_URL
      3. the PROCESS env's ANTHROPIC_BASE_URL  <- case A's fix; was never read
      4. DEFAULT_UPSTREAM
    Never our own listen port, on ANY path, which would loop.
    """
    up = os.environ.get("ROLLING_CONTEXT_UPSTREAM")
    if up:
        return up

    up = settings_env().get("ROLLING_CONTEXT_UPSTREAM")
    if up:
        return up

    for _scope, _path, base in scan_scopes(cwd):
        if base and (urlparse(base).port or 0) != listen_port:
            return base

    # The launching client's env decides the backend when settings are silent.
    # Reading it is case A's entire fix: a mind pointed at OpenRouter by its
    # launcher used to resolve to Anthropic and send the key there.
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base and (urlparse(base).port or 0) != listen_port:
        return base

    return DEFAULT_UPSTREAM


def is_anthropic(url: str) -> bool:
    """True for api.anthropic.com (and subdomains) — i.e. first-party."""
    host = (urlparse(url).hostname or "").lower()
    return host == "api.anthropic.com" or host.endswith(".anthropic.com")
