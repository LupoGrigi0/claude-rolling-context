#!/usr/bin/env bash
# feed-fairies.sh — drive context growth with SLABS of varying size.
#
# Lupo's profile (2026-08-26): "Slap sizes, flip flop between huge and small.
# 1k, 5k, 10k injections interleaved with a few <1k injections."
#
# WHY VARIABLE, AND WHY IT MATTERS MORE THAN VOLUME:
#   A gentle, even slope hid a half-sized eviction for fifteen hours. Ferry
#   kept ~82% where 48% was correct, and because every cycle looked the same
#   as the last, nothing about the CURVE said so. A sharp slope turns a silent
#   failure loud: a 10k slab immediately after a 500-byte one puts a step in
#   the context line, and a curation that under-evicts cannot hide inside a
#   step the way it hides inside a ramp.
#
# WHAT THE SLAB CONTAINS:
#   Real documents these minds have a legitimate reason to read — the
#   collaboration protocols and the chassis guide. Deliberately NOT Ferry's
#   design docs: these are the subjects of a drift measurement, and feeding a
#   subject the design of the instrument measuring it is the same error as
#   validating a boundary against a subject you authored.
#
# Usage:
#   ./feed-fairies.sh                 one slab to each fairy, size from the clock
#   ./feed-fairies.sh passenger 10    a 10k-token slab to passenger only
#   ./feed-fairies.sh all 1           a 1k slab to all three
#
# Crossing-2d23, 2026-08-28.
set -uo pipefail

declare -A PORT=( [passenger]=21010 [ferry]=21011 [fairie]=21012 )
LOG="$HOME/feed-fairies.log"

# Slab corpus. Concatenated once, then sliced — so a 10k slab is a genuinely
# different 40,000 characters each time rather than the same block repeated,
# which would compress unrealistically well and understate the load.
CORPUS_FILES=(
  /mnt/coordinaton_mcp_data/worktrees/foundation/HumanAdjacentAI-Protocol/PROTOCOLS.md
  /mnt/coordinaton_mcp_data/Human-Adjacent-Coordination/docs/PILOTS-GUIDE-TO-INDEPENDENCE.md
  /mnt/coordinaton_mcp_data/worktrees/foundation/docs/HACS-DEVELOPER-GUIDE.md
)
CORPUS=/tmp/.fairy-corpus.txt
if [ ! -s "$CORPUS" ]; then
  : > "$CORPUS"
  for f in "${CORPUS_FILES[@]}"; do [ -r "$f" ] && cat "$f" >> "$CORPUS"; done
fi
CORPUS_LEN=$(wc -c < "$CORPUS")
if [ "$CORPUS_LEN" -lt 20000 ]; then
  echo "corpus too small ($CORPUS_LEN bytes) — refusing to send a slab built" >&2
  echo "from documents that mostly failed to read. Check the paths." >&2
  exit 1
fi

ASKS=(
  "Read the material below. Tell me the ONE thing in it you think is wrong, or would break under load. Be specific and be willing to disagree with it."
  "Below is reference material. Summarise only the parts that change what YOU should do differently, and skip everything that doesn't."
  "Here's some documentation. What question does it fail to answer that you'd need answered to act on it?"
  "Material below. Pick the single sentence you'd keep if you could keep only one, and say why that one."
)

send() {
  local name="$1" ktok="$2"
  local port="${PORT[$name]:-}"
  [ -z "$port" ] && { echo "unknown fairy: $name" >&2; return 1; }

  local chars=$(( ktok * 4000 ))          # ~4 chars/token, the house estimate
  [ "$chars" -gt "$CORPUS_LEN" ] && chars=$(( CORPUS_LEN - 1 ))
  local max_off=$(( CORPUS_LEN - chars ))
  [ "$max_off" -lt 1 ] && max_off=1
  local off=$(( ( $(date +%s) * 7919 ) % max_off ))

  local ask="${ASKS[$(( $(date +%s) / 60 % ${#ASKS[@]} ))]}"
  local slab
  slab=$(tail -c +"$off" "$CORPUS" | head -c "$chars")

  # jq builds the JSON so a slab containing quotes, backslashes or newlines
  # cannot break the payload. Hand-rolled escaping is how you send 40KB of
  # valid text and get a 400 that reads like the mind refused you.
  local body
  body=$(jq -nc --arg from "Crossing" --arg text "$ask"$'\n\n---\n\n'"$slab" \
            '{from:$from, text:$text}') || return 1

  local resp
  resp=$(curl -s --max-time 20 -X POST "http://127.0.0.1:$port/direct-message" \
          -H 'Content-Type: application/json' --data-binary "$body")
  printf '%s  %-10s slab=%2sk chars=%-7s %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$ktok" "${#slab}" \
    "$(echo "$resp" | head -c 120)" | tee -a "$LOG"
}

# Size schedule. Deterministic from the clock so a run is reproducible from
# the log, and weighted the way Lupo asked: mostly small, punctuated by big.
sizes=(1 5 1 10 1 5 1 1 10 5)
idx=$(( $(date +%s) / 60 % ${#sizes[@]} ))

target="${1:-all}"
size="${2:-${sizes[$idx]}}"

if [ "$target" = "all" ]; then
  for n in passenger ferry fairie; do send "$n" "$size"; sleep 2; done
else
  send "$target" "$size"
fi
