"""Read Claude Code transcripts (~/.claude/projects/**/<session>.jsonl) into human prompts and agent replies.

Only what a person typed counts as a human turn: tool results, meta messages, slash-command echoes, skill loads,
and harness notices are excluded.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_LINES = 200_000  # a transcript larger than this is truncated, never slurped whole
MAX_EXCHANGES = 500
TAIL_BYTES = 512 * 1024
PASTED_CONTENT_TAG = "<pasted_content"
# Text that arrives in the user role but was not typed by the person: harness tags, interrupts, skill loads,
# and messages relayed from other agent sessions.
_NOT_HUMAN_PREFIXES = (
    "<",
    "[Request interrupted",
    "Caveat:",
    "Base directory for this skill",
    "Another Claude session sent a message",
)


@dataclass(frozen=True)
class Exchange:
    """One human prompt and the agent's final text reply to it (empty if the agent never replied in text)."""

    prompt: str
    reply: str
    ts: str = ""  # when the prompt was sent (ISO 8601, from the transcript)


def _text_blocks(content: Any) -> list[str] | None:
    """Text of a message's content, or None when it carries a tool result (not a typed message)."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def is_typed_by_human(text: str) -> bool:
    """The single authority on whether user-role text is a person's prompt (used by the hooks and the backfill)."""
    stripped = text.strip()
    if stripped.startswith(PASTED_CONTENT_TAG):  # a prompt that opens with a paste is still typed by the person
        return True
    return bool(stripped) and not stripped.startswith(_NOT_HUMAN_PREFIXES)


def human_prompt(entry: dict[str, Any]) -> str | None:
    """The typed prompt in a transcript entry, or None if the entry is not a human turn."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    texts = _text_blocks((entry.get("message") or {}).get("content"))
    if not texts:
        return None
    text = "\n".join(texts).strip()
    return text if is_typed_by_human(text) else None


def assistant_text(entry: dict[str, Any]) -> str | None:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    texts = _text_blocks((entry.get("message") or {}).get("content")) or []
    text = "\n".join(texts).strip()
    return text or None


def _entries(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_LINES:
                return
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


def exchanges(path: Path) -> list[Exchange]:
    """Every human prompt in order, each paired with the last assistant text before the next prompt."""
    found: list[Exchange] = []
    prompt: str | None = None
    reply, ts = "", ""
    for entry in _entries(path):
        typed = human_prompt(entry)
        if typed is not None:
            if prompt is not None:
                found.append(Exchange(prompt, reply, ts))
            if len(found) >= MAX_EXCHANGES:
                return found
            prompt, reply, ts = typed, "", str(entry.get("timestamp", ""))
            continue
        text = assistant_text(entry)
        if text is not None and prompt is not None:
            reply = text
    if prompt is not None:
        found.append(Exchange(prompt, reply, ts))
    return found


def first_prompt(path: Path) -> str | None:
    """The first typed prompt of a transcript: the session's intent when the guard joins a resumed session."""
    for entry in _entries(path):
        typed = human_prompt(entry)
        if typed is not None and not typed.startswith("/"):
            return typed
    return None


def last_reply(path: Path, tail_bytes: int = TAIL_BYTES) -> str:
    """The last assistant text in a transcript's tail: context for judging a short human reply.

    Reads only the final `tail_bytes`, so a hook stays fast on a multi-megabyte transcript.
    """
    assert tail_bytes > 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - tail_bytes))
        lines = handle.read().decode(errors="replace").splitlines()
    reply = ""
    for line in lines[1:] if size > tail_bytes else lines:  # the first line of a tail is usually partial
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = assistant_text(entry) if isinstance(entry, dict) else None
        if text is not None:
            reply = text
    return reply
