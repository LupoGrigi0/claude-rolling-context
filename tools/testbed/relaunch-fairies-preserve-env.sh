#!/usr/bin/env bash
# relaunch-fairies-preserve-env.sh — land and relaunch the fairies WITHOUT
# touching their .launch-env. RUN AS ROOT.
#
# WHY THIS EXISTS. Both older launchers -- launch-fairies.sh and
# relaunch-fairies.sh -- write .launch-env from a heredoc baked into the
# script. That was correct when the script was the only thing that had ever
# set those files. It is now actively dangerous: the fairies were migrated to
#
#     ANTHROPIC_MODEL=claude-haiku-4-5-20251001   + per-fairy Max OAuth
#
# on 2026-08-26, and both scripts still carry
#
#     MODEL="nvidia/nemotron-3-super-120b-a12b:free"  + a dummy token
#
# so running either one SILENTLY REVERTS the migration. The run would come
# back green, the proxies would come up, the minds would talk -- on the wrong
# substrate, with the wrong context window, and every conclusion about
# hysteresis drawn from it would be about a model we were not testing.
#
# That is the house failure mode exactly: not a crash, a plausible wrong
# answer. So this script does not write .launch-env at all. It reads it,
# reports what it found, and REFUSES when something is missing rather than
# helpfully supplying a default.
#
#   The launcher must never be the thing that decides what is being tested.
#
# Crossing-2d23, 2026-08-28.
set -uo pipefail

# --preflight-only runs every check and launches NOTHING, so the checks can be
# exercised by an unprivileged account before a root session is spent on them.
# A guard nobody has ever seen fire is a guard nobody knows works.
PREFLIGHT_ONLY=0
[ "${1:-}" = "--preflight-only" ] && PREFLIGHT_ONLY=1

if [ "$PREFLIGHT_ONLY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (needs sudo -u per fairy)" >&2
  echo "hint: $0 --preflight-only  runs the checks without root" >&2
  exit 1
fi

TB=/mnt/lupoportfolio/ferry-testbed
CHASSIS=/mnt/coordinaton_mcp_data/Human-Adjacent-Coordination/src/chassis/claude-code-channel

FLEET="passenger 5610 21010
ferry 5611 21011
fairie 5612 21012"

# ---- PREFLIGHT ------------------------------------------------------------
# Check EVERYTHING before launching ANYTHING. A half-launched fleet is worse
# than none: two minds on the new build and one on the old is a confound that
# does not announce itself, and we would be comparing them all evening.
fail=0
echo "=== preflight ==="
while read -r name proxy chan; do
  [ -z "$name" ] && continue
  env="$TB/$name/.launch-env"

  if [ ! -f "$env" ]; then
    echo "  $name: MISSING $env — refusing. This script does not write it;"
    echo "         see the header for why. Restore it before launching."
    fail=1; continue
  fi

  # Report what is actually in there. Never assume, and never print a token.
  model=$(grep -E '^ANTHROPIC_MODEL=' "$env" | head -1 | cut -d= -f2-)
  base=$(grep -E '^ANTHROPIC_BASE_URL=' "$env" | head -1 | cut -d= -f2-)
  hasauth=$(grep -cE '^ANTHROPIC_(API_KEY|AUTH_TOKEN)=' "$env")

  if ! ss -ltn 2>/dev/null | grep -q "127.0.0.1:$proxy "; then
    echo "  $name: proxy $proxy NOT listening — refusing to launch a mind"
    echo "         with nowhere to talk to. Start the proxies first."
    fail=1; continue
  fi

  # The base URL must point at this fairy's OWN proxy. A copy-paste that
  # points two fairies at one proxy interleaves two conversations into one
  # curation stream, and the metrics would look merely noisy.
  if [ "$base" != "http://127.0.0.1:$proxy" ]; then
    echo "  $name: base URL is '$base', expected 'http://127.0.0.1:$proxy' — refusing"
    fail=1; continue
  fi

  cred="own OAuth (no key in env)"
  [ "$hasauth" -gt 0 ] && cred="env-supplied token present"
  echo "  $name: model=$model  proxy=$proxy  chan=$chan  auth=$cred  OK"
done <<< "$FLEET"

[ "$fail" -ne 0 ] && { echo; echo "PREFLIGHT FAILED — nothing launched."; exit 1; }

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  echo
  echo "PREFLIGHT ONLY — nothing launched. Re-run as root without the flag."
  exit 0
fi

echo
echo "All three preflight clean. Models above are what will actually run."
echo "If any of them is not what you intended, stop now — this script will"
echo "not correct it for you, by design."
echo

# ---- LAND, THEN LAUNCH ----------------------------------------------------
while read -r name proxy chan; do
  [ -z "$name" ] && continue
  echo "=== $name ==="
  INSTANCEROOT="$TB" "$CHASSIS/land-claude-code-channel.sh" \
    --instance-id "$name" >/dev/null 2>&1 || true

  INSTANCEROOT="$TB" "$CHASSIS/launch-claude-code-channel.sh" \
    --instance-id "$name" --port "$chan" --canary-timeout 120 \
    2>&1 | python3 -c "import json,sys
d=json.loads(sys.stdin.read() or '{}')
print('  status=%s ready=%s hearing=%s' % (d.get('status'), d.get('channelReady'), d.get('channelHearing')))" 2>/dev/null \
    || echo "  (launcher output unparseable — read the pane below)"

  # hearing=false is NOT proof of deafness. The launcher used to collapse every
  # non-zero rc into DEAF, and the remedy for DEAF is land-and-relaunch — the
  # very operation that caused the original fault. A false DEAF recruits a
  # human to break a working mind. Read the pane.
  echo "  --- what the pane is showing ---"
  sed -e 's/\x1b\[[0-9;<>?]*[a-zA-Z]//g' -e 's/\r//g' \
      "/var/log/hacs/$name-channel-pane.log" 2>/dev/null \
    | grep -viE '^\s*$|^[─━]+$' | tail -6 | sed 's/^/    /'
  echo
done <<< "$FLEET"

echo "Launched. Verify each is HEARING before trusting any measurement:"
echo "  for p in 21010 21011 21012; do curl -s localhost:\$p/health; echo; done"
