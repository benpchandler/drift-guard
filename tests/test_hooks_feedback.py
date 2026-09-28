import json
from collections.abc import Callable
from typing import Any

from conftest import OFF, ON, FakeJudge, events

from drift_guard.config import Config
from drift_guard.hooks import PromptPayload, StopPayload, handle_stop, handle_user_prompt
from drift_guard.store import Store

Make = Callable[..., dict[str, Any]]


def prompt(make: Make, cfg: Config, store: Store, judge: FakeJudge, text: str) -> dict[str, Any] | None:
    return handle_user_prompt(PromptPayload.parse(make(text)), cfg, store, judge)


def stop(make: Make, cfg: Config, store: Store, judge: FakeJudge, text: str) -> dict[str, Any] | None:
    return handle_stop(StopPayload.parse(make(text)), cfg, store, judge)


def labels(store: Store) -> list[dict[str, Any]]:
    return [json.loads(line) for line in store.labels_path.read_text().splitlines()]


def test_wrong_warning_is_labeled_blocked_and_resets_drift(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF, {"serves_intent": ON, "serves_request": ON})
    prompt(payload, cfg, store, judge, "Fix the login test")
    assert prompt(payload, cfg, store, judge, "banana bread?") is not None  # warned
    stop(stop_payload, cfg, store, judge, "Here is a recipe")  # a newer, quiet agent judgment
    no_jev = FakeJudge()
    out = prompt(payload, cfg, store, no_jev, "drift: feedback wrong, this is the same task")
    assert out == {"decision": "block", "reason": "Recorded: guard was wrong on 'banana bread?'. Drift reset."}
    assert no_jev.states == []
    label = labels(store)[-1]
    warned = next(e for e in events(store) if e["action"] == "warn")
    assert label["judgment_id"] == warned["id"] and label["outcome"] == "false_positive"
    assert label["note"] == "this is the same task" and label["questions_version"] == warned["questions_version"]
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.human.score == 0.0 and state.human.alerted == 0
    assert events(store)[-1]["action"] == "feedback"


def test_right_on_a_warning_keeps_drift(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF)
    prompt(payload, cfg, store, judge, "Fix the login test")
    prompt(payload, cfg, store, judge, "banana bread?")
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback right")
    assert out is not None and out["reason"] == "Recorded: guard was right on 'banana bread?'."
    state = store.load(payload("x")["session_id"])
    assert state is not None and state.human.score > 0.5


def test_missed_drift_on_a_quiet_turn(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(ON, {"serves_intent": ON, "serves_request": ON})
    prompt(payload, cfg, store, judge, "Fix the login test")
    prompt(payload, cfg, store, judge, "let's also redo the logo")
    stop(stop_payload, cfg, store, judge, "Logo redone")
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback missed")
    assert out is not None and out["reason"] == "Recorded: guard missed drift on 'let's also redo the logo'."
    assert labels(store)[-1]["judgment_event"] == "human"


def test_agent_warning_in_latest_exchange_is_the_target(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(ON, OFF)
    prompt(payload, cfg, store, judge, "Fix the login test")
    prompt(payload, cfg, store, judge, "look at the fixture")
    stop(stop_payload, cfg, store, judge, "I rewrote the billing module")  # agent warned
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback right")
    assert out is not None and "billing module" in out["reason"] and labels(store)[-1]["judgment_event"] == "agent"


def test_agent_judgment_from_an_earlier_exchange_is_ignored(payload: Make, stop_payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(ON, OFF, ON)
    prompt(payload, cfg, store, judge, "Fix the login test")
    prompt(payload, cfg, store, judge, "look at the fixture")
    stop(stop_payload, cfg, store, judge, "I rewrote the billing module")  # agent warned, then a new prompt
    prompt(payload, cfg, store, judge, "ok run the tests")
    prompt(payload, cfg, store, FakeJudge(), "drift: feedback right")
    assert labels(store)[-1]["excerpt"] == "ok run the tests" and labels(store)[-1]["outcome"] == "true_negative"


def test_free_text_note(payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(ON), "Fix the login test")
    prompt(payload, cfg, store, FakeJudge(ON), "check the fixture")
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback the warnings are too wordy")
    assert out is not None and out["reason"] == "Recorded note on 'check the fixture'."
    assert labels(store)[-1]["verdict"] == "note" and labels(store)[-1]["note"] == "the warnings are too wordy"


def test_feedback_before_any_judgment(payload: Make, cfg: Config, store: Store) -> None:
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback wrong")
    assert out is not None and out["decision"] == "block" and "no guard judgment" in out["reason"]
    assert labels(store)[-1]["judgment_id"] == "" and store.load(payload("x")["session_id"]) is None


def test_new_intent_keeps_the_labelable_judgment(payload: Make, cfg: Config, store: Store) -> None:
    prompt(payload, cfg, store, FakeJudge(), "Fix the login test")
    prompt(payload, cfg, store, FakeJudge(OFF), "banana bread?")
    prompt(payload, cfg, store, FakeJudge(), "drift: new intent bake banana bread")
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback right")
    assert out is not None and "banana bread?" in out["reason"]


def test_state_written_by_the_previous_version_still_loads(payload: Make, cfg: Config, store: Store) -> None:
    sid = payload("x")["session_id"]
    store.sessions.mkdir(parents=True)
    old = {
        "session_id": sid,
        "intent": "Fix it",
        "intent_source": "first_prompt",
        "intent_set_at": "2026-09-23T00:00:00+00:00",
        "human": {"score": 0.0, "turns": 1, "warnings": 0, "blocks": 0, "alerted": 0},
        "agent": {"score": 0.0, "turns": 0, "warnings": 0, "blocks": 0, "alerted": 0},
        "last_request": "Fix it",
        "last_reply": "",
    }
    (store.sessions / f"{sid}.json").write_text(json.dumps(old))
    out = prompt(payload, cfg, store, FakeJudge(), "drift: feedback right")
    assert out is not None and "no guard judgment" in out["reason"]
