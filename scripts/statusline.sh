#!/bin/bash
# Claude Code status line segment for Drift. Reads the session's drift score
# straight from its state file (no process spawn) and prints it colored by the
# guard's own thresholds: green below warn, yellow in the warn zone, red in the
# block zone. The agent's drift is shown only once it reaches the warn zone.
#
# Use as the whole status line, or call it from an existing one with the same
# JSON payload on stdin. Requires jq.

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')

reset="\033[0m"
green="\033[1;32m"
yellow="\033[1;33m"
red="\033[1;31m"
dim="\033[2m"

guard_file="${DRIFT_STATE_DIR:-$HOME/.local/state/drift}/sessions/${session_id}.json"
if [ -z "$session_id" ] || [ "${DRIFT_MODE:-warn}" = "off" ] || [ ! -f "$guard_file" ]; then
    exit 0
fi

read -r human_pct agent_pct < <(jq -r '"\((.human.score // 0) * 100 | floor) \((.agent.score // 0) * 100 | floor)"' "$guard_file" 2>/dev/null)
[ -z "$human_pct" ] && exit 0

warn_pct=$(awk "BEGIN { print int(${DRIFT_WARN:-0.5} * 100) }")
block_pct=$(awk "BEGIN { print int(${DRIFT_BLOCK:-0.8} * 100) }")

guard_color() {
    if [ "$1" -ge "$block_pct" ]; then printf '%s' "$red"
    elif [ "$1" -ge "$warn_pct" ]; then printf '%s' "$yellow"
    else printf '%s' "$green"; fi
}

section="drift $(guard_color "$human_pct")${human_pct}%${reset}"
if [ -n "$agent_pct" ] && [ "$agent_pct" -ge "$warn_pct" ]; then
    section="${section} ${dim}agent${reset} $(guard_color "$agent_pct")${agent_pct}%${reset}"
fi
printf "%b\n" "$section"
