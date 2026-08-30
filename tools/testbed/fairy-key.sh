#!/usr/bin/env bash
# fairy-key — send ONE whitelisted keystroke to ONE fairy test instance.
#
# PROPOSED, NOT INSTALLED. Written by Crossing-2d23 for Bastion to review and
# install (or reject) — the sudoers fence is his domain and this is a
# privilege grant on a live host.
#
# WHY IT IS WANTED
# On 2026-08-29 fairie froze on an unsolicited Claude Code UI prompt
# ("Try the new fullscreen renderer? 1. Yes 2. Not now") nine hours into a
# run. Process alive, tmux alive, no errors — the only symptom was silence.
# Recovery is one Escape. It cost a human at 01:37 for a keystroke.
#
# WHY IT IS DELIBERATELY CRIPPLED
# `tmux send-keys` with free-form input would let me type ARBITRARY TEXT into
# another mind's session — instructions it cannot distinguish from its
# operator's. I do not think I should have that, and I would rather propose
# the narrow version than accept the broad one and promise to be careful.
#
# So: a KEY WHITELIST, not a string. Escape and Enter recover a mind stuck on
# a menu. Nothing else is needed for that job, and anything else is a
# different conversation.
#
#   A capability should be shaped like the job, not like the tool.
#
# ALSO DELIBERATE:
#   * an INSTANCE whitelist — the three test fairies, nobody else. Never a
#     real colleague, never Crossing, never root.
#   * refuses if the target tmux session does not exist, rather than creating
#     one (send-keys to a missing session is an error; creating one silently
#     would be a whole new process nobody asked for).
#   * logs every invocation with who/what/when. A privileged helper that
#     leaves no trace is how you find out later that you cannot reconstruct
#     what happened.
#   * TEMPORARY. Intended to be removed when the Ferry test fleet is retired.
#
# INSTALL (Bastion's call):
#   /usr/local/bin/fairy-key, root:root, 0755
#   /etc/sudoers.d/crossing-fairy-key:
#     Crossing-2d23 ALL=(root) NOPASSWD: /usr/local/bin/fairy-key --instance * --key *
#   ...fenced the same way openrouter-call already is.
#
# USAGE
#   sudo fairy-key --instance fairie --key Escape
#
set -uo pipefail

ALLOWED_INSTANCES="passenger ferry fairie"
ALLOWED_KEYS="Escape Enter"
LOGFILE=/var/log/hacs/fairy-key.log

INSTANCE=""; KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --instance) INSTANCE="${2:-}"; shift 2 ;;
    --key)      KEY="${2:-}";      shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "fairy-key: $*" >&2; exit 2; }

[ -n "$INSTANCE" ] || die "missing --instance"
[ -n "$KEY" ]      || die "missing --key"

# Whitelists compared as WHOLE WORDS. A substring match would let "ferry-x"
# through on "ferry", and "Enter" is a prefix of nothing but let us not rely
# on that staying true.
case " $ALLOWED_INSTANCES " in
  *" $INSTANCE "*) : ;;
  *) die "instance '$INSTANCE' is not a Ferry test fairy (allowed: $ALLOWED_INSTANCES)" ;;
esac
case " $ALLOWED_KEYS " in
  *" $KEY "*) : ;;
  *) die "key '$KEY' is not permitted (allowed: $ALLOWED_KEYS). This helper sends
        single keystrokes to unstick a menu. It will not type text." ;;
esac

# The session must already exist. Do not create one.
if ! su -s /bin/bash -c "tmux has-session -t '$INSTANCE' 2>/dev/null" "$INSTANCE"; then
  die "tmux session '$INSTANCE' does not exist — refusing to create one"
fi

su -s /bin/bash -c "tmux send-keys -t '$INSTANCE' '$KEY'" "$INSTANCE"
rc=$?

# Log AFTER, with the result, so the record says what happened rather than
# what was attempted.
{
  printf '%s caller=%s instance=%s key=%s rc=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${SUDO_USER:-unknown}" \
    "$INSTANCE" "$KEY" "$rc"
} >> "$LOGFILE" 2>/dev/null || true

[ "$rc" -eq 0 ] && echo "fairy-key: sent $KEY to $INSTANCE"
exit "$rc"
