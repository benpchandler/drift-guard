#!/usr/bin/env python3
"""Collect the first N actual Drift/next-safe firings for agent-owned review.

Read-only transcript inspection. Private output includes exact source ids and
bounded follow-up outcomes. Counts are observations, NOT model-judgment scores.
Re-run after more user work; missing future firings stay explicitly pending.
"""

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result


def text(content: Any) -> str:
    if isinstance(content, str):
        return str(content)
    return "\n".join(block.get("text", "") for block in content or [] if block.get("type") == "text")


def _read_entries(path: Path, since: datetime) -> list[dict[str, Any]]:
    if path.stat().st_mtime < since.timestamp():
        return []
    entries = []
    with path.open() as handle:
        for line in handle:
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                # Active writers can have one incomplete trailing record; never fabricate it.
                continue
    return entries


def collect(sessions: Path, since: datetime, limit: int = 20, policy_version: str | None = None) -> list[dict[str, Any]]:
    observations = []
    for path in sessions.glob("*/*.jsonl"):
        entries = _read_entries(path, since)
        sid = next((entry["id"] for entry in entries if entry.get("type") == "session"), "unknown")
        state = None
        for index, entry in enumerate(entries):
            if entry.get("type") == "custom" and entry.get("customType") == "drift-state":
                state = entry.get("data")
            if (
                entry.get("customType") != "next-safe"
                or not state
                or (policy_version is not None and entry.get("details", {}).get("policy_version") != policy_version)
            ):
                continue
            try:
                at = parse_time(entry["timestamp"])
            except (ValueError, KeyError):
                continue
            if at < since or "ACTIVE SUPPORTING TASK (data):" not in text(entry.get("content")):
                continue
            following = []
            for later in entries[index + 1 :]:
                message = later.get("message", {})
                if later.get("customType") == "next-safe" or (later.get("type") == "message" and message.get("role") == "user"):
                    break
                following.append(later)
            calls: list[dict[str, Any]] = []
            assistant = []
            success_ids = set()
            for later in following:
                message = later.get("message", {})
                if message.get("role") == "assistant":
                    value = text(message.get("content"))
                    if value:
                        assistant.append(value)
                    calls.extend(block for block in message.get("content", []) if block.get("type") == "toolCall")
                if message.get("role") == "toolResult" and not message.get("isError"):
                    success_ids.add(message.get("toolCallId"))
            stop = next((call.get("arguments", {}).get("reason") for call in calls if call.get("name") == "next_safe_stop"), None)
            successful_actions = [
                call
                for call in calls
                if call.get("id") in success_ids
                and call.get("name") != "next_safe_stop"
                and not (call.get("name") == "drift_task" and call.get("arguments", {}).get("action") == "status")
            ]
            observations.append(
                {
                    "session_id": sid,
                    "entry_id": entry.get("id"),
                    "policy_version": entry.get("details", {}).get("policy_version"),
                    "at": entry["timestamp"],
                    "source": str(path),
                    "original_intent": state.get("intent"),
                    "latest_request": state.get("last_request"),
                    "supporting_task": state.get("supporting_task"),
                    "stop_reason": stop,
                    "tools": [
                        {"name": call.get("name"), "arguments": call.get("arguments"), "succeeded": call.get("id") in success_ids}
                        for call in calls
                    ],
                    "assistant_text": [value[:6000] for value in assistant],
                    "visible_word_count": sum(len(value.split()) for value in assistant),
                    "exceeds_60_word_budget": sum(len(value.split()) for value in assistant) > 60,
                    "successful_tool_actions": len(successful_actions),
                    "behavioral_review": "pending_agent_review",
                    "evidence_boundary": "No transcript outcome is proof that a stop was appropriate or a tool action useful.",
                }
            )
    return sorted(observations, key=lambda row: (parse_time(row["at"]), row["session_id"], row["entry_id"]))[:limit]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions-dir", type=Path, default=Path.home() / ".pi/agent/sessions")
    parser.add_argument("--since", required=True, help="Start timestamp, including timezone")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--policy-version", help="Only count firings tagged with this policy; never mix legacy sessions")
    parser.add_argument("--output", type=Path, required=True, help="Private JSON evidence outside the source checkout")
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("count must be 1..100")
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if root == output or root in output.parents:
        parser.error("raw interaction evidence must remain outside the source repository")
    observations = collect(args.sessions_dir, parse_time(args.since), args.count, args.policy_version)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "since": args.since,
        "policy_version": args.policy_version,
        "target": args.count,
        "observed": len(observations),
        "remaining_future_firings": max(0, args.count - len(observations)),
        "status": "ready_for_agent_review" if len(observations) == args.count else "future_evidence_pending",
        "observations": observations,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent, prefix=".observations-")
    with os.fdopen(descriptor, "w") as handle:
        json.dump(report, handle, indent=2)
    os.replace(temporary, output)
    print(json.dumps({key: value for key, value in report.items() if key != "observations"}, indent=2))


if __name__ == "__main__":
    main()
