import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import OFF, ON, FakeJudge, events

from drift_guard import cli, codex
from drift_guard.config import Config, Mode, from_env
from drift_guard.store import Store

SID = "thr_codex-123"


def prompt(text: str, **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": text, "cwd": "/work", **extra}


def stop(text: str | None, **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": "Stop", "session_id": SID, "last_assistant_message": text, "stop_hook_active": False, **extra}


def test_runtime_defaults_isolate_state() -> None:
    claude, native = from_env({}), from_env({}, runtime="codex")
    assert claude.runtime == "claude" and native.runtime == "codex"
    assert native.state_dir.name == "drift-codex" and native.state_dir != claude.state_dir
    assert native.mode is Mode.WARN
    assert from_env({"DRIFT_STATE_DIR": "/tmp/custom"}, runtime="codex").state_dir == Path("/tmp/custom")


def test_direct_fields_ignore_transcript_and_keep_reply_context(cfg: Config, store: Store, tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"Wrong historical goal"}}\n')
    judge = FakeJudge(ON, ON)
    codex.handle("user-prompt", prompt("Current goal", transcript_path=str(transcript)), cfg, store, judge)
    codex.handle("stop", stop("Implemented the goal"), cfg, store, judge)
    codex.handle("user-prompt", prompt("yes", transcript_path=str(transcript)), cfg, store, judge)
    state = store.load(SID)
    assert state is not None and state.intent == "Current goal" and state.intent_source == "first_prompt"
    assert judge.states[0]["response"] == "Implemented the goal"
    assert judge.states[1]["previous_assistant_message"] == "Implemented the goal"
    assert judge.states[1]["intent"] == "Current goal"


def test_native_stop_block_is_continuation_and_cannot_loop(cfg: Config, store: Store) -> None:
    cfg = replace(cfg, runtime="codex", mode=Mode.BLOCK)
    judge = FakeJudge(OFF, OFF)
    codex.handle("user-prompt", prompt("goal"), cfg, store, judge)
    first = codex.handle("stop", stop("unrelated first"), cfg, store, judge)
    assert first is not None and "decision" not in first
    out = codex.handle("stop", stop("unrelated second"), cfg, store, judge)
    assert out is not None and out["decision"] == "block" and "hookSpecificOutput" not in out
    assert out["reason"].startswith(codex.CONTINUATION_PREFIX)
    before = store.load(SID)
    assert codex.handle("user-prompt", prompt(out["reason"]), cfg, store, judge) is None
    assert codex.handle("user-prompt", prompt("[Jev Stop review] continue"), cfg, store, judge) is None
    assert store.load(SID) == before
    assert codex.handle("stop", stop("refocused", stop_hook_active=True), cfg, store, judge) is None
    assert len(judge.states) == 2


def test_warn_mode_and_null_reply(cfg: Config, store: Store) -> None:
    cfg = replace(cfg, runtime="codex")
    judge = FakeJudge(OFF, OFF)
    codex.handle("user-prompt", prompt("goal"), cfg, store, judge)
    assert codex.handle("stop", stop(None), cfg, store, judge) is None
    for text in ["off one", "off two"]:
        output = codex.handle("stop", stop(text), cfg, store, judge)
        assert output is not None and "systemMessage" in output and "decision" not in output


def test_cli_native_dispatch_never_requests_claude_recap(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    judge = FakeJudge()
    monkeypatch.setattr(judge, "close", lambda: None, raising=False)
    monkeypatch.setattr(cli, "JevJudge", lambda *_: judge)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(prompt("goal"))))
    monkeypatch.setenv("DRIFT_STATE_DIR", str(cfg.state_dir))

    def forbidden(*_: Any) -> None:
        pytest.fail("Codex must not request a Claude recap")

    monkeypatch.setattr(cli.synthesis, "request_recap", forbidden)
    assert cli.main(["codex-hook", "user-prompt"]) == 0
    assert "intent recorded" in json.loads(capsys.readouterr().out)["systemMessage"]
    assert store.load(SID) is not None and not store.recaps.exists()


@pytest.mark.parametrize("raw", ["not json", json.dumps(stop("x", stop_hook_active="false")), json.dumps(stop({}))])
def test_malformed_native_input_fails_open(
    raw: str, cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = FakeJudge()
    monkeypatch.setattr(judge, "close", lambda: None, raising=False)
    monkeypatch.setattr(cli, "JevJudge", lambda *_: judge)
    assert cli.run_hook("stop", raw, replace(cfg, runtime="codex")) == ""
    assert events(store)[-1]["action"] == "error"
