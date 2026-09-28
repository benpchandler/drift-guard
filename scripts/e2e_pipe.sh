#!/bin/sh
# Pipe the real captured hook payloads (tests/fixtures) through the installed hook against live Jev.
# Uses an isolated state dir and a fresh session id so the run never touches real sessions or the live measurement.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
HOOK="$ROOT/.venv/bin/drift"
export DRIFT_STATE_DIR=$(mktemp -d /tmp/drift-guard-pipe.XXXXXX)
SESSION="e2e-pipe-$(date +%s)"
UPS=$(jq -c --arg s "$SESSION" '.session_id=$s | .transcript_path="/nonexistent.jsonl"' "$ROOT/tests/fixtures/user_prompt_submit.json")
STOP=$(jq -c --arg s "$SESSION" '.session_id=$s | .transcript_path="/nonexistent.jsonl"' "$ROOT/tests/fixtures/stop.json")
prompt() {
  echo "> $1"
  start=$(date +%s%N)
  echo "$UPS" | jq -c --arg p "$1" '.prompt=$p' | "$HOOK" hook user-prompt
  echo "  [exit=$? wall_ms=$(( ($(date +%s%N) - start) / 1000000 ))]"
}
prompt "Fix the flaky login test in the budget repo pytest suite"
prompt "The failure is in test_login_redirect; check the session fixture"
echo "> (Stop) agent: Found it: the session fixture leaks state. Want me to reset it per test?"
echo "$STOP" | jq -c '.last_assistant_message="Found it: the session fixture leaks state. Want me to reset it per test?"' | "$HOOK" hook stop
prompt "yes do it"
prompt "Also, what is a good banana bread recipe?"
prompt "drift: feedback wrong, this is the same task"
prompt "drift: ack - quick detour"
prompt "drift: new intent plan the Q4 roadmap doc"
echo "> fail-open with a bad API key"
echo "$UPS" | jq -c '.prompt="banana bread again"' | TYPESAFE_API_KEY=bad "$HOOK" hook user-prompt
echo "  [exit=$? no output expected]"
echo "-- labels"
jq -c '{judgment_action, verdict, outcome, note, excerpt}' "$DRIFT_STATE_DIR/labels.jsonl"
echo "-- guard log"
jq -c '{event, action, turn_drift, drift_score, latency_ms, error: .error[0:60]}' "$DRIFT_STATE_DIR/judgments.jsonl"
