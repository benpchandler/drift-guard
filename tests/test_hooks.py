import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conftest import OFF, ON, TANGENT, FakeJudge, events

from drift_guard.config import Config
from drift_guard.hooks import PromptPayload, StopPayload, handle_stop, handle_user_prompt
from drift_guard.store import Store

Make = Callable[..., dict[str, Any]]


def prompt(make: Make, cfg: Config, store: Store, judge: FakeJudge, text: str, **kw: Any) -> dict[str, Any] | None:
    return handle_user_prompt(PromptPayload.parse(make(text, **kw)), cfg, store, judge)


def test_first_prompt_sets_intent_without_judging(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge()
    out = prompt(payload, cfg, store, judge, "Fix the flaky login test")
    assert out is not None and "intent recorded" in out["systemMessage"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.intent == "Fix the flaky login test" and state.intent_source == "first_prompt"
    assert judge.states == []
    assert [e["action"] for e in events(store)] == ["intent_set"]


def test_on_topic_is_silent_off_topic_warns(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(ON, OFF)
    prompt(payload, cfg, store, judge, "Fix the flaky login test")
    assert prompt(payload, cfg, store, judge, "look at the fixture") is None
    out = prompt(payload, cfg, store, judge, "banana bread recipe?")
    assert out is not None and "drifting (drift" in out["systemMessage"] and "decision" not in out
    logged = events(store)[-1]
    assert logged["action"] == "warn" and logged["drift_score"] == 0.6 and logged["probabilities"]["serves_intent"] == list(OFF)
    assert judge.states[1]["intent"] == "Fix the flaky login test" and judge.states[1]["turn"] == "banana bread recipe?"


PASTED_INTENT = (
    '<pasted_content id="3ec6">\nMake sbt\'s follow-selection real in ~/Dev/switchbard (TASK-359).\n\n'
    "The proof of concept works and the owner approved it. "
    + "Lots more detail. " * 40
    + '\nWhen it works, squash-merge it when CI is green.\n</pasted_content id="3ec6">'
)


def test_warning_repeats_the_intent_as_its_opening_sentence(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF)
    prompt(payload, cfg, store, judge, PASTED_INTENT)
    out = prompt(payload, cfg, store, judge, "banana bread recipe?")
    assert out is not None
    message = out["systemMessage"]
    assert "Make sbt's follow-selection real in ~/Dev/switchbard (TASK-359)." in message
    assert "pasted_content" not in message and "[...]" not in message
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.intent == PASTED_INTENT.strip()  # the judge still sees all of it


def test_warn_mode_never_blocks(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF, OFF, OFF)
    prompt(payload, cfg, store, judge, "intent")
    outs = [prompt(payload, cfg, store, judge, f"off {i}") for i in range(3)]
    assert all(o is None or "decision" not in o for o in outs)
    assert [o is not None for o in outs] == [True, True, False]  # warn, sustained warn, then quiet


def test_block_mode_blocks_and_keeps_last_request(payload: Make, block_cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF, OFF)
    prompt(payload, block_cfg, store, judge, "intent")
    first = prompt(payload, block_cfg, store, judge, "off one")
    assert first is not None and "decision" not in first
    second = prompt(payload, block_cfg, store, judge, "off two")
    assert second is not None and second["decision"] == "block" and "drift: ack" in second["reason"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.last_request == "off one" and state.human.blocks == 1


def test_ack_resets_score_and_passes(payload: Make, block_cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF, OFF, OFF)
    prompt(payload, block_cfg, store, judge, "intent")
    prompt(payload, block_cfg, store, judge, "off one")
    prompt(payload, block_cfg, store, judge, "off two")
    out = prompt(payload, block_cfg, store, judge, "drift: ack off two")
    assert out is not None and "acknowledged" in out["systemMessage"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.human.score == 0.0 and state.intent == "intent"
    after = prompt(payload, block_cfg, store, judge, "off three")
    assert after is not None and "decision" not in after  # one off-topic turn after a reset only warns


def test_new_intent_replaces_intent(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF)
    prompt(payload, cfg, store, judge, "intent A")
    prompt(payload, cfg, store, judge, "off")
    out = prompt(payload, cfg, store, judge, "drift: new intent write the quarterly report")
    assert out is not None and "Drift: new intent" in out["systemMessage"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.intent == "write the quarterly report" and state.intent_source == "restated"
    assert state.human.score == 0.0 and state.human.warnings == 1


def test_new_intent_as_first_prompt(payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(), "drift: new intent B")
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.intent == "B"


def test_mistyped_escape_is_answered_not_judged(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF)
    prompt(payload, cfg, store, judge, "intent A")
    before = store.load(payload("x")["session_id"])
    out = prompt(payload, cfg, store, judge, "drift: new intent")
    assert out is not None and out["decision"] == "block"
    assert "drift: ack" in out["reason"] and "drift: new intent <text>" in out["reason"]
    assert store.load(payload("x")["session_id"]) == before  # no judgment, no state change


def test_slash_commands_are_skipped(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge()
    assert prompt(payload, cfg, store, judge, "/compact") is None
    assert store.load(payload("x")["session_id"]) is None and judge.states == []


def test_resumed_session_adopts_transcript_intent(payload: Make, cfg: Config, store: Store, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"content": "Original task: migrate the DB"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Migrated. Run tests?"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines))
    judge = FakeJudge(ON)
    assert prompt(payload, cfg, store, judge, "yes", transcript_path=str(path)) is None
    assert judge.states[0]["intent"] == "Original task: migrate the DB"
    assert judge.states[0]["previous_assistant_message"] == "Migrated. Run tests?"


def test_stop_judges_agent_against_intent_or_request(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(), "intent")
    # Off the intent but on the request: not agent drift.
    judge = FakeJudge({"serves_intent": OFF, "serves_request": ON}, {"serves_intent": OFF, "serves_request": OFF})
    assert handle_stop(StopPayload.parse(stop_payload("did the thing")), cfg, store, judge) is None
    out = handle_stop(StopPayload.parse(stop_payload("wandered off")), cfg, store, judge)
    assert out is not None and "agent is drifting" in out["systemMessage"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.last_reply == "wandered off" and state.agent.turns == 2
    assert [e["event"] for e in events(store)][-2:] == ["agent", "agent"]


def test_stop_block_mode_feeds_back_to_claude(payload: Make, stop_payload: Make, block_cfg: Config, store: Store) -> None:
    prompt(payload, block_cfg, store, FakeJudge(), "intent")
    judge = FakeJudge(OFF, OFF)
    handle_stop(StopPayload.parse(stop_payload("a")), block_cfg, store, judge)
    out = handle_stop(StopPayload.parse(stop_payload("b")), block_cfg, store, judge)
    assert out is not None and out["hookSpecificOutput"]["hookEventName"] == "Stop"


def test_stop_skips_when_hook_active_or_agent_off(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(), "intent")
    judge = FakeJudge()
    assert handle_stop(StopPayload.parse(stop_payload("x", stop_hook_active=True)), cfg, store, judge) is None
    off = Config(agent=False, state_dir=cfg.state_dir)
    assert handle_stop(StopPayload.parse(stop_payload("y")), off, store, judge) is None
    state = store.load(payload("x")["session_id"])
    assert judge.states == [] and state is not None and state.last_reply == "y"


def test_stop_without_session_state_is_noop(stop_payload: Make, cfg: Config, store: Store) -> None:
    assert handle_stop(StopPayload.parse(stop_payload("x")), cfg, store, FakeJudge()) is None


def test_tangent_then_short_reply_uses_last_reply_context(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(), "intent")
    quiet = Config(agent=False, state_dir=cfg.state_dir)
    handle_stop(StopPayload.parse(stop_payload("Shall I also update the docs?")), quiet, store, FakeJudge())
    judge = FakeJudge(TANGENT)
    prompt(payload, cfg, store, judge, "yes")
    assert judge.states[0]["previous_assistant_message"] == "Shall I also update the docs?"
