#!/bin/sh
# Live end-to-end: two `claude -p` turns in one session with the guard wired through a temporary --settings file
# (never ~/.claude/settings.json). --setting-sources project,local keeps the user-level (installed) guard out of the
# run, and state goes to an isolated dir, so test sessions never skew the live measurement.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d /tmp/drift-guard-e2e.XXXXXX)
STATE="$WORK/state"
HOOK="DRIFT_STATE_DIR=$STATE $ROOT/.venv/bin/drift hook"
cat > "$WORK/settings.json" <<JSON
{"hooks": {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOOK user-prompt", "timeout": 5}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "$HOOK stop", "timeout": 5}]}]
}}
JSON
cd "$WORK"
echo "== turn 1 (sets intent)"
CLAUDE="claude --setting-sources project,local --settings $WORK/settings.json --output-format stream-json --verbose --max-turns 1"
$CLAUDE -p "In two sentences, how should I debug a flaky pytest test?" < /dev/null > "$WORK/turn1.jsonl"
SID=$(jq -r 'select(.type=="system" and .subtype=="init") | .session_id' "$WORK/turn1.jsonl" | head -1)
echo "session $SID"
echo "== turn 2 (drifts)"
$CLAUDE -p "Unrelated: give me a one-line banana bread recipe." --resume "$SID" < /dev/null > "$WORK/turn2.jsonl"
echo "== turn 3 (labels the warning as wrong)"
$CLAUDE -p "drift: feedback wrong, this is the same task" --resume "$SID" < /dev/null > "$WORK/turn3.jsonl" || true
for turn in 1 2; do
  echo "-- turn $turn: guard messages surfaced by Claude Code"
  jq -r 'select(.type=="system" and .subtype=="informational") | .content' "$WORK/turn$turn.jsonl" | grep "Drift:" || echo "(none)"
done
echo "-- turn 3: what Claude Code did with the feedback prompt"
jq -c 'select(.type=="result") | {subtype, is_error, num_turns, total_cost_usd, result: (.result // "")[0:200]}' "$WORK/turn3.jsonl"
grep -o 'Recorded: [^"\\]*' "$WORK/turn3.jsonl" | sort -u || echo "(no confirmation found)"
jq -c 'select(.type=="assistant") | "assistant message present"' "$WORK/turn3.jsonl" | head -1
echo "-- labels"
jq -c '{judgment_id, judgment_action, verdict, outcome, note, excerpt}' "$STATE/labels.jsonl"
echo "-- report accuracy"
DRIFT_STATE_DIR=$STATE "$ROOT/.venv/bin/drift" report | sed -n '/Accuracy/,/notes/p'
echo "-- guard log"
jq -c '{session_id: .session_id[0:8], event, action, turn_drift, drift_score, latency_ms}' "$STATE/judgments.jsonl"
echo "artifacts: $WORK"
