import json
from pathlib import Path

from drift_guard import transcript


def write(path: Path, entries: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def text(t: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": t}]}


ENTRIES: list[dict[str, object]] = [
    {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
    {"type": "user", "message": {"content": "Fix the login test"}},
    {"type": "assistant", "message": text("Looking")},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "assistant", "message": text("Fixed it. Push?")},
    {"type": "user", "isMeta": True, "message": text("Base directory for this skill: x")},
    {"type": "user", "message": text("yes push")},
    {"type": "assistant", "isSidechain": True, "message": text("subagent chatter")},
    {"type": "user", "message": {"content": "[Request interrupted by user]"}},
]


def test_exchanges_pair_prompts_with_final_reply(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", ENTRIES)
    got = transcript.exchanges(path)
    assert got == [transcript.Exchange("Fix the login test", "Fixed it. Push?"), transcript.Exchange("yes push", "")]


def test_first_prompt_and_last_reply(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", ENTRIES)
    assert transcript.first_prompt(path) == "Fix the login test"
    assert transcript.last_reply(path) == "Fixed it. Push?"


def test_last_reply_reads_only_the_tail(tmp_path: Path) -> None:
    filler = [{"type": "assistant", "message": text("x" * 500)} for _ in range(50)]
    path = write(tmp_path / "s.jsonl", [*filler, {"type": "assistant", "message": text("final")}])
    assert transcript.last_reply(path, tail_bytes=2048) == "final"


def test_garbage_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text('not json\n{"type":"user","message":{"content":"hi"}}\n')
    assert transcript.first_prompt(path) == "hi"


def test_is_typed_by_human() -> None:
    assert transcript.is_typed_by_human("fix it")
    assert transcript.is_typed_by_human('<pasted_content id="1">\nlog\n</pasted_content id="1">\nwhy?')
    for harness in ("<task-notification>x", "Another Claude session sent a message: <teammate-message>", "  ", "Caveat: x"):
        assert not transcript.is_typed_by_human(harness)
